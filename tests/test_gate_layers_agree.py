"""The checker must be correct standalone, not only behind the bash prefilter.

A peer session (`me:7`) probed both layers separately and found they disagreed. Its table:

| case                               | deployed | checker alone |
|------------------------------------|----------|---------------|
| multi-line, quotes a git write      | DENY     | DENY          |
| multi-line, NO git anywhere         | allow    | **DENY**      |

The checker alone refused a multi-line command containing no git; the deployed pair allowed it,
because the shim short-circuits on payloads with no `git` in them. So the shim was load-bearing
for CORRECTNESS while documented as an optimisation, and two things followed that neither of us
had noticed: a test asserting the checker's behaviour proved nothing about the box, and anyone
widening the shim would have turned a latent refusal live across every multi-line Bash call.

Their suggested remedy was better than the one I was about to write — make `Unparseable` stop
denying things it never identified as a write — so that is what this pins.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[1] / "hooks" / "session-coord-gate-check"
_spec = importlib.util.spec_from_loader(
    "gate_la", importlib.machinery.SourceFileLoader("gate_la", str(HOOK)))
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)

REPO = "/home/user/code/me/tools"


class TestTheTwoLayersAgreeWithoutTheShim:
    @pytest.mark.parametrize("command", [
        "echo one\necho two",
        "ls -la\npwd",
        "python3 -c 'print(1)'\npython3 -c 'print(2)'",
    ])
    def test_a_multiline_command_with_no_git_is_never_a_write(self, command):
        assert not gate.analyse(command, REPO)[0], command

    def test_an_unparseable_command_with_no_git_is_not_a_write(self):
        # `Unparseable` used to mean "assume a write" unconditionally. But we never identified a
        # write in it, and there is no git in it to identify -- so denying was asserting
        # something we had no evidence for, in the direction that blocks work.
        assert not gate.analyse("echo 'unterminated\nmore text", REPO)[0]

    def test_but_an_unparseable_command_WITH_a_write_verb_still_is(self):
        # Conservative where it counts: unreadable AND it mentions staging.
        assert gate.analyse("git add -A 'unterminated", REPO)[0]

    def test_an_unparseable_write_asks_the_repo_wide_question(self):
        assert gate.analyse("git commit -s 'unterminated", REPO)[1] is None


class TestTheShimIsNowOnlyAnOptimisation:
    """Whatever the shim passes through, the checker decides correctly on its own."""

    @pytest.mark.parametrize("command,expected", [
        ("echo hello", False),
        ("echo one\necho two", False),
        ("git status", False),
        ("git add -A", True),
        ("echo x\ngit add -A", True),
        ("session-coord msg me:7 \"do not git add -A here\"", False),
    ])
    def test_the_checker_alone_gets_every_case_right(self, command, expected):
        assert gate.analyse(command, REPO)[0] is expected, command
