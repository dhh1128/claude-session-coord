"""Regressions for what the PR #3 review found.

Two independent reviewers — GitHub Copilot and a Codex second opinion — went over the first
version of this package and between them found twenty-two real defects. Several were fail-open:
the guard stopped guarding exactly when it mattered, while still reporting success. Each test
here pins one of those, named for the failure rather than for the function.
"""

from __future__ import annotations

import json

import pytest
from sessioncoord import cli, locks, store, tmuxio
from sessioncoord.model import Ask, Finding, Presence
from sessioncoord.tmuxio import SendRefused


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


def rec(sid, **kw):
    return Presence(session_id=sid, repo_root="/repo", workdir=kw.pop("workdir", "/repo"),
                    pane=kw.pop("pane", "%1"), claude_pid=kw.pop("claude_pid", 111), **kw)


class TestClearDoesNotDoubleCount:
    def test_a_new_session_id_in_the_same_pane_replaces_the_old_record(self, home, fake_world):
        # /clear mints a new session id while the SAME claude process stays in the SAME pane.
        # The old record is still genuinely live by the liveness predicate, so without this one
        # conversation shows up as two sessions in one working tree -- and the shared-tree
        # detector then reports a blocking collision between a session and itself.
        cli.main(["register", "--repo", "/repo", "--session", "old", "--pane", "%1",
                  "--pid", "111", "--charter", "Work in progress."])
        cli.main(["register", "--repo", "/repo", "--session", "new", "--pane", "%1",
                  "--pid", "111"])
        assert [p.session_id for p in store.load_all("/repo")] == ["new"]

    def test_the_charter_and_claims_survive_the_clear(self, home, fake_world):
        # Daniel cleared the context, not the assignment.
        cli.main(["register", "--repo", "/repo", "--session", "old", "--pane", "%1",
                  "--pid", "111", "--charter", "Cyber hardening.", "--claim", "docs/**"])
        cli.main(["register", "--repo", "/repo", "--session", "new", "--pane", "%1",
                  "--pid", "111"])
        p = store.load_all("/repo")[0]
        assert p.charter == "Cyber hardening." and p.claims == ("docs/**",)

    def test_a_different_pane_is_left_alone(self, home, fake_world):
        cli.main(["register", "--repo", "/repo", "--session", "a", "--pane", "%1", "--pid", "111"])
        cli.main(["register", "--repo", "/repo", "--session", "b", "--pane", "%2", "--pid", "222"])
        assert len(store.load_all("/repo")) == 2


class TestPidSurvivesAReregister:
    def test_updating_a_charter_without_pid_does_not_blank_it(self, home, fake_world):
        # SKILL.md's documented charter/claim update omits --pid, so this is the COMMON path.
        # Blanking the pid made the next sweep treat a running session as dead.
        cli.main(["register", "--repo", "/repo", "--session", "s1", "--pane", "%1", "--pid", "111"])
        cli.main(["register", "--repo", "/repo", "--session", "s1", "--charter", "Now with a job."])
        p = store.load_all("/repo")[0]
        assert p.claude_pid == 111 and p.charter == "Now with a job."

    def test_and_the_session_therefore_survives_the_next_sweep(self, home, fake_world):
        cli.main(["register", "--repo", "/repo", "--session", "s1", "--pane", "%1", "--pid", "111"])
        cli.main(["register", "--repo", "/repo", "--session", "s1", "--charter", "Still here."])
        store.sweep("/repo", {"%1": 111})
        assert len(store.load_all("/repo")) == 1


class TestTheGateIsScopedToTheCaller:
    def test_a_session_in_its_own_worktree_is_not_denied_for_someone_elses_collision(
            self, home, fake_world, capsys):
        # Two sessions share the main checkout; a third sits safely in its own worktree. The
        # repo-wide question refused the compliant session -- which is how a gate earns being
        # switched off.
        store.save(rec("a", workdir="/repo", pane="%1", claude_pid=111))
        store.save(rec("b", workdir="/repo", pane="%2", claude_pid=222))
        # The caller is genuinely somewhere else, so `gitio.workdir` answers with its own root
        # rather than the shared one the fixture returns for registration.
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(cli.gitio, "workdir",
                            lambda p, run=None: "/repo/.worktrees/mine")
        try:
            rc = cli.main(["check", "--repo", "/repo", "--workdir", "/repo/.worktrees/mine",
                           "--json"])
        finally:
            monkeypatch.undo()
        codes = [f["code"] for f in json.loads(capsys.readouterr().out)]
        # Nothing BLOCKING survives the filter, which is what the gate acts on. A repo-wide
        # note like `no-conductor` still reaches everybody, correctly -- it is about the repo,
        # not about anyone's tree.
        assert rc != 2 and "shared-tree" not in codes and "unworktreed" not in codes

    def test_a_session_actually_in_the_shared_tree_is_still_denied(self, home, fake_world,
                                                                   capsys):
        store.save(rec("a", workdir="/repo", pane="%1", claude_pid=111))
        store.save(rec("b", workdir="/repo", pane="%2", claude_pid=222))
        rc = cli.main(["check", "--repo", "/repo", "--workdir", "/repo", "--json"])
        assert rc == 2
        assert json.loads(capsys.readouterr().out)[0]["code"] == "shared-tree"

    def test_a_repo_wide_finding_still_reaches_everybody(self, home):
        # A finding with no workdirs is about the repo, not about one tree.
        f = Finding(code="stale-default", severity="note", summary="behind")
        assert f.workdirs == ()


