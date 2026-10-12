"""Is this presence record still a running session?

The predicate is deliberately stricter than `kill -0 <pid>`. A pid outlives its session --
it gets recycled, and a stale record whose pid now belongs to some unrelated process would
read as live forever. So the test is positional: the recorded pid must still be the claude
process occupying the recorded pane. That is the idiom `tmux-claude` already uses when it
drops registry lines at save time, and it is right for the same reason.

The second rule is the one both reviewers of PR #3 led with, independently. **An unavailable
observation is not an observation of absence.** `None` means tmux could not be consulted --
timeout, missing server, permission error, a server mid-restart -- and every caller treats it
as "assume everyone is still there". `{}` means tmux answered and there is nobody. Collapsing
the two made a ten-second tmux hiccup erase every presence record in a repo, after which the
PreToolUse gate saw no peers and permitted exactly the commit it exists to refuse. Failing
open at the moment observation fails is the shape this package's own detectors moralise about.

Pure. The pane -> claude-pid mapping is gathered by `tmuxio` and passed in.
"""

from __future__ import annotations

from typing import Iterable, Mapping

from .model import Presence

# None = could not look. {} = looked, nobody there. The distinction is load-bearing.
PaneMap = "Mapping[str, int | None] | None"


# pid -> start fingerprint (None for a pid that is gone). None as a whole = could not look.
StartMap = "Mapping[int, str | None] | None"


def is_live(p: Presence, pane_claude_pids: PaneMap, server: str | None = None,
            pid_starts: StartMap = None) -> bool:
    """True iff `p` is still the session it recorded.

    Two proofs, chosen by what the record carries. A record with a tmux pane is live iff that
    pane still holds its claude process -- the original rule. A record WITHOUT a pane, which is
    every session on a machine that does not use tmux, is live iff its pid is held by a process
    that started at the recorded moment.

    Each proof fails open on its own source. `pane_claude_pids` None (tmux could not be asked)
    keeps every pane record; `pid_starts` None (the process table could not be read) keeps
    every pid record. Before the split, a box with no tmux at all had no way to be live, so
    nothing outside tmux could ever register -- the package silently did nothing on a laptop.
    """
    if p.pane:
        if pane_claude_pids is None:
            return True
        # A pane id only means anything within one tmux server incarnation. An empty recorded
        # server is a record from before the field existed and is given the benefit of the
        # doubt, so upgrading the tool does not sweep every session on the box.
        if server and p.tmux_server and p.tmux_server != server:
            return False
        if p.claude_pid is None:
            return False
        return pane_claude_pids.get(p.pane) == p.claude_pid
    if p.claude_pid is None or not p.pid_started:
        # Headless runs, and records that carry neither proof, cannot be placed. Treating them
        # as live would let an unplaceable record warn real sessions forever.
        return False
    if pid_starts is None:
        return True
    return pid_starts.get(p.claude_pid) == p.pid_started


def partition(
    records: Iterable[Presence],
    pane_claude_pids: PaneMap,
    server: str | None = None,
    pid_starts: StartMap = None,
) -> tuple[list[Presence], list[Presence]]:
    """Split a registry into (live, stale). Stale records are the sweeper's input.

    With an unavailable source nothing that depends on it is stale, so a caller that deletes
    what this returns cannot destroy the board on a transient failure.
    """
    live: list[Presence] = []
    dead: list[Presence] = []
    for r in records:
        (live if is_live(r, pane_claude_pids, server, pid_starts) else dead).append(r)
    return live, dead
