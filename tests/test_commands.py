"""The surface the hooks and the skill call. Exit codes matter as much as output here.

`check` is wired into a PreToolUse gate, so its exit code decides whether a tool call is
refused. And the quiet case is a contract, not a nicety: one session alone in a repo is almost
every session, and anything printed there gets the whole tool muted.
"""

from __future__ import annotations

import json

import pytest
from sessioncoord import cli, store
from sessioncoord.model import Presence


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_COORD_HOME", str(tmp_path / "state"))
    return tmp_path


@pytest.fixture
def fake_world(monkeypatch):
    """A repo with no git and no tmux, so the CLI is exercised without either."""
    monkeypatch.setattr(cli.gitio, "repo_root", lambda p, run=None: "/repo")
    monkeypatch.setattr(cli.gitio, "workdir", lambda p, run=None: "/repo")
    monkeypatch.setattr(cli.gitio, "branch", lambda p, run=None: "main")
    monkeypatch.setattr(cli.gitio, "facts", lambda r, run=None: None)
    monkeypatch.setattr(cli.tmuxio, "claude_pids", lambda run=None: {"%1": 111, "%2": 222})
    monkeypatch.setattr(cli.tmuxio, "window_of", lambda p, run=None: "me:1")


def register(session_id, workdir="/repo", pane="%1", pid=111, **kw):
    store.save(Presence(session_id=session_id, repo_root="/repo", workdir=workdir,
                        pane=pane, claude_pid=pid, window=kw.pop("window", "me:1"), **kw))


class TestBoard:
    def test_says_nothing_when_one_session_is_alone(self, home, fake_world, capsys):
        register("solo")
        assert cli.main(["board", "--repo", "/repo", "--quiet"]) == 0
        assert capsys.readouterr().out == ""

    def test_lists_everyone_when_asked_directly(self, home, fake_world, capsys):
        register("solo", charter="Doing the thing.")
        assert cli.main(["board", "--repo", "/repo"]) == 0
        assert "Doing the thing." in capsys.readouterr().out

    def test_shows_peers_without_being_asked_when_there_are_any(self, home, fake_world, capsys):
        register("a", pane="%1", pid=111)
        register("b", pane="%2", pid=222, workdir="/repo/.worktrees/b")
        assert cli.main(["board", "--repo", "/repo", "--quiet"]) == 0
        assert "2 sessions" in capsys.readouterr().out


class TestCheck:
    def test_exit_zero_and_silent_for_a_clean_board(self, home, fake_world, capsys):
        register("solo", workdir="/repo/.worktrees/solo")
        assert cli.main(["check", "--repo", "/repo", "--quiet"]) == 0
        assert capsys.readouterr().out == ""

    def test_exit_two_on_a_blocking_finding(self, home, fake_world, capsys):
        # Two sessions in one tree. The PreToolUse gate turns this exit code into a refusal.
        register("a", pane="%1", pid=111, workdir="/repo")
        register("b", pane="%2", pid=222, workdir="/repo")
        assert cli.main(["check", "--repo", "/repo"]) == 2
        assert "shared-tree" in capsys.readouterr().out

    def test_exit_one_on_a_warning(self, home, fake_world, capsys):
        register("a", pane="%1", pid=111, workdir="/repo")
        register("b", pane="%2", pid=222, workdir="/repo/.worktrees/b")
        assert cli.main(["check", "--repo", "/repo"]) == 1

    def test_json_output_for_a_caller_that_parses(self, home, fake_world, capsys):
        register("a", pane="%1", pid=111, workdir="/repo")
        register("b", pane="%2", pid=222, workdir="/repo")
        cli.main(["check", "--repo", "/repo", "--json"])
        assert json.loads(capsys.readouterr().out)[0]["code"] == "shared-tree"

    def test_a_dead_session_stops_counting_immediately(self, home, fake_world, capsys):
        # %3 holds no claude. Without the sweep, a crashed session warns live ones forever.
        register("a", pane="%1", pid=111, workdir="/repo")
        register("ghost", pane="%3", pid=333, workdir="/repo")
        assert cli.main(["check", "--repo", "/repo", "--quiet"]) == 0


class TestRegister:
    def test_records_the_session_and_reports_peers(self, home, fake_world, capsys):
        rc = cli.main(["register", "--repo", "/repo", "--session", "s1", "--pane", "%1",
                       "--pid", "111", "--charter", "Building the coordination tool."])
        assert rc == 0
        assert store.load_all("/repo")[0].charter == "Building the coordination tool."

    def test_claims_are_recorded_so_overlap_can_be_detected(self, home, fake_world):
        cli.main(["register", "--repo", "/repo", "--session", "s1", "--pane", "%1",
                  "--pid", "111", "--claim", "claude/install.sh", "--claim", "sessioncoord/**"])
        assert store.load_all("/repo")[0].claims == ("claude/install.sh", "sessioncoord/**")

    def test_registering_twice_updates_rather_than_duplicating(self, home, fake_world):
        for charter in ("first", "second"):
            cli.main(["register", "--repo", "/repo", "--session", "s1", "--pane", "%1",
                      "--pid", "111", "--charter", charter])
        assert len(store.load_all("/repo")) == 1


