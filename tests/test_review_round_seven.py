"""The seventh and final review round of PR #3.

The findings here are narrower than the earlier ones and three of them are the same class in
new places — a function promising tolerance and then raising on valid JSON that is not the
shape it wanted. Worth pinning anyway: `load_asks` and the transcript reader both sit under
commands that run unattended, where an uncaught `AttributeError` is a silent stop rather than
a visible one.

The one with teeth is the ambiguous window label. A tmux window can be split, discovery
deliberately finds Claude in every pane of it, and `pane_of_window` returned the first match —
so `msg me:1` could type into whichever pane happened to sort first. Addressing the wrong
session silently is the worst thing this package can do.
"""

from __future__ import annotations

import json

import pytest
from sessioncoord import cli, harvest, locks, store, tmuxio
from sessioncoord.model import Presence
from sessioncoord.tmuxio import AmbiguousTarget, SendRefused


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


class TestASplitWindowDoesNotNameASession:
    @pytest.fixture
    def split(self, monkeypatch):
        monkeypatch.setattr(tmuxio, "_children", lambda: {})
        monkeypatch.setattr(tmuxio, "claude_under", lambda pane_pid, kids: 4141161)
        # One window, two panes, a Claude in each -- which discovery deliberately supports.
        return lambda argv: "%1\tme:1\t1\t/repo\n%2\tme:1\t2\t/repo"

    def test_resolving_an_ambiguous_label_refuses_rather_than_picking(self, split):
        with pytest.raises(AmbiguousTarget, match="2 panes"):
            tmuxio.pane_of_window("me:1", split)

    def test_and_send_refuses_with_it(self, split):
        with pytest.raises(SendRefused, match="does not name one session"):
            tmuxio.send("me:1", "a message that must not go to the wrong pane", split)

    def test_an_unambiguous_label_still_resolves(self, monkeypatch):
        monkeypatch.setattr(tmuxio, "_children", lambda: {})
        monkeypatch.setattr(tmuxio, "claude_under", lambda pane_pid, kids: 4141161)
        assert tmuxio.pane_of_window("me:1", lambda argv: "%1\tme:1\t1\t/repo") == "%1"


class TestTheBoardSweepsUnderTheLock:
    def test_a_board_read_does_not_mutate_while_a_claim_is_mid_flight(self, home, fake_world):
        # `check`/`board` sweep, and a sweep is a mutation: it could delete a record in the
        # middle of `conductor claim`'s read-modify-save and have it written straight back.
        store.save(rec("ghost", pane="%9", claude_pid=999))
        with locks.hold("/repo", "presence", owner="a claim in progress"):
            cli.main(["board", "--repo", "/repo", "--quiet"])
        assert [p.session_id for p in store.load_all("/repo")] == ["ghost"]

    def test_but_it_still_answers_rather_than_failing(self, home, fake_world, capsys):
        # Read-only on contention. A board one sweep stale is much better than no board, since
        # every caller here is trying to find out who else is present.
        store.save(rec("a", pane="%1", claude_pid=111))
        store.save(rec("b", pane="%2", claude_pid=222, workdir="/repo/.worktrees/b"))
        with locks.hold("/repo", "presence", owner="a claim in progress"):
            assert cli.main(["board", "--repo", "/repo"]) == 0
        assert "2 sessions" in capsys.readouterr().out


class TestQuietMeansBlockingOnly:
    def test_warnings_beside_a_block_are_not_printed(self, home, fake_world, capsys):
        # The gate is the caller that relies on --quiet being terse, and printing a block's
        # accompanying warnings broke the option's own documented contract.
        store.save(rec("a", pane="%1", claude_pid=111, claims=("x/**",)))
        store.save(rec("b", pane="%2", claude_pid=222, claims=("x/**",)))
        cli.main(["check", "--repo", "/repo", "--quiet"])
        out = capsys.readouterr().out
        assert "shared-tree" in out and "claim-overlap" not in out

    def test_the_exit_code_still_reflects_everything(self, home, fake_world, capsys):
        store.save(rec("a", pane="%1", claude_pid=111))
        store.save(rec("b", pane="%2", claude_pid=222, workdir="/repo/.worktrees/b"))
        assert cli.main(["check", "--repo", "/repo", "--quiet"]) == 1


class TestTheDocumentedHarvestCommandWorks:
    def test_it_finds_the_session_from_the_environment(self, home, fake_world, monkeypatch,
                                                       tmp_path, capsys):
        # SKILL.md documents `session-coord harvest --quiet` with no --session, so this was the
        # path the documentation told people to take and it reported "no transcript". Same
        # class as the --pid bug two rounds ago.
        t = tmp_path / "transcript.jsonl"
        t.write_text(json.dumps({
            "type": "assistant",
            "message": {"content": [{"type": "text",
                                     "text": "  [ P-AAAA ]  A finding nobody answered here."}]},
        }) + "\n")
        store.save(rec("s1", transcript=str(t)))
        monkeypatch.setenv("CLAUDE_SESSION_ID", "s1")
        cli.main(["harvest", "--repo", "/repo"])
        assert "P-AAAA" in capsys.readouterr().out