class TestConcurrentQueueMutation:
    def test_an_ask_add_takes_the_queue_lock(self, home, fake_world, capsys):
        # Proven by holding the lock and watching the add refuse rather than race. Without the
        # lock the read-modify-write silently dropped whichever update landed second.
        with locks.hold("/repo", "asks", owner="a peer"):
            rc = cli.main(["ask", "add", "--repo", "/repo", "--session", "s1", "--tag", "Q-AAAA",
                           "--kind", "Q", "--wait", "0",
                           "--text", "A question long enough to stand on its own here."])
        assert rc == 75
        assert "a peer" in capsys.readouterr().err

    def test_listing_never_waits_on_a_writer(self, home, fake_world, capsys):
        with locks.hold("/repo", "asks", owner="a peer"):
            assert cli.main(["ask", "list", "--repo", "/repo"]) == 0
        assert "No open asks" in capsys.readouterr().out

    def test_claiming_the_conductor_takes_the_presence_lock(self, home, fake_world, capsys):
        store.save(rec("a"))
        with locks.hold("/repo", "presence", owner="a peer"):
            rc = cli.main(["conductor", "claim", "--repo", "/repo", "--session", "a",
                           "--wait", "0"])
        assert rc == 75


class TestAskArgumentValidation:
    @pytest.mark.parametrize("argv,need", [
        (["ask", "answer", "--repo", "/repo"], "--tag"),
        (["ask", "dissolve", "--repo", "/repo"], "--tag"),
        (["ask", "add", "--repo", "/repo", "--kind", "Q", "--text", "x" * 60], "--tag"),
        (["ask", "add", "--repo", "/repo", "--tag", "Q-AAAA", "--kind", "Q"], "--text"),
    ])
    def test_a_missing_option_is_a_usage_error_not_a_traceback(self, home, fake_world, capsys,
                                                               argv, need):
        assert cli.main(argv) == 2
        assert need in capsys.readouterr().err


