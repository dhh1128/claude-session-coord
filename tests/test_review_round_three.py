"""The third review round.

By this point the findings are narrower but the shape repeats: every one of them is a place
where a partial or refused result was reported as a complete or successful one. That is the
same defect as the round-one blocker, and it kept turning up in new corners — a skipped tmux
row, a refused unlink counted as a deletion, a validation that ran after the destructive step.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
from pathlib import Path

import pytest
from sessioncoord import cli, store, tmuxio
from sessioncoord.model import Presence


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_COORD_HOME", str(tmp_path / "state"))
    return tmp_path


@pytest.fixture
def fake_world(monkeypatch):
    monkeypatch.setattr(cli.gitio, "repo_root", lambda p, run=None: "/repo")
    monkeypatch.setattr(cli.gitio, "workdir", lambda p, run=None: "/repo")
    monkeypatch.setattr(cli.gitio, "branch", lambda p, run=None: "main")
    monkeypatch.setattr(cli.gitio, "facts", lambda r, run=None: None)
    monkeypatch.setattr(cli.tmuxio, "claude_pids", lambda run=None: {"%1": 111, "%2": 222})
    monkeypatch.setattr(cli.tmuxio, "window_of", lambda p, run=None: "me:1")


def rec(sid, pane="%1", pid=111, workdir="/repo", **kw):
    return Presence(session_id=sid, repo_root="/repo", workdir=workdir, pane=pane,
                    claude_pid=pid, **kw)


class TestValidationRunsBeforeAnythingDestructive:
    def test_a_malformed_id_does_not_delete_the_live_record_first(self, home, fake_world,
                                                                  capsys):
        # The superseded record used to be unlinked before `store.save` validated the new id,
        # so a bad id deregistered a running session and THEN reported an error -- leaving the
        # pane invisible, which is the single state this tool must never produce.
        cli.main(["register", "--repo", "/repo", "--session", "good", "--pane", "%1",
                  "--pid", "111", "--charter", "Real work."])
        assert cli.main(["register", "--repo", "/repo", "--session", "../evil", "--pane", "%1",
                         "--pid", "111"]) == 2
        assert [p.session_id for p in store.load_all("/repo")] == ["good"]


class TestMsgUsesItsOwnGuard:
    def test_the_recorded_pid_is_passed_through_to_send(self, home, fake_world, monkeypatch):
        # `send` documents expect_pid as the guard against pane-id reuse, and the CLI was not
        # using it -- so a window reassigned between resolutions would have been typed into as
        # though it were the intended peer.
        store.save(rec("peer", pane="%9", pid=4141161, window="infra:11"))
        seen = {}

        def fake_send(target, text, run=None, expect_pid=None, **kw):
            seen["expect_pid"] = expect_pid
            return "%9"

        monkeypatch.setattr(cli.tmuxio, "send", fake_send)
        cli.main(["msg", "--repo", "/repo", "infra:11", "a message for that peer"])
        assert seen["expect_pid"] == 4141161

    def test_an_explicit_pid_wins(self, home, fake_world, monkeypatch):
        seen = {}
        monkeypatch.setattr(cli.tmuxio, "send",
                            lambda t, x, run=None, expect_pid=None, **kw: seen.update(p=expect_pid))
        cli.main(["msg", "--repo", "/repo", "%9", "hello there", "--pid", "777"])
        assert seen["p"] == 777

    def test_an_unregistered_target_still_works_without_a_pid(self, home, fake_world,
                                                              monkeypatch):
        # Nothing better to check against; refusing would make the tool useless for a peer that
        # has not registered yet.
        seen = {}
        monkeypatch.setattr(cli.tmuxio, "send",
                            lambda t, x, run=None, expect_pid=None, **kw: seen.update(p=expect_pid))
        assert cli.main(["msg", "--repo", "/repo", "%404", "hello there"]) == 0
        assert seen["p"] is None


class TestAPartialTmuxAnswerIsNotAnAnswer:
    def test_a_malformed_row_makes_the_whole_result_unknown(self, monkeypatch):
        monkeypatch.setattr(tmuxio, "_children", lambda: {})
        listing = "%1\tme:1\t1\t/repo\nthis row is not the shape we asked for"
        assert tmuxio.panes(lambda argv: listing) is None

    def test_and_therefore_sweep_leaves_the_board_alone(self, home, monkeypatch):
        monkeypatch.setattr(tmuxio, "_children", lambda: {})
        store.save(rec("a"))
        listing = "%1\tme:1\t1\t/repo\nmangled"
        store.sweep("/repo", tmuxio.claude_pids(lambda argv: listing))
        assert len(store.load_all("/repo")) == 1

    def test_an_unparseable_pane_pid_is_also_unknown(self, monkeypatch):
        monkeypatch.setattr(tmuxio, "_children", lambda: {})
        assert tmuxio.panes(lambda argv: "%1\tme:1\tnot-a-pid\t/repo") is None


class TestSweepReportsOnlyWhatItDropped:
    def test_a_refused_removal_is_not_reported_as_a_drop(self, home):
        # It was reported as dropped, and reported again on every later sweep: a false
        # statement, repeated, about the one thing the command exists to tell you.
        bad = store.state_dir("/repo") / "sessions"
        bad.mkdir(parents=True, exist_ok=True)
        (bad / "hostile.json").write_text(
            Presence(session_id="../../evil", repo_root="/repo", workdir="/repo",
                     pane="%9", claude_pid=9).to_json())
        assert store.sweep("/repo", {}) == []

    def test_a_genuine_drop_is_still_reported(self, home):
        store.save(rec("gone", pane="%9", pid=9))
        assert [p.session_id for p in store.sweep("/repo", {})] == ["gone"]


HOOK = Path(__file__).resolve().parents[1] / "hooks" / "session-coord-gate-check"
_spec = importlib.util.spec_from_loader(
    "gate_check3", importlib.machinery.SourceFileLoader("gate_check3", str(HOOK)))
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)


@pytest.mark.parametrize("command", [
    "git -c user.name=x commit -m x",            # already matched; pinned so it stays matching
    "git -c user.email=a@b -c core.pager=cat commit -s",
    "git --no-pager commit -s -m x",             # this one genuinely missed
    "git --no-optional-locks add -A",
    "git --literal-pathspecs add .",
    "git --namespace foo commit -s",
])
def test_a_git_global_option_does_not_hide_the_write(command):
    assert gate.analyse(command, "/repo")[0], command


@pytest.mark.parametrize("command", [
    "git --version",
    "echo git --no-pager commit",
    "git --no-pager log --oneline",
])
def test_the_global_option_allowance_does_not_over_match(command):
    assert not gate.analyse(command, "/repo")[0], command
