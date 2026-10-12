"""A presence record is a claim about a running process. These tests pin what makes it false.

The whole registry self-cleans on this predicate, so a wrong answer here is either a session
that silently vanishes from the board while still working (missed collisions) or a dead
session that keeps warning live ones forever (noise nobody reads). Both have been observed in
the pane registry this borrows its idiom from: `tmux-claude save` drops a registry line whose
recorded pid is not the process still in the pane, for exactly this reason.
"""

from __future__ import annotations

from sessioncoord.liveness import is_live, partition
from sessioncoord.model import Presence


def p(session_id="s1", pane="%67", pid=4141161, **kw):
    return Presence(
        session_id=session_id,
        repo_root="/home/user/code/acme/infra",
        workdir=kw.pop("workdir", "/home/user/code/acme/infra"),
        branch=kw.pop("branch", "main"),
        pane=pane,
        window=kw.pop("window", "infra:11"),
        claude_pid=pid,
        **kw,
    )


class TestIsLive:
    def test_live_when_the_recorded_pid_is_still_the_claude_process_in_that_pane(self):
        assert is_live(p(pane="%67", pid=4141161), {"%67": 4141161})

    def test_dead_when_the_pane_is_gone(self):
        # The window was closed. Nothing to reattach to; the record is scrap.
        assert not is_live(p(pane="%67", pid=4141161), {"%53": 3157817})

    def test_dead_when_another_claude_now_occupies_the_pane(self):
        # Daniel closed the session and started a new one in the same window. This is the case
        # a bare `kill -0 pid` gets wrong: the OLD pid may still be alive as somebody else's
        # process, and the pane is genuinely in use -- but not by this record's session.
        assert not is_live(p(pane="%67", pid=4141161), {"%67": 999999})

    def test_dead_when_the_pane_holds_no_claude_at_all(self):
        # Window 8 in the 2026-09-17 incident: Daniel closed the session but kept the window,
        # because closing it would have renumbered the others. A pane with only bash in it.
        assert not is_live(p(pane="%67", pid=4141161), {"%67": None})

    def test_dead_when_the_record_never_knew_its_pane(self):
        # A headless `claude -p` run, or a session started outside tmux.
        assert not is_live(p(pane=None, pid=4141161), {"%67": 4141161})

    def test_dead_when_the_record_never_knew_its_pid(self):
        assert not is_live(p(pane="%67", pid=None), {"%67": 4141161})


class TestPartition:
    def test_splits_a_registry_into_live_and_stale(self):
        live_one = p("s1", pane="%67", pid=4141161)
        live_two = p("s2", pane="%53", pid=3157817)
        stale = p("s3", pane="%60", pid=3401077)
        panes = {"%67": 4141161, "%53": 3157817, "%60": None}

        alive, dead = partition([live_one, live_two, stale], panes)

        assert [r.session_id for r in alive] == ["s1", "s2"]
        assert [r.session_id for r in dead] == ["s3"]

    def test_an_empty_registry_is_not_an_error(self):
        assert partition([], {}) == ([], [])
