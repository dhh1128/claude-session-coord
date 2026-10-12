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
PaneMap = Mapping[str, "int | None"] | None


def is_live(p: Presence, pane_claude_pids: PaneMap, server: str | None = None) -> bool:
    """True iff `p`'s pane still holds `p`'s claude process.

    `pane_claude_pids` maps a tmux pane id to the pid of the claude process running under it,
    or None when the pane holds no claude at all (a window Daniel kept open after closing the
    session -- he does this to avoid renumbering the others). The whole mapping is None when
    tmux could not be consulted, and then every record is assumed live.
    """
    if pane_claude_pids is None:
        return True
    # A pane id only means anything within one tmux server incarnation. An empty recorded
    # server is a record from before the field existed and is given the benefit of the doubt,
    # so upgrading the tool does not sweep every session on the box.
    if server and p.tmux_server and p.tmux_server != server:
        return False
    if not p.pane or p.claude_pid is None:
        # Sessions outside tmux, and headless `claude -p` runs, cannot be placed. Treating
        # them as live would let an unplaceable record warn real sessions forever.
        return False
    return pane_claude_pids.get(p.pane) == p.claude_pid


def partition(
    records: Iterable[Presence],
    pane_claude_pids: PaneMap,
    server: str | None = None,
) -> tuple[list[Presence], list[Presence]]:
    """Split a registry into (live, stale). Stale records are the sweeper's input.

    With an unavailable source nothing is stale, so a caller that deletes what this returns
    cannot destroy the board on a transient failure.
    """
    live: list[Presence] = []
    dead: list[Presence] = []
    for r in records:
        (live if is_live(r, pane_claude_pids, server) else dead).append(r)
    return live, dead
