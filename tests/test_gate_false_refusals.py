"""The gate must not refuse work that is not a write. Found in live use, not in review.

Round seven split every command on every newline and accepted the consequence: a quoted
multi-line string breaks, `Unparseable` is raised, and the caller assumes a write. I wrote that
down as "the right trade". It was not, and the way it surfaced is the point.

`me:7` tried to send `session-coord msg` warning me about the shared tree, and the gate refused
it — because the MESSAGE spanned two lines. The tool's own inter-session channel, blocked by the
tool's own gate, on a command that touches no file. Minutes later it refused my own `<<'PYEOF'`
heredoc for containing `git add -A` as example text, while I sat in a worktree, on the strength
of a collision in a tree I was not in.

Two lessons, both encoded below. A multi-line message containing no git at all was also refused,
which is the tell that the trade was never about git — it was about newlines. And a false
refusal is not the mild failure: it is the one that ends with the gate switched off, after
which every real write goes through.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[1] / "hooks" / "session-coord-gate-check"
_spec = importlib.util.spec_from_loader(
    "gate_fr", importlib.machinery.SourceFileLoader("gate_fr", str(HOOK)))
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)

REPO = "/home/user/code/me/tools"


class TestItDoesNotRefuseTalking:
    @pytest.mark.parametrize("command", [
        'session-coord msg me:5 "do not git add -A in a shared tree"',
        'session-coord msg me:5 "run git status first, never git add -A or git commit"',
        'session-coord msg me:5 "line one mentions git add -A\nline two continues"',
        'session-coord msg me:5 "line one\nline two"',
    ])
    def test_a_message_is_never_a_write(self, command):
        # The observed failure: a peer could not warn me about the shared tree, because the
        # warning had to name the commands the gate matches on.
        assert not gate.analyse(command, REPO)[0], command


class TestItDoesNotRefuseQuotedText:
    @pytest.mark.parametrize("command", [
        "echo 'git add -A'",
        'printf "%s\\n" "git commit -s"',
        "cat <<'EOF'\ngit add -A\nEOF",
        "cat <<EOF\ngit commit -s -m x\nEOF",
        "cat <<-'PYEOF'\ngit clean -fd\nPYEOF",
        'python3 - <<"PY"\nprint("git add -A")\nPY',
    ])
    def test_a_heredoc_body_is_data_not_commands(self, command):
        # The author's own edit was refused by this. Writing a patch script whose text mentions
        # git is not staging anything, and heredocs are how patch scripts get written here.
        assert not gate.analyse(command, REPO)[0], command


class TestItStillCatchesRealMultilineWrites:
    """The reason line-splitting existed at all. Fixing the false refusals must not undo it."""

    @pytest.mark.parametrize("command", [
        "echo starting\ngit add -A",
        "ls -la\ngit commit -s -m x",
        "set -e\ncd /tmp\ngit clean -fd",
        "cat <<'EOF' > /tmp/x\nsome text\nEOF\ngit add -A",
    ])
    def test_a_write_on_a_later_line_is_still_found(self, command):
        assert gate.analyse(command, REPO)[0], command

    def test_a_write_after_a_heredoc_body_is_found(self):
        # The heredoc body is skipped; the command AFTER the delimiter is not.
        assert gate.analyse("cat <<EOF\nnot a command\nEOF\ngit commit -s", REPO)[0]


class TestLogicalLines:
    def test_a_quoted_newline_does_not_split(self):
        assert gate._logical_lines('msg "a\nb"') == ['msg "a\nb"']

    def test_a_bare_newline_does_split(self):
        assert gate._logical_lines("a\nb") == ["a", "b"]

    def test_a_heredoc_body_is_dropped_and_the_tail_kept(self):
        got = gate._logical_lines("cat <<EOF\nbody line\nEOF\nafter")
        assert "body line" not in got and "after" in got

    def test_an_escaped_newline_does_not_split(self):
        assert len(gate._logical_lines("git \\\nstatus")) == 1


class TestTheHeredocTerminatorIsMatchedTheWayTheShellMatchesIt:
    """`.strip()` ended the body early, and the rest of the body was then read as commands.

    That is the same false-refusal failure the surrounding module exists to fix, reintroduced
    inside the fix: an indented ` EOF ` in a body is ordinary prose -- it is what a document
    about heredocs looks like -- and the shell does not treat it as the terminator.
    """

    def test_an_indented_delimiter_does_not_end_a_plain_heredoc(self):
        command = "cat <<EOF\n  EOF\ngit add -A\nEOF"
        assert not gate.analyse(command, REPO)[0]
        assert "git add -A" not in gate._logical_lines(command)

    def test_but_it_does_end_a_dash_heredoc_when_the_indent_is_tabs(self):
        # `<<-` strips leading TABS, and only tabs. The delimiter genuinely terminates here, so
        # what follows is a real command and must still be caught.
        assert gate.analyse("cat <<-EOF\nbody\n\tEOF\ngit add -A", REPO)[0]

    def test_and_spaces_still_do_not_end_a_dash_heredoc(self):
        assert not gate.analyse("cat <<-EOF\n  EOF\ngit add -A\nEOF", REPO)[0]
