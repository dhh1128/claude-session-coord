"""End-to-end tests for `claude/hooks/session-coord-mail`, the UserPromptSubmit delivery path.

Copilot, PR #12: *"this is the only delivery path for turns that were already starting"* and it
had no automated coverage. I had argued the harness was more scaffolding than the fix was
worth. That was wrong, and cheaply so: the hook resolves its CLI from `__file__`, so standing a
fake tree up around a copy of it exercises the real resolution as a side effect.

The hook is run as a SUBPROCESS with real stdin and a real exit code, because every property
worth asserting here lives at that boundary -- the Claude hook JSON envelope, the `--repo-of`
argument it constructs, what it does with a nonzero exit, and its promise never to raise. A
test that imported it would assert none of those.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
HOOK = REPO / "hooks" / "session-coord-mail"


@pytest.fixture
def tree(tmp_path):
    """A fake plugin: `<root>/bin/session-coord` beside `<root>/hooks/session-coord-mail`.

    The hook computes its CLI as `parents[1] / "bin" / "session-coord"`, so the layout is the
    thing under test as much as the logic is.
    """
    hooks = tmp_path / "hooks"
    (tmp_path / "bin").mkdir()
    hooks.mkdir(parents=True)
    shutil.copy2(HOOK, hooks / "session-coord-mail")
    return tmp_path


# `inbox --ids` answers with the ids on a sentinel first line and the bodies after it, from a
# single lock-held snapshot. The fakes speak that protocol, because a fake that does not is a
# fake that cannot fail the way the real thing does -- which is how an earlier version of this
# suite passed while the hook was taking two inconsistent peeks.
IDS_PREFIX = "#ids "


def fake_cli(root: Path, *, body: str = "", ids: str = "m1", rc: int = 0,
             emit_ids: bool = True) -> Path:
    """A CLI that answers `--ids` with the sentinel line, then `body`."""
    cli = root / "bin" / "session-coord"
    head = (IDS_PREFIX + ids + "\n") if (emit_ids and body) else ""
    cli.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "if '--ids' in sys.argv:\n"
        f"    sys.stdout.write({head!r} + {body!r})\n"
        "else:\n"
        f"    sys.stdout.write({body!r})\n"
        f"sys.exit({rc})\n"
    )
    cli.chmod(0o755)
    return cli


def run(root: Path, payload) -> subprocess.CompletedProcess:
    hook = root / "hooks" / "session-coord-mail"
    return subprocess.run([sys.executable, str(hook)], input=payload, text=True,
                          capture_output=True, timeout=30)


def ok_payload(cwd: Path) -> str:
    return json.dumps({"session_id": "s1", "cwd": str(cwd)})


class TestSilence:
    def test_says_nothing_when_there_is_no_mail(self, tree):
        # Load-bearing: this runs on every prompt in every session, and a hook that speaks when
        # it has nothing to say is a hook that gets turned off.
        fake_cli(tree, body="")
        out = run(tree, ok_payload(tree))
        assert out.returncode == 0
        assert out.stdout == ""

    def test_says_nothing_when_the_cli_prints_only_whitespace(self, tree):
        fake_cli(tree, body="   \n\n")
        assert run(tree, ok_payload(tree)).stdout == ""


class TestDelivery:
    def test_wraps_mail_in_the_claude_hook_envelope(self, tree):
        fake_cli(tree, body="1 unread message(s)\n\n  from me:4\n  the gate is fixed\n")
        out = run(tree, ok_payload(tree))
        assert out.returncode == 0
        payload = json.loads(out.stdout)
        spec = payload["hookSpecificOutput"]
        assert spec["hookEventName"] == "UserPromptSubmit"
        assert "the gate is fixed" in spec["additionalContext"]

    def test_tells_the_reader_it_was_not_interrupted(self, tree):
        fake_cli(tree, body="mail\n")
        ctx = json.loads(run(tree, ok_payload(tree)).stdout)["hookSpecificOutput"][
            "additionalContext"]
        assert "nobody interrupted you" in ctx
        assert "[no reply needed]" in ctx

    def test_passes_the_prompt_cwd_as_repo_of(self, tree):
        # The hook runs with the prompt's cwd, which can be any subdirectory of a worktree, so
        # it must hand the CLI a path rather than relying on the process cwd.
        cli = tree / "bin" / "session-coord"
        # Echoes its own argv as the message body, behind the sentinel line, so the argv the
        # hook builds is what lands in the delivered context.
        cli.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            "if '--ids' in sys.argv:\n"
            "    sys.stdout.write('#ids m1' + chr(10) + ' '.join(sys.argv[1:]) + chr(10))\n"
        )
        cli.chmod(0o755)
        sub = tree / "deep" / "nested"
        sub.mkdir(parents=True)
        ctx = json.loads(run(tree, ok_payload(sub)).stdout)["hookSpecificOutput"][
            "additionalContext"]
        assert "--repo-of" in ctx and str(sub) in ctx
        assert "--session s1" in ctx
        assert "--quiet" in ctx


class TestAFailedDrainIsNotDelivered:
    def test_stdout_from_a_nonzero_exit_is_ignored(self, tree):
        # The nasty one: `cmd_inbox` renders BEFORE it persists read markers, so a save failure
        # leaves the mail unread with output already produced. Injecting it would repeat the
        # same messages into every subsequent prompt, forever -- the hook that exists to stop
        # peers interrupting each other becoming the thing that interrupts continuously.
        fake_cli(tree, body="1 unread message(s)\n  from me:4\n  important\n", rc=2)
        out = run(tree, ok_payload(tree))
        assert out.returncode == 0
        assert out.stdout == ""


class TestItNeverBreaksASession:
    def test_malformed_stdin_exits_clean(self, tree):
        fake_cli(tree, body="mail\n")
        out = run(tree, "not json at all")
        assert out.returncode == 0
        assert out.stdout == ""

    def test_a_payload_that_is_not_an_object_exits_clean(self, tree):
        fake_cli(tree, body="mail\n")
        assert run(tree, json.dumps([1, 2, 3])).stdout == ""

    def test_a_missing_session_id_exits_clean(self, tree):
        fake_cli(tree, body="mail\n")
        assert run(tree, json.dumps({"cwd": str(tree)})).stdout == ""

    def test_an_absent_cli_exits_clean(self, tree):
        # No `session-coord` beside it at all: the tool is simply not installed on this box,
        # and complaining on every prompt about that would be worse than silence.
        out = run(tree, ok_payload(tree))
        assert out.returncode == 0
        assert out.stdout == ""

    def test_a_cli_that_cannot_be_executed_exits_clean(self, tree):
        cli = fake_cli(tree, body="mail\n")
        cli.chmod(0o644)  # present but not executable
        out = run(tree, ok_payload(tree))
        assert out.returncode == 0
        assert out.stdout == ""

def recording_cli(root: Path, *, body: str = "mail\n", ids: str = "m1 m2",
                  rc: int = 0) -> Path:
    """A fake CLI that logs its argv, so the call sequence can be asserted."""
    cli = root / "bin" / "session-coord"
    log = root / "calls.log"
    cli.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        f"open({str(log)!r}, 'a').write(' '.join(sys.argv[1:]) + chr(10))\n"
        "if '--ids' in sys.argv:\n"
        f"    sys.stdout.write({(IDS_PREFIX + ids + chr(10))!r} + {body!r})\n"
        f"sys.exit({rc})\n"
    )
    cli.chmod(0o755)
    return cli


def calls(root: Path) -> list[str]:
    log = root / "calls.log"
    return log.read_text().splitlines() if log.exists() else []


class TestMailIsNotDrainedUntilItIsDelivered:
    """Copilot, PR #12: `inbox` marked mail read before the envelope was printed, so a crash
    in that gap lost it. The delivery guarantee inverted by its own bookkeeping -- the exact
    failure `cmd_inbox` documents guarding against one layer down."""

    def test_it_peeks_first_and_acknowledges_afterwards(self, tree):
        recording_cli(tree)
        out = run(tree, ok_payload(tree))
        assert json.loads(out.stdout)["hookSpecificOutput"]["additionalContext"]
        seen = calls(tree)
        # TWO calls, not three. Bodies and ids come from ONE snapshot: a second peek to learn
        # the ids could see a message the first did not, and acknowledge it undelivered.
        assert len(seen) == 2, seen
        assert "--peek" in seen[0] and "--ids" in seen[0], "one read, bodies and ids together"
        assert "--peek" not in seen[1] and "--ack" in seen[1], "acknowledge that snapshot"

    def test_it_acknowledges_by_id_rather_than_by_recipient(self, tree):
        # The race this closes: a peer filing a message between the read and the
        # acknowledgement. Clearing by recipient would mark the newcomer read although it was
        # never delivered; naming the ids cannot.
        recording_cli(tree)
        run(tree, ok_payload(tree))
        ack = calls(tree)[1]
        assert "--ack m1,m2" in ack, ack

    def test_nothing_is_drained_when_there_is_no_mail(self, tree):
        recording_cli(tree, body="")
        run(tree, ok_payload(tree))
        seen = calls(tree)
        assert len(seen) == 1 and "--peek" in seen[0]
        assert "--ack" not in seen[0]

    def test_nothing_is_drained_when_the_read_fails(self, tree):
        # A nonzero exit says nothing about what is waiting, so the mail stays put.
        recording_cli(tree, body="mail that was never delivered\n", rc=2)
        out = run(tree, ok_payload(tree))
        assert out.stdout == ""
        assert len(calls(tree)) == 1

    def test_a_drain_that_fails_still_leaves_the_mail_delivered(self, tree):
        # Delivered but not drained: it repeats next turn, which is the safe direction. A
        # repeat is visible and harmless; a silent loss is neither.
        cli = tree / "bin" / "session-coord"
        log = tree / "calls.log"
        cli.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            f"open({str(log)!r}, 'a').write(' '.join(sys.argv[1:]) + chr(10))\n"
            "if '--ids' in sys.argv:\n"
            "    sys.stdout.write('#ids m1\\nmail\\n'); sys.exit(0)\n"
            "sys.exit(9)\n"
        )
        cli.chmod(0o755)
        out = run(tree, ok_payload(tree))
        assert out.returncode == 0
        assert "mail" in json.loads(out.stdout)["hookSpecificOutput"]["additionalContext"]
        # one peek, then the failing acknowledgement.
        assert len(calls(tree)) == 2

    def test_a_cli_without_ids_support_delivers_nothing(self, tree):
        # An older `session-coord` that does not know `--ids` prints bodies with no sentinel.
        # Delivering them would mean acknowledging ids we do not have -- a repeat is
        # recoverable, acknowledging the wrong thing is not.
        fake_cli(tree, body="some mail\n", emit_ids=False)
        out = run(tree, ok_payload(tree))
        assert out.returncode == 0
        assert out.stdout == ""
