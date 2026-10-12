"""The eighth and last review round of PR #3.

The worst bug in eight rounds is here, and it is a data-loss one: `load_asks` returned `[]` for
the whole file when a single entry was malformed, and the next `ask add` then SAVED that empty
list over every real ask. A tolerant reader that loses data is worse than a strict one that
refuses — the refusal is visible and the loss is not, and the file it overwrites is the one
queue Daniel reads.

Two of the others are bugs I introduced in round seven while fixing real ones, which is the
argument for the round rather than against it: narrowing a guard and widening a verb list are
both edits that can go one step too far, and only a test says which.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
from pathlib import Path

import pytest
from sessioncoord import cli, store, tmuxio
from sessioncoord.model import Presence
from sessioncoord.tmuxio import AmbiguousTarget


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
    monkeypatch.setattr(cli.tmuxio, "server_pid", lambda run=None: "999")
    monkeypatch.setattr(cli.tmuxio, "window_of", lambda p, run=None: "me:1")


def rec(sid, **kw):
    return Presence(session_id=sid, repo_root="/repo", workdir=kw.pop("workdir", "/repo"),
                    pane=kw.pop("pane", "%1"), claude_pid=kw.pop("claude_pid", 111), **kw)


def _gate():
    hook = Path(__file__).resolve().parents[1] / "hooks" / "session-coord-gate-check"
    spec = importlib.util.spec_from_loader(
        "gate8", importlib.machinery.SourceFileLoader("gate8", str(hook)))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestTheAskQueueCannotBeErased:
    def test_one_malformed_entry_does_not_discard_the_valid_ones(self, home):
        store.state_dir("/repo").mkdir(parents=True, exist_ok=True)
        (store.state_dir("/repo") / "asks.json").write_text(json.dumps([
            {"tag": "Q-GOOD", "kind": "Q", "text": "A real question that must survive this.",
             "session_id": "s1"},
            {"tag": 42},                      # malformed
            "not even an object",
        ]))
        assert [a.tag for a in store.load_asks("/repo")] == ["Q-GOOD"]

    def test_and_a_subsequent_add_preserves_it(self, home, fake_world):
        # The actual data loss: read [] for the whole file, then write [] back over it.
        store.state_dir("/repo").mkdir(parents=True, exist_ok=True)
        (store.state_dir("/repo") / "asks.json").write_text(json.dumps([
            {"tag": "Q-GOOD", "kind": "Q", "text": "A real question that must survive this.",
             "session_id": "s1"},
            "not even an object",
        ]))
        cli.main(["ask", "add", "--repo", "/repo", "--session", "s2", "--tag", "Q-NEW",
                  "--kind", "Q", "--text", "A second question, also spelled out in full."])
        assert sorted(a.tag for a in store.load_asks("/repo")) == ["Q-GOOD", "Q-NEW"]


class TestPlumbingThatWritesTheIndex:
    @pytest.mark.parametrize("command", [
        "git update-index --add secrets.env",
        "git read-tree -m HEAD",
        "git checkout-index -a -f",
        "git sparse-checkout set src",
    ])
    def test_it_is_gated(self, command):
        # `git update-index --add` stages a peer's path with no `git add` in the command line.
        assert _gate().analyse(command, "/repo")[0], command

    @pytest.mark.parametrize("command", [
        "git worktree list",
        "git submodule status",
        "git gc --auto",
        "git update-ref -d refs/heads/gone",
    ])
    def test_and_the_reads_beside_it_are_not(self, command):
        # A first pass at this list denied `git worktree list`, caught by the existing suite
        # within seconds. The false-refusal failure is the one that ends with the gate switched
        # off, so the plumbing list stays narrow and the docstring says why.
        assert not _gate().analyse(command, "/repo")[0], command


class TestASplitWindowWithAShellIsNotAmbiguous:
    def test_one_claude_and_one_shell_names_one_session(self, monkeypatch):
        # A false refusal introduced in round seven while fixing a real one: the shell pane was
        # never an addressable target, so counting it made `msg <window>` refuse a valid one.
        monkeypatch.setattr(tmuxio, "_children", lambda: {})
        monkeypatch.setattr(tmuxio, "claude_under",
                            lambda pane_pid, kids: 999 if int(pane_pid) == 1 else None)
        listing = "%1\tme:1\t1\t/repo\n%2\tme:1\t2\t/repo"
        assert tmuxio.pane_of_window("me:1", lambda argv: listing) == "%1"

    def test_two_claudes_in_one_window_is_still_ambiguous(self, monkeypatch):
        monkeypatch.setattr(tmuxio, "_children", lambda: {})
        monkeypatch.setattr(tmuxio, "claude_under", lambda pane_pid, kids: 999)
        listing = "%1\tme:1\t1\t/repo\n%2\tme:1\t2\t/repo"
        with pytest.raises(AmbiguousTarget):
            tmuxio.pane_of_window("me:1", lambda argv: listing)


class TestAStaleServerRecordCannotReceiveAMessage:
    def test_msg_refuses_a_record_from_a_previous_tmux_server(self, home, fake_world,
                                                              monkeypatch, capsys):
        # expect_pid alone would pass: a restart reuses %N ids and pids get recycled, so the
        # message would go to whoever inherited the pane.
        store.save(rec("peer", pane="%9", claude_pid=4141161, window="me:9",
                       tmux_server="111"))
        monkeypatch.setattr(cli.tmuxio, "server_pid", lambda run=None: "222")
        assert cli.main(["msg", "--repo", "/repo", "me:9", "a message for a session"]) == 2
        assert "stale" in capsys.readouterr().err


class TestTheRepoMarkerOutlivesTheRace:
    def test_the_session_record_is_written_before_the_marker(self, home):
        # The other order let a concurrent save write the marker, be observed by a `remove`
        # that saw no JSON yet, have its marker deleted, and then write its record -- leaving a
        # live repo absent from known_repos() with nothing to put it back.
        store.save(rec("a"))
        assert store.known_repos() == ["/repo"]