class TestConductor:
    def test_claiming_the_role_records_it(self, home, fake_world, capsys):
        register("a")
        assert cli.main(["conductor", "claim", "--repo", "/repo", "--session", "a"]) == 0
        assert store.load_all("/repo")[0].role == "conductor"

    def test_claiming_moves_the_role_off_whoever_held_it(self, home, fake_world, capsys):
        # Two conductors is worse than none: sessions would route to different arbiters.
        register("a", pane="%1", pid=111)
        register("b", pane="%2", pid=222, workdir="/repo/.worktrees/b")
        cli.main(["conductor", "claim", "--repo", "/repo", "--session", "a"])
        cli.main(["conductor", "claim", "--repo", "/repo", "--session", "b"])
        roles = {p.session_id: p.role for p in store.load_all("/repo")}
        assert roles == {"a": None, "b": "conductor"}

    def test_show_names_the_holder(self, home, fake_world, capsys):
        register("a", role="conductor", window="infra:11")
        cli.main(["conductor", "show", "--repo", "/repo"])
        assert "infra:11" in capsys.readouterr().out

    def test_show_says_plainly_when_nobody_holds_it(self, home, fake_world, capsys):
        register("a")
        cli.main(["conductor", "show", "--repo", "/repo"])
        assert "nobody" in capsys.readouterr().out.lower()


class TestAsks:
    def test_adding_and_listing(self, home, fake_world, capsys):
        rc = cli.main(["ask", "add", "--repo", "/repo", "--session", "s1", "--tag", "Q-ZNYN",
                       "--kind", "Q", "--text",
                       "Merge the PR that carries three sessions' accumulated work?"])
        assert rc == 0
        cli.main(["ask", "list", "--repo", "/repo"])
        assert "Q-ZNYN" in capsys.readouterr().out

    def test_a_bare_identifier_is_refused_with_a_usable_message(self, home, fake_world, capsys):
        rc = cli.main(["ask", "add", "--repo", "/repo", "--session", "s1", "--tag", "Q-0001",
                       "--kind", "Q", "--text", "Approve @idymqyp3"])
        assert rc == 2
        assert "stand on its own" in capsys.readouterr().err

    def test_answering_by_prefix(self, home, fake_world, capsys):
        cli.main(["ask", "add", "--repo", "/repo", "--session", "s1", "--tag", "Q-ZNYN",
                  "--kind", "Q", "--text", "Merge the PR carrying three sessions' work?"])
        assert cli.main(["ask", "answer", "--repo", "/repo", "--tag", "q-zn",
                         "--text", "go"]) == 0
        cli.main(["ask", "list", "--repo", "/repo"])
        assert "No open asks" in capsys.readouterr().out

    def test_an_empty_queue_prints_the_empty_line_not_a_header(self, home, fake_world, capsys):
        cli.main(["ask", "list", "--repo", "/repo"])
        assert capsys.readouterr().out.strip() == "No open asks."


class TestSweep:
    def test_reports_what_it_dropped(self, home, fake_world, capsys):
        register("ghost", pane="%9", pid=999)
        assert cli.main(["sweep", "--repo", "/repo"]) == 0
        assert "ghost" in capsys.readouterr().out
        assert store.load_all("/repo") == []


class TestLock:
    def test_runs_the_command_under_the_lock(self, home, fake_world, tmp_path):
        marker = tmp_path / "ran"
        assert cli.main(["lock", "--repo", "/repo", "merge", "--", "touch", str(marker)]) == 0
        assert marker.exists()

    def test_reports_a_busy_lock_with_a_retryable_exit_code(self, home, fake_world, capsys):
        # 75 is EX_TEMPFAIL: distinguishable from any exit code the wrapped command could
        # return, so pr-merge can tell "someone else is merging" from "the merge failed".
        from sessioncoord import locks
        with locks.hold("/repo", "merge", owner="infra:11 (merging #69)"):
            rc = cli.main(["lock", "--repo", "/repo", "merge", "--wait", "0", "--", "true"])
        assert rc == 75
        assert "infra:11" in capsys.readouterr().err

    def test_status_names_the_holder(self, home, fake_world, capsys):
        from sessioncoord import locks
        with locks.hold("/repo", "merge", owner="infra:11"):
            cli.main(["lock", "--repo", "/repo", "merge", "--status"])
        assert "infra:11" in capsys.readouterr().out

    def test_status_says_free_when_it_is(self, home, fake_world, capsys):
        cli.main(["lock", "--repo", "/repo", "merge", "--status"])
        assert "free" in capsys.readouterr().out


class TestUnknownRepo:
    def test_outside_a_git_repo_it_says_so_and_does_not_crash(self, home, monkeypatch, capsys):
        monkeypatch.setattr(cli.gitio, "repo_root", lambda p, run=None: None)
        assert cli.main(["board"]) == 0
        assert "not a git repo" in capsys.readouterr().err.lower()
