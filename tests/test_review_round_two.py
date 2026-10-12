"""The second review round, which found the first round's fixes incomplete.

The instructive one: closing the "unknown is not empty" hole in the tmux path left the same
hole open in the process-tree path. `_children()` returned `{}` when `/proc` could not be read,
`panes()` treated that as a successful observation in which no pane holds a Claude, and
`sweep()` deleted the whole board — the exact blocker from round one, through a different door.
A fix that names one source rather than one *property* leaves the property broken everywhere
else it is reachable.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
from pathlib import Path

import pytest
from sessioncoord import cli, store, tmuxio
from sessioncoord.model import Presence
from sessioncoord.tmuxio import SendRefused


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_COORD_HOME", str(tmp_path / "state"))
    return tmp_path


REPO = "/home/user/code/acme/infra"


def rec(sid="s1", pane="%1", pid=100, workdir=REPO):
    return Presence(session_id=sid, repo_root=REPO, workdir=workdir, pane=pane, claude_pid=pid)


class TestProcFailureIsAlsoUnknown:
    def test_an_unreadable_proc_yields_unknown_not_an_empty_board(self, monkeypatch):
        monkeypatch.setattr(tmuxio, "_children", lambda: None)
        assert tmuxio.claude_pids(lambda argv: "%1\tme:1\t1\t/repo") is None

    def test_and_therefore_sweep_deletes_nothing(self, home, monkeypatch):
        monkeypatch.setattr(tmuxio, "_children", lambda: None)
        store.save(rec("a"))
        store.sweep(REPO, tmuxio.claude_pids(lambda argv: "%1\tme:1\t1\t/repo"))
        assert len(store.load_all(REPO)) == 1


class TestEveryPaneNotEveryWindow:
    def test_discovery_asks_for_panes(self, monkeypatch):
        # `list-windows -a` emits one record per WINDOW -- its active pane -- so a session in a
        # split window's other pane is invisible, its record gets swept while it is running,
        # and the gate reports no peers. `tmux-claude` already used `list-panes -a`.
        seen = []

        def run(argv):
            seen.append(argv)
            return ""

        monkeypatch.setattr(tmuxio, "_children", lambda: {})
        tmuxio.panes(run)
        assert seen[0][:3] == ["tmux", "list-panes", "-a"]

    def test_a_second_pane_in_one_window_is_discovered(self, monkeypatch):
        monkeypatch.setattr(tmuxio, "_children", lambda: {})
        monkeypatch.setattr(tmuxio, "claude_under",
                            lambda pane_pid, kids: 999 if int(pane_pid) == 2 else None)
        listing = "%1\tme:1\t1\t/repo\n%2\tme:1\t2\t/repo"   # same window, two panes
        assert tmuxio.claude_pids(lambda argv: listing) == {"%1": None, "%2": 999}


class TestSendUsesTheLateResolvedPane:
    def test_the_recheck_result_is_what_gets_written_to(self, monkeypatch):
        # Discarding it was worse than not re-checking: the check validated pane B while the
        # keystrokes still went to the stale pane A.
        monkeypatch.setattr(tmuxio, "_children", lambda: {})
        monkeypatch.setattr(tmuxio, "claude_under", lambda pane_pid, kids: 4141161)
        # One `resolve()` consults tmux twice: once directly, once through `pane_of_window`.
        # So the window moves panes after the FIRST resolve has finished, which is exactly the
        # interleaving the discarded re-check failed to notice.
        sent = []
        calls = {"n": 0}

        def run(argv):
            if argv[:2] == ["tmux", "list-panes"]:
                calls["n"] += 1
                return ("%1\tme:1\t1\t/repo" if calls["n"] <= 2
                        else "%2\tme:1\t2\t/repo")
            if argv[:2] == ["tmux", "capture-pane"]:
                return "\u276f  "          # an idle prompt; the busy check must not interfere
            sent.append(argv)
            return ""

        assert tmuxio.send("me:1", "a message", run) == "%2"
        assert sent[0][3] == "%2"


class TestWorkdirIsNormalised:
    def test_a_subdirectory_still_matches_its_worktree_root(self, home, monkeypatch, capsys):
        # Registration stores the worktree ROOT; a hook's cwd can be any subdirectory. Comparing
        # raw paths found no match, dropped the shared-tree blocker, and allowed the staging
        # command -- fail-open through a path-shape mismatch.
        monkeypatch.setattr(cli.gitio, "repo_root", lambda p, run=None: REPO)
        monkeypatch.setattr(cli.gitio, "facts", lambda r, run=None: None)
        monkeypatch.setattr(cli.tmuxio, "claude_pids", lambda run=None: {"%1": 1, "%2": 2})
        monkeypatch.setattr(cli.gitio, "workdir", lambda p, run=None: REPO)
        store.save(rec("a", "%1", 1, workdir=REPO))
        store.save(rec("b", "%2", 2, workdir=REPO))
        assert cli.main(["check", "--repo", REPO, "--workdir", f"{REPO}/docs/cyber",
                         "--json"]) == 2


class TestRemoveValidatesToo:
    def test_a_hostile_id_cannot_unlink_outside_the_sessions_directory(self, home):
        # `save` validated and `remove` did not, which is the more dangerous half: one writes,
        # the other deletes.
        with pytest.raises(store.UnsafeName):
            store.remove(REPO, "../../../etc/passwd")

    def test_the_cli_turns_that_into_an_error_not_a_traceback(self, home, monkeypatch, capsys):
        monkeypatch.setattr(cli.gitio, "repo_root", lambda p, run=None: REPO)
        assert cli.main(["deregister", "--repo", REPO, "--session", "../x"]) == 2
        assert "not usable as a filename" in capsys.readouterr().err

    def test_a_sweep_steps_over_a_hostile_record_rather_than_dying(self, home):
        store.save(rec("good", "%1", 1))
        bad = store.state_dir(REPO) / "sessions" / "bad.json"
        bad.write_text(Presence(session_id="../../evil", repo_root=REPO,
                                workdir=REPO, pane="%9", claude_pid=9).to_json())
        store.sweep(REPO, {"%1": 1})  # must not raise
        assert any(p.session_id == "good" for p in store.load_all(REPO))


HOOK = Path(__file__).resolve().parents[1] / "hooks" / "session-coord-gate-check"


def _gate():
    spec = importlib.util.spec_from_loader(
        "gate_check2", importlib.machinery.SourceFileLoader("gate_check2", str(HOOK)))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gate = _gate()


@pytest.mark.parametrize("command", [
    "sudo git add -A",
    "env git commit -s -m x",
    "command git stash",
    "nice -n 19 git rebase origin/main",
    "nice -n 19 ionice -c 3 git add -A",
    "env GIT_AUTHOR_NAME=x git commit -s",
])
def test_a_wrapped_git_write_is_still_a_git_write(command):
    # CLAUDE.core.md asks for `nice -n 19` on shell work by name, so wrapped invocations are
    # the house style here, not an evasion. Skipping them left the gate bypassable by a prefix
    # nobody was hiding behind.
    assert gate.analyse(command, "/repo")[0], command


@pytest.mark.parametrize("command", [
    "sudo systemctl restart nginx",
    "echo sudo git add",
    "nice -n 19 pytest tests -q",
])
def test_and_the_wrapper_allowance_does_not_swallow_everything(command):
    assert not gate.analyse(command, "/repo")[0], command
