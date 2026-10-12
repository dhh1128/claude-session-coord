""""I could not observe" must never be spelled the same way as "nobody is there".

Both reviewers of PR #3 led with this, independently, and they were right. The original
`tmuxio._run` mapped every failure -- timeout, missing server, permission error, tmux
restarting -- to an empty string, `panes()` turned that into `{}`, `is_live()` read every
absent pane as dead, and `sweep()` then unlinked every presence record in the repo. The next
`check` reported no peers, and the PreToolUse gate permitted exactly the commit it exists to
refuse.

That is fail-open at the moment observation fails, which is the shape this package's own
detectors moralise about. So: an unavailable source is `None`, distinct from an empty `{}`,
and every consumer treats `None` as "assume everyone is still there".
"""

from __future__ import annotations

import pytest
from sessioncoord import store, tmuxio
from sessioncoord.liveness import is_live, partition
from sessioncoord.model import Presence


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_COORD_HOME", str(tmp_path / "state"))
    return tmp_path


REPO = "/home/user/code/acme/infra"


def rec(sid="s1", pane="%1", pid=100):
    return Presence(session_id=sid, repo_root=REPO, workdir=REPO, pane=pane, claude_pid=pid)


class TestUnknownIsNotEmpty:
    def test_a_session_is_assumed_live_when_the_source_is_unavailable(self):
        assert is_live(rec(), None)

    def test_a_session_is_dead_when_the_source_is_available_and_lacks_it(self):
        assert not is_live(rec(), {})

    def test_partition_keeps_everyone_when_the_source_is_unavailable(self):
        rs = [rec("a", "%1", 1), rec("b", "%2", 2)]
        alive, dead = partition(rs, None)
        assert alive == rs and dead == []

    def test_sweep_deletes_nothing_when_the_source_is_unavailable(self, home):
        # The blocker. A ten-second tmux timeout must not erase the board.
        store.save(rec("a", "%1", 1))
        store.save(rec("b", "%2", 2))
        assert store.sweep(REPO, None) == []
        assert len(store.load_all(REPO)) == 2

    def test_sweep_still_works_when_the_source_is_genuinely_empty(self, home):
        # An empty tmux server is a real observation: nobody is running. Records go.
        store.save(rec("a", "%1", 1))
        assert [p.session_id for p in store.sweep(REPO, {})] == ["a"]


class FailingTmux:
    """Every tmux invocation fails, the way a restarting server behaves."""

    def __call__(self, argv):
        return None


class EmptyTmux:
    """tmux answers, and the answer is that there are no panes."""

    def __call__(self, argv):
        return ""


class TestAdapterReportsUnavailability:
    def test_a_failing_tmux_yields_unknown_rather_than_empty(self, monkeypatch):
        monkeypatch.setattr(tmuxio, "_children", lambda: {})
        assert tmuxio.claude_pids(FailingTmux()) is None

    def test_a_genuinely_empty_tmux_yields_an_empty_map(self, monkeypatch):
        monkeypatch.setattr(tmuxio, "_children", lambda: {})
        assert tmuxio.claude_pids(EmptyTmux()) == {}

    def test_the_unknown_result_survives_a_round_trip_through_sweep(self, home, monkeypatch):
        # The test Codex asked for: the adapter's sentinel must be exercised through the
        # consumer that would destroy data, not merely asserted at the adapter boundary.
        monkeypatch.setattr(tmuxio, "_children", lambda: {})
        store.save(rec("a", "%1", 1))
        store.sweep(REPO, tmuxio.claude_pids(FailingTmux()))
        assert len(store.load_all(REPO)) == 1
