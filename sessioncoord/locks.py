"""Named per-repo locks for operations that mutate shared state.

The one operation this exists for today is a merge. `pr-merge run` waits for CI and for a
Copilot review -- minutes -- and then, in a few seconds at the end, removes a worktree, deletes
two branches, prunes, and fast-forwards the main checkout. Two of those overlapping in one repo
is the normal case rather than a rare one, because the wait is long enough that any two merges
started within ten minutes of each other will collide.

What is NOT at risk, because `pr-merge` already designed it out: a branch switch in the shared
checkout. `prmerge/git.py` states that nothing in it switches a branch, and it fast-forwards
rather than checking out. That was the failure that cost the 2026-09-17 afternoon, and it
cannot arrive by automation.

What remains, and what this covers:

  * Ref and worktree lock contention -- concurrent `fetch --prune`, `worktree remove` and
    `worktree prune` under one `.git`. Usually survivable, occasionally a loud transient
    failure in the middle of an otherwise finished merge.
  * Merge ordering under `strict_required_status_checks`. `acme/infra` requires a PR to be
    up to date with `main`, so the first merge to land makes every other open PR stale. The
    second session discovers this only after spending a full CI-and-review cycle. Serializing
    the merge step means it finds out before it waits, not after.

Deliberately NOT held across the CI wait. That is where the minutes are, it is pure polling
against GitHub, and serializing it would throw away the parallelism that makes running several
sessions worthwhile in the first place.

`flock` is right here and wrong for presence, which is worth stating because the two look
similar. A merge runs in one long-lived process that can hold a lock for its whole life, and a
second merge genuinely should wait. A session's presence is a fact about liveness, spread
across many short-lived shells, and nothing ever blocks on it.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import os
import subprocess
import time
from pathlib import Path
from typing import Iterator, Sequence

from .store import check_name, home, state_dir


class Busy(RuntimeError):
    """Someone else holds this lock. The message names them where it can."""


# Passed as `repo_root` to take a lock at the ESTATE root instead of inside one repo's state
# directory. The doorbell budget is per session and per hour across every repo, so a per-repo
# lock would serialise the wrong thing: two `msg` calls in different repos would not contend,
# which is exactly the pair that needs to.
ESTATE = None


def _lock_path(repo_root: str | None, name: str) -> Path:
    check_name(name, "lock name")  # a name becomes a path component; `../other` is CLI-reachable
    d = (home() if repo_root is ESTATE else state_dir(repo_root)) / "locks"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{name}.lock"


def _note_path(repo_root: str, name: str) -> Path:
    return _lock_path(repo_root, name).with_suffix(".owner")


def holder(repo_root: str, name: str) -> str | None:
    """Who holds this lock right now, or None if it is free.

    Answered by trying to take the lock rather than by reading the note, because the note
    outlives the lock on purpose -- a crashed holder leaves a breadcrumb worth having, and it
    must never be mistaken for the lock itself.
    """
    path = _lock_path(repo_root, name)
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    except OSError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        note = _note_path(repo_root, name)
        try:
            return note.read_text().strip() or "another session"
        except OSError:
            return "another session"
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return None
    finally:
        os.close(fd)


@contextlib.contextmanager
def hold(repo_root: str, name: str, *, wait: float = 300.0, owner: str = "") -> Iterator[Path]:
    """Hold a named lock for the duration of the block.

    `wait` is a ceiling in seconds, not a promise to block that long -- an unbounded wait would
    park a session forever behind one that died in a way the kernel did not notice. On timeout
    this raises `Busy` naming the holder, so the caller reports rather than guesses.

    Release is the kernel closing the fd, so a holder that crashes, is killed, or exits via an
    exception frees the lock immediately. That is the property a pid file does not have, and
    the reason a wedged merge cannot block every later one.
    """
    path = _lock_path(repo_root, name)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    deadline = time.monotonic() + max(0.0, wait)
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as e:
                if e.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                if time.monotonic() >= deadline:
                    who = holder(repo_root, name) or "another session"
                    raise Busy(
                        f"`{name}` in {repo_root} is held by {who}. Nothing was done. Wait for "
                        f"it to finish, or ask that session where it is."
                    ) from None
                time.sleep(0.25)
        # Written on EVERY acquisition, not only when a label was supplied. Skipping it left
        # the previous holder's name behind, so an unlabelled holder made a waiter go and ask
        # a session that had already finished -- worse than saying nothing, because a name
        # gets acted on. The pid lets a reader tell a live note from a crashed holder's.
        try:
            _note_path(repo_root, name).write_text(
                f"{owner or 'an unlabelled holder'} (pid {os.getpid()})\n")
        except OSError:
            pass
        yield path
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def run(repo_root: str, name: str, argv: Sequence[str], *, wait: float = 300.0,
        owner: str = "") -> int:
    """Run a command under a named lock. Returns its exit code.

    The wrapper form exists so a lock cannot be leaked: there is no `acquire` for a caller to
    forget to pair with a `release`, and a killed wrapper drops the lock with its fd.
    """
    with hold(repo_root, name, wait=wait, owner=owner):
        try:
            return subprocess.run(list(argv)).returncode
        except OSError as e:
            raise RuntimeError(f"could not run {argv[0]!r}: {e}") from e
