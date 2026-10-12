"""Everything this package needs from git, behind one seam.

Kept separate from `detect` so the detectors stay pure and the suite can hold the 2026-09-17
tangle as a literal instead of standing up four worktrees. The `run` parameter is the seam:
tests pass a fake, production passes the default.
"""

from __future__ import annotations

import os
import subprocess
from typing import Callable

from .model import RepoFacts

Runner = Callable[[list[str], str], str]


def _run(argv: list[str], cwd: str) -> str:
    """Return stdout, or "" for any failure. Never raises; a missing repo is not an error here."""
    try:
        out = subprocess.run(argv, cwd=cwd or None, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def repo_root(path: str, run: Runner = _run) -> str | None:
    """The MAIN checkout of the repo containing `path`, not the worktree.

    This is the whole reason `git rev-parse --show-toplevel` is wrong here: in a worktree it
    returns the worktree, so four sessions in four worktrees would register as four unrelated
    repos and no detector would ever fire. `--git-common-dir` points at the shared `.git`, and
    its parent is the main checkout every worktree hangs off.
    """
    path = os.path.abspath(os.path.expanduser(path))
    common = run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], path)
    if not common:
        return None
    root = os.path.dirname(common.rstrip("/")) if os.path.basename(
        common.rstrip("/")) == ".git" else common
    return os.path.realpath(root) or None


def workdir(path: str, run: Runner = _run) -> str:
    """The working tree `path` sits in -- the worktree, when there is one."""
    top = run(["git", "rev-parse", "--show-toplevel"], os.path.abspath(path))
    return os.path.realpath(top) if top else os.path.abspath(path)


def branch(path: str, run: Runner = _run) -> str | None:
    b = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], path)
    return None if not b or b == "HEAD" else b


def default_branch(path: str, run: Runner = _run) -> str:
    ref = run(["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"], path)
    if ref.startswith("origin/"):
        return ref.split("/", 1)[1]
    for guess in ("main", "master"):
        if run(["git", "rev-parse", "--verify", "--quiet", guess], path):
            return guess
    return "main"


def dirty_paths(path: str, run: Runner = _run) -> tuple[str, ...]:
    out = run(["git", "status", "--porcelain"], path)
    return tuple(line[3:] for line in out.splitlines() if len(line) > 3)


def facts(repo: str, run: Runner = _run) -> RepoFacts:
    """One gather, passed to the detectors. Nothing re-queries git downstream.

    No `git fetch`: this runs on every session start and a network round trip there would be
    felt. `remote_default_sha` is therefore as fresh as the last fetch, which understates
    staleness rather than inventing it -- the same tradeoff `repo-status` already makes.
    """
    dflt = default_branch(repo, run)
    return RepoFacts(
        repo_root=repo,
        default_branch=dflt,
        local_default_sha=run(["git", "rev-parse", "--verify", "--quiet", dflt], repo),
        remote_default_sha=run(
            ["git", "rev-parse", "--verify", "--quiet", f"origin/{dflt}"], repo),
        dirty_paths=dirty_paths(repo, run),
    )
