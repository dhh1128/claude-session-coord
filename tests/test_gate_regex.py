"""What the PreToolUse gate treats as a git write.

Codex's review of PR #3 found the original substring match wrong in both directions: it missed
`/usr/bin/git add -A` and `git -C /repo add -A`, and it denied `printf 'git add -A'` and any
command that merely quoted the phrase. Both directions matter and for different reasons — a
miss removes the guard, a false positive blocks legitimate work, and a gate that blocks
legitimate work gets switched off, which removes the guard too.

The hook is a script rather than a module, so it is loaded by path.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[1] / "hooks" / "session-coord-gate-check"


def _load():
    spec = importlib.util.spec_from_loader(
        "gate_check", importlib.machinery.SourceFileLoader("gate_check", str(HOOK)))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gate = _load()


@pytest.mark.parametrize("command", [
    "git add -A",
    "git commit -s -m 'x'",
    "git add -A && git commit -s -m 'x'",
    "/usr/bin/git add -A",                       # absolute path — missed by the old matcher
    "git -C /home/user/code/acme/infra add -A",   # -C — also missed
    "git --work-tree=/repo add .",
    "cd /repo; git commit --amend",
    "make build || git checkout -- .",
    "git\tstash",
    "git rebase origin/main",
    "git switch main",
])
def test_these_are_git_writes(command):
    assert gate.analyse(command, "/repo")[0], command


@pytest.mark.parametrize("command", [
    "printf '%s\\n' 'git add -A'",               # quoting one is not running one
    "echo git commit",
    "grep -n 'git add' /tmp/notes.md",
    "rg 'git commit' docs/",
    "git status",                                # reads are not writes
    "git log --oneline -5",
    "git diff origin/main",
    "git worktree list",
    "gitk --all",                                # not git
    "legitimate add commit merge words in prose",
    "python3 -c \"print('git add')\"",
])
def test_these_are_not(command):
    assert not gate.analyse(command, "/repo")[0], command


class TestFailsOpenOnJunk:
    @pytest.mark.parametrize("payload", [
        [],                                      # a list, not a dict
        {"tool_input": "a string"},              # tool_input of the wrong type
        {"tool_input": {}},                      # no command
        {"tool_input": {"command": None}},       # command of the wrong type
        {},
    ])
    def test_a_malformed_payload_is_ignored_rather_than_fatal(self, payload, monkeypatch,
                                                              capsys):
        # This runs on every git-ish Bash call in every session. A traceback here degrades the
        # whole session, and a gate that breaks work is worse than no gate at all.
        import io
        import json as _json
        import sys as _sys
        monkeypatch.setattr(_sys, "stdin", io.StringIO(_json.dumps(payload)))
        assert gate.main() == 0
        assert capsys.readouterr().out == ""

    def test_unparseable_stdin_is_ignored(self, monkeypatch, capsys):
        import io
        import sys as _sys
        monkeypatch.setattr(_sys, "stdin", io.StringIO("{not json"))
        assert gate.main() == 0