class TestTolerantReadersAreActuallyTolerant:
    @pytest.mark.parametrize("body", ['"a string"', "42", "null"])
    def test_an_unreadable_ask_queue_raises_rather_than_reading_as_empty(self, home, body):
        # REVERSED in round nine, and the reversal is the point. This test used to assert that
        # a malformed queue "degrades instead of raising" -- which is precisely what caused the
        # data loss: `cmd_ask` took the [] as the current queue and saved it back over the file
        # it had failed to read. A reader that cannot distinguish "nothing here" from "I could
        # not read this" hands its caller a licence to destroy what it failed to read.
        #
        # Item-level tolerance is still right and still tested below; container-level tolerance
        # was never tolerance, it was silent truncation.
        (store.state_dir("/repo")).mkdir(parents=True, exist_ok=True)
        (store.state_dir("/repo") / "asks.json").write_text(body)
        with pytest.raises(store.UnreadableQueue):
            store.load_asks("/repo")

    @pytest.mark.parametrize("body", ["[1, 2]", '[{"tag": 1}]'])
    def test_but_a_bad_ITEM_inside_a_good_list_is_still_skipped(self, home, body):
        (store.state_dir("/repo")).mkdir(parents=True, exist_ok=True)
        (store.state_dir("/repo") / "asks.json").write_text(body)
        assert store.load_asks("/repo") == []

    def test_and_a_mutation_refuses_rather_than_overwriting(self, home, fake_world, capsys):
        (store.state_dir("/repo")).mkdir(parents=True, exist_ok=True)
        (store.state_dir("/repo") / "asks.json").write_text("not json at all")
        rc = cli.main(["ask", "add", "--repo", "/repo", "--session", "s1", "--tag", "Q-AAAA",
                       "--kind", "Q", "--text", "A question that must not destroy the file."])
        assert rc == 2
        assert "Refusing to mutate" in capsys.readouterr().err
        assert (store.state_dir("/repo") / "asks.json").read_text() == "not json at all"

    @pytest.mark.parametrize("line", ['"a string"', "42", "[1,2]", "null"])
    def test_a_transcript_line_that_is_not_a_record_is_skipped(self, tmp_path, line):
        # This runs at SessionEnd, where an uncaught exception is a silent stop: nobody is
        # watching the pane by then, so the loose ends simply never get reported.
        t = tmp_path / "t.jsonl"
        t.write_text(line + "\n" + json.dumps({
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "hello"}]},
        }) + "\n")
        assert [turn.text for turn in harvest.load_transcript(str(t))] == ["hello"]

    def test_a_record_whose_message_is_not_an_object_is_skipped(self, tmp_path):
        t = tmp_path / "t.jsonl"
        t.write_text(json.dumps({"type": "user", "message": "not an object"}) + "\n")
        assert harvest.load_transcript(str(t)) == []


class TestMultilineCommands:
    """shlex treats a newline as whitespace, so a multi-line call lexed into ONE argv.

    `echo starting\ngit add -A` became a single command beginning with `echo`, the write was
    never examined, and the gate permitted it. Multi-line Bash is the house style here rather
    than an edge case, so this was a wide-open door rather than a corner.
    """

    @pytest.mark.parametrize("command", [
        "echo starting\ngit add -A",
        "ls -la\ngit commit -s -m x",
        "set -e\ncd /tmp\ngit clean -fd",
        "python3 -c 'print(1)'\n\ngit stash",
    ])
    def test_a_write_on_a_later_line_is_found(self, command):
        import importlib.machinery
        import importlib.util
        from pathlib import Path
        hook = Path(__file__).resolve().parents[1] / "hooks" / "session-coord-gate-check"
        spec = importlib.util.spec_from_loader(
            "gate7", importlib.machinery.SourceFileLoader("gate7", str(hook)))
        gate = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gate)
        assert gate.analyse(command, "/repo")[0], command

    def test_a_multiline_command_with_no_write_is_still_clean(self):
        import importlib.machinery
        import importlib.util
        from pathlib import Path
        hook = Path(__file__).resolve().parents[1] / "hooks" / "session-coord-gate-check"
        spec = importlib.util.spec_from_loader(
            "gate7b", importlib.machinery.SourceFileLoader("gate7b", str(hook)))
        gate = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gate)
        assert not gate.analyse("echo one\necho two\ngit status", "/repo")[0]