class TestSendIsHostileInput:
    @pytest.fixture
    def tmux(self, monkeypatch):
        monkeypatch.setattr(tmuxio, "claude_under",
                            lambda pane_pid, kids: 4141161 if int(pane_pid) == 1 else None)
        monkeypatch.setattr(tmuxio, "_children", lambda: {})

        class T:
            def __init__(self):
                self.sent = []

            def __call__(self, argv):
                if argv[:2] == ["tmux", "list-panes"]:
                    return "%1\tme:1\t1\t/repo\n%2\tme:2\t2\t/repo"
                if argv[:2] == ["tmux", "send-keys"]:
                    self.sent.append(argv)
                return ""
        return T()

    def test_the_message_is_typed_literally(self, tmux):
        # Without -l, tmux parses KEY NAMES. A sentence containing the word Enter would submit
        # the recipient's half-written prompt; one containing C-c would interrupt its work.
        tmuxio.send("me:1", "press Enter when the C-c finishes", tmux)
        assert "-l" in tmux.sent[0]

    def test_a_carriage_return_is_refused_rather_than_stripped(self, tmux):
        # A CR submits early, turning one message into a truncated instruction plus a stray
        # second prompt. Silently altering a message would be worse than declining to send it.
        with pytest.raises(SendRefused, match="control characters"):
            tmuxio.send("me:1", "do the first thing\rand then the second", tmux)
        assert tmux.sent == []

    def test_an_escape_sequence_is_refused(self, tmux):
        with pytest.raises(SendRefused, match="control characters"):
            tmuxio.send("me:1", "hello \x1b[2J there", tmux)
        assert tmux.sent == []

    def test_an_enormous_message_spills_to_a_file_rather_than_being_refused(self, tmux):
        # CHANGED 2026-09-18. This used to refuse at 4000 chars and tell the caller to write a
        # file; the tool now writes the file itself and types a pointer, which is what the
        # advice was asking the caller to do by hand. The cap still guards the TYPED text.
        tmuxio.send("me:1", "x" * 5000, tmux)
        assert "see /" in tmux.sent[0][-1] and len(tmux.sent[0][-1]) < 120

    def test_it_refuses_when_the_pane_holds_the_wrong_claude(self, tmux):
        # Pane ids are reused. A caller holding a presence record addresses the SESSION.
        with pytest.raises(SendRefused, match="not the 999"):
            tmuxio.send("me:1", "a message for a session that has gone", tmux, expect_pid=999)

    def test_it_reports_failure_instead_of_claiming_it_sent(self, monkeypatch):
        monkeypatch.setattr(tmuxio, "claude_under", lambda pane_pid, kids: 4141161)
        monkeypatch.setattr(tmuxio, "_children", lambda: {})

        def run(argv):
            if argv[:2] == ["tmux", "list-panes"]:
                return "%1\tme:1\t1\t/repo"
            return None  # the send fails

        with pytest.raises(SendRefused, match="refused to type"):
            tmuxio.send("me:1", "a perfectly ordinary message", run)

    def test_a_failed_submit_says_the_message_is_sitting_unsent(self, monkeypatch):
        # The worst outcome to report wrongly: the text is in their prompt but not submitted,
        # so a retry delivers it twice.
        monkeypatch.setattr(tmuxio, "claude_under", lambda pane_pid, kids: 4141161)
        monkeypatch.setattr(tmuxio, "_children", lambda: {})
        calls = []

        def run(argv):
            if argv[:2] == ["tmux", "list-panes"]:
                return "%1\tme:1\t1\t/repo"
            calls.append(argv)
            return "" if "-l" in argv else None

        with pytest.raises(SendRefused, match="sitting"):
            tmuxio.send("me:1", "a perfectly ordinary message", run)


class TestDeregistration:
    def test_a_session_can_remove_its_own_record(self, home, fake_world, capsys):
        # SessionEnd cannot rely on a sweep: the hook is a child of the still-running claude,
        # so tmux reports its pane/pid pair as live and the sweep correctly keeps the record.
        store.save(rec("going"))
        assert cli.main(["deregister", "--repo", "/repo", "--session", "going"]) == 0
        assert store.load_all("/repo") == []

    def test_the_last_departure_leaves_no_phantom_repo_behind(self, home):
        # known_repos() promised repos WITH a registrant; the marker outlived the records, so
        # an estate-wide scan accumulated repos nobody was in.
        store.save(rec("only"))
        store.remove("/repo", "only")
        assert store.known_repos() == []

    def test_a_repo_with_remaining_sessions_stays_listed(self, home):
        store.save(rec("a"))
        store.save(rec("b"))
        store.remove("/repo", "a")
        assert store.known_repos() == ["/repo"]


class TestUnsafeIdentifiers:
    def test_a_session_id_cannot_escape_its_directory(self, home):
        with pytest.raises(store.UnsafeName):
            store.save(rec("../../elsewhere"))

    def test_a_lock_name_cannot_escape_its_directory(self, home):
        with pytest.raises(store.UnsafeName):
            with locks.hold("/repo", "../other"):
                pass

    def test_the_cli_reports_it_rather_than_crashing(self, home, fake_world, capsys):
        assert cli.main(["register", "--repo", "/repo", "--session", "../x", "--pane", "%1",
                         "--pid", "111"]) == 2
        assert "not usable as a filename" in capsys.readouterr().err


class TestLockOwnerAttribution:
    def test_an_unlabelled_holder_does_not_inherit_the_previous_name(self, home):
        # A stale name is worse than no name: a waiter goes and asks a session that has
        # already finished, and a name gets acted on.
        with locks.hold("/repo", "merge", owner="me:5 merging #3"):
            pass
        with locks.hold("/repo", "merge"):
            who = locks.holder("/repo", "merge") or ""
        assert "me:5" not in who


class TestConcurrentWritersDoNotShareATempFile:
    def test_each_writer_uses_its_own_temp_name(self, home):
        # A shared <name>.tmp let two writers interleave: A writes, B truncates and rewrites,
        # A renames B's bytes into place, B's rename then fails on a file that is gone.
        store.save(rec("s1"))
        leftovers = list((store.state_dir("/repo") / "sessions").glob("*.tmp"))
        assert leftovers == []
