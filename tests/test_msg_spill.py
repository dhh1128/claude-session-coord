"""Long messages go to a file; only a short sigil-marked pointer is typed.

Daniel's design, 2026-09-18, after watching a peer's message merge into his half-typed sentence.
His reasoning: keep the keystrokes short so a collision costs a line rather than a paragraph,
and mark the seam with a sigil so a reader can see the context switched mid-line.

Worth stating what this does NOT do, because it is the tempting conclusion: it does not fix the
co-mingling. A short message appends to an unfinished sentence exactly as a long one does, and
the Enter still submits it. The busy-prompt refusal in `test_msg_comingling.py` is what prevents
that; this bounds the damage when that check cannot see, and it makes inter-session PROSE
possible at all — `_clean` refuses control characters, so a multi-line message was impossible
to type until the body moved to a file.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sessioncoord import tmuxio

IDLE = "output\n\n❯  \n"


@pytest.fixture
def tmux(monkeypatch):
    monkeypatch.setattr(tmuxio, "_children", lambda: {})
    monkeypatch.setattr(tmuxio, "claude_under", lambda pane_pid, kids: 4141161)

    class T:
        def __init__(self):
            self.sent = []

        def __call__(self, argv):
            if argv[:2] == ["tmux", "list-panes"]:
                return "%79\tme:7\t1\t/repo"
            if argv[:2] == ["tmux", "capture-pane"]:
                return IDLE
            if argv[:2] == ["tmux", "send-keys"]:
                self.sent.append(argv)
            return ""
    return T()


def typed(t):
    return t.sent[0][-1]


class TestShortMessagesStayInline:
    def test_a_one_liner_is_typed_directly(self, tmux):
        tmuxio.send("me:7", "your blocker cleared, PR #7 landed", tmux, sender="me:5")
        assert "your blocker cleared" in typed(tmux)
        assert "/tmp" not in typed(tmux)

    def test_it_still_carries_the_sigil_and_the_sender(self, tmux):
        tmuxio.send("me:7", "short", tmux, sender="me:5")
        assert typed(tmux).startswith(" >>> from me:5: ")


class TestLongMessagesSpillToAFile:
    def test_only_a_pointer_is_typed(self, tmux):
        body = "x" * 500
        tmuxio.send("me:7", body, tmux, sender="me:5")
        assert len(typed(tmux)) < 120, typed(tmux)
        assert typed(tmux).startswith(" >>> from me:5: see /")

    def test_the_file_holds_the_whole_body(self, tmux):
        body = "a considered paragraph. " * 40
        tmuxio.send("me:7", body, tmux, sender="me:5")
        path = typed(tmux).rsplit("see ", 1)[1]
        assert Path(path).read_text().startswith("a considered paragraph.")

    def test_a_spilled_body_may_contain_newlines(self, tmux):
        # The point. `_clean` refuses control characters in anything TYPED, so multi-line prose
        # between sessions was impossible until the body moved to a file -- which is exactly
        # what a peer hit when its second attempt to reach me was refused for containing
        # newlines.
        body = "# Heading\n\n- one\n- two\n\nAnd a closing paragraph that runs on a while.\n" * 5
        tmuxio.send("me:7", body, tmux, sender="me:5")
        path = typed(tmux).rsplit("see ", 1)[1]
        assert "\n- one\n" in Path(path).read_text()

    def test_the_typed_pointer_itself_carries_no_control_characters(self, tmux):
        tmuxio.send("me:7", "line one\nline two\n" * 40, tmux, sender="me:5")
        assert not tmuxio.CONTROL_RE.search(typed(tmux))


class TestTheThresholdIsAdjustable:
    def test_spilling_can_be_turned_off(self, tmux):
        tmuxio.send("me:7", "y" * 400, tmux, sender="me:5", spill_over=0)
        assert "y" * 400 in typed(tmux)

    def test_and_lowered(self, tmux):
        tmuxio.send("me:7", "z" * 30, tmux, sender="me:5", spill_over=10)
        assert "see /" in typed(tmux)


class TestTwoSpillsInOneSecondDoNotCollide:
    def test_each_body_gets_its_own_file(self, tmux):
        # The name was sender + HHMMSS + pid, which is READABLE but not unique: two long
        # messages from one process in one second produced the same path and the second
        # overwrote the first, so the pointer already typed into a peer's prompt opened
        # somebody else's body.
        first = tmuxio.spill("the first body", "me:5")
        second = tmuxio.spill("the second body", "me:5")
        assert first != second
        assert Path(first).read_text().strip() == "the first body"
        assert Path(second).read_text().strip() == "the second body"


class TestTheSenderLabelIsFilteredToo:
    def test_a_control_byte_in_the_sender_is_refused(self, tmux):
        # `_clean` runs on the BODY, before the sender is interpolated into the final string,
        # so a label carrying an escape sequence reached `send-keys -l` unchecked.
        with pytest.raises(tmuxio.SendRefused, match="sender label"):
            tmuxio.send("me:7", "harmless", tmux, sender="me:5\x1b[2J")

    def test_and_nothing_is_typed_when_it_is(self, tmux):
        with pytest.raises(tmuxio.SendRefused):
            tmuxio.send("me:7", "harmless", tmux, sender="me\r5")
        assert tmux.sent == []
