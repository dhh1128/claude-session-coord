"""Never type over somebody mid-sentence.

Observed 2026-09-18. A peer's `session-coord msg` landed in Daniel's pane while he was part-way
through typing, and the Enter that delivered the message submitted his unfinished sentence with
it. What arrived was one turn reading:

    okay, me:3 and me:6 should now see the skillFrom me:4. (1) Registered:

Two harms, and the second is the worse one. He could not tell whose words were whose — and his
own half-written message was submitted before he had finished writing it. The first is
confusing; the second takes an action on his behalf that he did not choose.

Two defences, deliberately different in strength. A positive observation of a busy prompt
REFUSES. An unreadable prompt does not, because the glyph is a TUI detail that will change one
day and failing closed there would silently kill inter-session messaging on the next release —
so every message is also attributed, and a collision that slips through is at least legible.
"""

from __future__ import annotations

import pytest
from sessioncoord import tmuxio
from sessioncoord.tmuxio import SendRefused

IDLE = "some output\n\n❯  \n"
BUSY = "some output\n\n❯  okay, me:3 and me:6 should now see the skill\n"


@pytest.fixture
def tmux(monkeypatch):
    monkeypatch.setattr(tmuxio, "_children", lambda: {})
    monkeypatch.setattr(tmuxio, "claude_under", lambda pane_pid, kids: 4141161)

    class T:
        def __init__(self, capture=IDLE):
            self.capture = capture
            self.sent = []

        def __call__(self, argv):
            if argv[:2] == ["tmux", "list-panes"]:
                return "%79\tme:7\t1\t/repo"
            if argv[:2] == ["tmux", "capture-pane"]:
                return self.capture
            if argv[:2] == ["tmux", "send-keys"]:
                self.sent.append(argv)
            return ""
    return T


class TestABusyPromptIsNotTypedOver:
    def test_it_refuses_when_the_target_is_mid_sentence(self, tmux):
        t = tmux(BUSY)
        with pytest.raises(SendRefused, match="unsubmitted text"):
            tmuxio.send("me:7", "a message that would have been appended", t)
        assert t.sent == []

    def test_it_sends_when_the_prompt_is_idle(self, tmux):
        t = tmux(IDLE)
        assert tmuxio.send("me:7", "a message for an idle peer", t) == "%79"
        assert t.sent

    def test_force_overrides_it(self, tmux):
        t = tmux(BUSY)
        tmuxio.send("me:7", "an urgent message", t, force=True)
        assert t.sent

    def test_an_unreadable_prompt_does_not_block_the_channel(self, tmux):
        # Failing closed here would silently kill peer messaging the next time the TUI changes
        # its prompt glyph, which is a worse outcome than an occasional legible collision.
        t = tmux("output with no prompt line at all\n")
        assert tmuxio.send("me:7", "a message", t) == "%79"


class TestPromptDetection:
    def test_an_idle_prompt_reads_as_free(self, tmux):
        assert tmuxio.prompt_is_busy("%79", tmux(IDLE)) is False

    def test_a_typed_prompt_reads_as_busy(self, tmux):
        assert tmuxio.prompt_is_busy("%79", tmux(BUSY)) is True

    def test_no_prompt_line_is_unknown_rather_than_free(self, tmux):
        # The unknown sentinel again: "I could not look" is not "nobody is typing".
        assert tmuxio.prompt_is_busy("%79", tmux("no prompt here\n")) is None

    def test_a_failed_capture_is_unknown(self, tmux):
        t = tmux()
        t.capture = None
        assert tmuxio.prompt_is_busy("%79", lambda argv: None) is None


class TestAttribution:
    def test_the_sender_is_prefixed_onto_the_message(self, tmux):
        t = tmux(IDLE)
        tmuxio.send("me:7", "the body of the message", t, sender="me:5")
        assert t.sent[0][-1] == " >>> from me:5: the body of the message"

    def test_an_unattributed_message_still_gets_the_sigil(self, tmux):
        # The sigil marks the seam whether or not we can name the sender; the attribution is
        # the bonus, the seam marker is the point.
        t = tmux(IDLE)
        tmuxio.send("me:7", "the body of the message", t)
        assert t.sent[0][-1] == " >>> the body of the message"


class TestASuggestionIsNotTyping:
    """2026-10-10: Claude Code's dim suggested prompt read as somebody typing, in every idle peer."""

    SUGGESTED = "\x1b[39m❯\xa0\x1b[2mlow stakes, quick check and merge\x1b[0m\n"
    TYPED = "\x1b[39m❯\xa0okay, me:3 and me:6 should\x1b[0m\n"
    TYPED_OVER = "\x1b[39m❯\xa0lo\x1b[2mw stakes, quick check and merge\x1b[0m\n"

    def test_a_dim_suggestion_reads_as_free(self, tmux):
        assert tmuxio.prompt_is_busy("%79", tmux(self.SUGGESTED)) is False

    def test_typed_text_with_escapes_still_reads_as_busy(self, tmux):
        assert tmuxio.prompt_is_busy("%79", tmux(self.TYPED)) is True

    def test_typed_text_ahead_of_a_dim_completion_reads_as_busy(self, tmux):
        assert tmuxio.prompt_is_busy("%79", tmux(self.TYPED_OVER)) is True

    def test_the_capture_asks_tmux_for_attributes(self, tmux):
        seen = []

        def run(argv):
            seen.append(argv)
            return self.SUGGESTED
        tmuxio.prompt_is_busy("%79", run)
        assert "-e" in seen[0]
