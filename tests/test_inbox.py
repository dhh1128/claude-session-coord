"""The queue, and the doorbell that says something is in it.

Daniel's design. The thing it fixes, which the busy-prompt guard could not: a peer whose prompt
is occupied was simply unreachable. Three consecutive sends to one session were refused within a
minute, correctly, and the thing that needed saying went nowhere — it sat in a file nobody had
been told about. Filing first and ringing second turns that from a failure into a delay.

The doorbell is a runnable command, not a bare sigil, and the reason is the recipient: a
language model reading its own prompt, not a parser. A bare mark needs a decoder and would be
met with "what does this mean?", routed straight back to Daniel — the interruption this whole
package exists to remove.
"""

from __future__ import annotations

import pytest
from sessioncoord import cli, inbox, store, tmuxio
from sessioncoord.model import Presence

REPO = "/repo"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_COORD_HOME", str(tmp_path / "state"))
    return tmp_path


@pytest.fixture
def two_sessions(home, monkeypatch):
    monkeypatch.setattr(cli.gitio, "repo_root", lambda p, run=None: REPO)
    monkeypatch.setattr(cli.gitio, "workdir", lambda p, run=None: REPO)
    monkeypatch.setattr(cli.gitio, "branch", lambda p, run=None: "main")
    monkeypatch.setattr(cli.gitio, "facts", lambda r, run=None: None)
    monkeypatch.setattr(cli.tmuxio, "server_pid", lambda run=None: "999")
    for sid, pane, pid, win in [("me5", "%1", 1, "me:5"), ("me7", "%2", 2, "me:7")]:
        store.save(Presence(session_id=sid, repo_root=REPO, workdir=REPO, pane=pane,
                            claude_pid=pid, window=win, tmux_server="999"))
    monkeypatch.setenv("TMUX_PANE", "%1")   # we are me:5


def m(mid, recipient="me7", sender="me:5", body="hello", read=False, created="t1"):
    return inbox.Message(id=mid, sender=sender, recipient=recipient, body=body,
                         created=created, read=read)


class TestTheQueue:
    def test_unread_is_per_recipient_and_oldest_first(self):
        q = [m("3", created="t3"), m("1", created="t1"), m("2", created="t2"),
             m("x", recipient="someone-else")]
        assert [x.id for x in inbox.unread(q, "me7")] == ["1", "2", "3"]

    def test_read_messages_drop_out(self):
        q = [m("1"), m("2", read=True)]
        assert [x.id for x in inbox.unread(q, "me7")] == ["1"]

    def test_marking_read_touches_only_the_named_ones(self):
        # A drain that read part of the queue must not mark the rest, or the unread ones vanish
        # without anyone having seen them.
        q = [m("1"), m("2"), m("3")]
        got = inbox.mark_read(q, ["1", "3"])
        assert [x.id for x in got if x.read] == ["1", "3"]


class TestTheDoorbellRingsOnce:
    def test_it_rings_for_the_first_unread(self):
        assert inbox.needs_doorbell([m("1")], "me7")

    def test_it_stays_silent_while_mail_is_already_waiting(self):
        # Within the window: a peer told two minutes ago has been told.
        assert not inbox.needs_doorbell(
            [m("1", created="2026-09-18T05:00:00"), m("2")], "me7", "2026-09-18T05:02:00")

    def test_but_it_rings_again_when_the_oldest_unread_has_been_ignored(self):
        # THE CORRECTION. The original rule bound "has been told" to "has unread mail", and the
        # two stopped being the same thing the moment a peer did not drain. Two peers
        # accumulated three and six unread messages inside two hours and read none; one then
        # re-proposed a diagnosis that my refutation, unread in its inbox, had already answered.
        assert inbox.needs_doorbell(
            [m("1", created="2026-09-18T05:00:00"), m("2")], "me7", "2026-09-18T05:20:00")

    def test_an_unparseable_timestamp_does_not_ring_forever(self):
        assert not inbox.needs_doorbell([m("1", created="whenever"), m("2")], "me7", "now")

    def test_a_naive_timestamp_beside_an_aware_one_does_not_raise(self):
        # `_now()` is timezone-aware; a message filed by an older build or edited by hand may
        # not be, and subtracting the two is a TypeError rather than a ValueError. This runs
        # AFTER the new message is saved, so the crash lost nothing and reported a successful
        # delivery as a traceback.
        assert inbox.needs_doorbell(
            [m("1", created="2026-09-18T05:00:00"), m("2")], "me7",
            "2026-09-18T05:20:00+00:00")
        assert not inbox.needs_doorbell(
            [m("1", created="2026-09-18T05:00:00+00:00"), m("2")], "me7",
            "2026-09-18T05:02:00")

    def test_it_rings_again_once_the_queue_has_been_drained(self):
        assert inbox.needs_doorbell([m("1", read=True), m("2")], "me7")

    def test_the_doorbell_is_a_runnable_command(self):
        # Not a bare sigil: a session that has never loaded the skill must still know what to
        # do with it, and "what does >>> mean?" goes straight back to Daniel.
        #
        # The earlier version of this test asserted the constant carried its own leading space
        # and sigil, and passed green while the recipient was actually shown
        # ` >>>  >>> session-coord inbox` -- because `tmuxio.send` decorates everything it
        # types. It pinned the bug by checking the ingredient instead of the meal. So this
        # asserts on what LANDS IN THE PROMPT, which is the only thing anybody reads.
        assert inbox.DOORBELL == "session-coord inbox"

    def test_and_what_lands_in_the_prompt_is_that_command_once(self, monkeypatch):
        typed = []
        monkeypatch.setattr(tmuxio, "panes",
                            lambda run=None: {"%2": {"pane": "%2", "window": "me:7",
                                                     "path": "/repo", "claude_pid": 2}})
        monkeypatch.setattr(tmuxio, "prompt_is_busy", lambda pane, run=None: False)
        tmuxio.send("%2", inbox.DOORBELL, lambda argv: typed.append(argv) or "", sender="",
                    spill_over=0)
        assert typed[0][-1] == " >>> session-coord inbox"


class TestMsgFilesThenRings:
    def test_the_body_is_filed_and_only_the_doorbell_is_typed(self, two_sessions, monkeypatch):
        sent = {}
        monkeypatch.setattr(cli.tmuxio, "send",
                            lambda t, x, **kw: sent.update(target=t, text=x) or "%2")
        body = "a long considered paragraph. " * 20
        assert cli.main(["msg", "--repo", REPO, "me:7", body]) == 0
        assert sent["text"] == inbox.DOORBELL
        assert store.load_inbox(REPO)[0].body == body

    def test_a_second_message_files_without_ringing_again(self, two_sessions, monkeypatch):
        rings = []
        monkeypatch.setattr(cli.tmuxio, "send", lambda t, x, **kw: rings.append(x) or "%2")
        cli.main(["msg", "--repo", REPO, "me:7", "first"])
        cli.main(["msg", "--repo", REPO, "me:7", "second"])
        assert len(rings) == 1
        assert [x.body for x in store.load_inbox(REPO)] == ["first", "second"]

    def test_a_refused_doorbell_is_not_a_failure(self, two_sessions, monkeypatch, capsys):
        # THE POINT. A busy peer used to mean the message went nowhere; now it means the
        # message is filed and the ring is late.
        def refuse(t, x, **kw):
            raise cli.tmuxio.SendRefused("prompt is busy")
        monkeypatch.setattr(cli.tmuxio, "send", refuse)
        assert cli.main(["msg", "--repo", REPO, "me:7", "important"]) == 0
        assert [x.body for x in store.load_inbox(REPO)] == ["important"]
        assert "could not be rung" in capsys.readouterr().err

    def test_an_unconfirmed_codex_doorbell_is_reported_unknown(self, two_sessions,
                                                                 monkeypatch, capsys):
        def unknown(t, x, **kw):
            raise cli.tmuxio.SendUnknown("composer still holds the doorbell")
        monkeypatch.setattr(cli.tmuxio, "send", unknown)
        assert cli.main(["msg", "--repo", REPO, "me:7", "important"]) == 0
        assert [x.body for x in store.load_inbox(REPO)] == ["important"]
        err = capsys.readouterr().err
        assert "doorbell outcome unknown" in err
        assert "could not be rung" not in err

    def test_an_unregistered_target_is_still_typed_directly(self, two_sessions, monkeypatch):
        # No inbox to file into, so fall back to the old behaviour rather than dropping it.
        sent = {}
        monkeypatch.setattr(cli.tmuxio, "send",
                            lambda t, x, **kw: sent.update(text=x) or "%9")
        cli.main(["msg", "--repo", REPO, "%9", "hello there"])
        assert "hello there" in sent["text"]
        assert store.load_inbox(REPO) == []


class TestDraining:
    def test_inbox_prints_the_bodies_and_marks_them_read(self, two_sessions, monkeypatch,
                                                         capsys):
        monkeypatch.setattr(cli.tmuxio, "send", lambda t, x, **kw: "%2")
        cli.main(["msg", "--repo", REPO, "me:7", "the whole message body"])
        monkeypatch.setenv("TMUX_PANE", "%2")   # now we are me:7
        cli.main(["inbox", "--repo", REPO])
        out = capsys.readouterr().out
        # CHANGED 2026-10-10: a label is the stable name with the live window after it, since
        # windows renumber when one closes. The window is still in it, which is what this asks.
        assert "the whole message body" in out and "(now me:5)" in out
        assert inbox.unread(store.load_inbox(REPO), "me7") == []

    def test_peek_leaves_them_unread(self, two_sessions, monkeypatch, capsys):
        monkeypatch.setattr(cli.tmuxio, "send", lambda t, x, **kw: "%2")
        cli.main(["msg", "--repo", REPO, "me:7", "body"])
        monkeypatch.setenv("TMUX_PANE", "%2")
        cli.main(["inbox", "--repo", REPO, "--peek"])
        assert len(inbox.unread(store.load_inbox(REPO), "me7")) == 1

    def test_peek_does_not_claim_to_have_marked_them_read(self, two_sessions, monkeypatch,
                                                          capsys):
        # The footer reports the persisted state, not a fixed string. Telling a reader their
        # mail was cleared when it was not is the one thing a peek must not do.
        monkeypatch.setattr(cli.tmuxio, "send", lambda t, x, **kw: "%2")
        cli.main(["msg", "--repo", REPO, "me:7", "body"])
        monkeypatch.setenv("TMUX_PANE", "%2")
        cli.main(["inbox", "--repo", REPO, "--peek"])
        out = capsys.readouterr().out
        assert "Still unread" in out and "Marked read." not in out

    def test_an_empty_inbox_is_quiet_when_asked_to_be(self, two_sessions, capsys):
        assert cli.main(["inbox", "--repo", REPO, "--session", "me7", "--quiet"]) == 0
        assert capsys.readouterr().out == ""

    def test_and_says_so_when_not(self, two_sessions, capsys):
        cli.main(["inbox", "--repo", REPO, "--session", "me7"])
        assert "Inbox empty" in capsys.readouterr().out


class TestTheQueueIsNotAWayRoundTheTerminalGuard:
    def test_prose_is_filed(self, two_sessions, monkeypatch):
        # Newlines and tabs are the POINT of the queue -- a filed message may be a list or a
        # diff -- so the filing check must not reject them the way the typed path does.
        monkeypatch.setattr(cli.tmuxio, "send", lambda t, x, **kw: "%2")
        body = "one\n\t- two\n- three"
        assert cli.main(["msg", "--repo", REPO, "me:7", body]) == 0
        assert store.load_inbox(REPO)[0].body == body

    def test_an_escape_sequence_is_refused_rather_than_filed(self, two_sessions, capsys):
        assert cli.main(["msg", "--repo", REPO, "me:7", "hi\x1b[2J there"]) == 2
        assert store.load_inbox(REPO) == []
        assert "control characters" in capsys.readouterr().err

    def test_a_newline_in_the_subject_is_refused(self, two_sessions):
        # The subject is rendered inline in a header, where a newline breaks the shape.
        assert cli.main(["msg", "--repo", REPO, "me:7", "body", "--subject", "a\nb"]) == 2

    def test_a_control_byte_already_on_disk_is_escaped_when_rendered(self):
        out = inbox.render([m("1", body="before\x1bafter")])
        assert "\x1b" not in out and "\\x1b" in out


class TestAnUnreadableInboxIsNotAnEmptyOne:
    def test_filing_preserves_it_instead_of_overwriting(self, two_sessions, monkeypatch,
                                                        capsys):
        # The ask-queue data loss arriving through the inbox door: load returns [], the caller
        # appends and saves, and every filed message is gone. Quarantine keeps the bytes AND
        # keeps the channel open, which refusing the write would not.
        monkeypatch.setattr(cli.tmuxio, "send", lambda t, x, **kw: "%2")
        cli.main(["msg", "--repo", REPO, "me:7", "the first message"])
        path = store._inbox_path(REPO)
        path.write_text("{not a list")
        assert cli.main(["msg", "--repo", REPO, "me:7", "the second message"]) == 0
        assert [x.body for x in store.load_inbox(REPO)] == ["the second message"]
        kept = sorted(path.parent.glob("inbox.unreadable-*.json"))
        assert len(kept) == 1 and kept[0].read_text() == "{not a list"

    def test_reading_says_so_instead_of_reporting_an_empty_inbox(self, two_sessions, capsys):
        store._inbox_path(REPO).write_text('{"not": "a list"}')
        assert cli.main(["inbox", "--repo", REPO, "--session", "me7"]) == 2
        err = capsys.readouterr().err
        assert "not a list of messages" in err and "nothing was changed" in err

    def test_one_bad_ENTRY_still_degrades_rather_than_raising(self, two_sessions):
        # Per-item tolerance is deliberate and stays: refusing to deliver nine messages over
        # one malformed tenth would break the channel to fix a nuisance.
        store._inbox_path(REPO).write_text('[{"id": "1", "sender": "a", "recipient": "me7", '
                                           '"body": "kept"}, 42, "nonsense"]')
        assert [x.body for x in store.load_inbox(REPO)] == ["kept"]


class TestAnAmbiguousTargetFilesNothing:
    def test_a_window_naming_two_sessions_is_refused_before_anything_is_written(
            self, two_sessions, monkeypatch, capsys):
        # Filing into whichever record sorted first, and only THEN letting `send` refuse the
        # ambiguity, delivered the message to the wrong session and reported a refusal.
        from sessioncoord.model import Presence
        store.save(Presence(session_id="me7b", repo_root=REPO, workdir=REPO, pane="%3",
                            claude_pid=3, window="me:7", tmux_server="999"))
        sent = []
        monkeypatch.setattr(cli.tmuxio, "send", lambda t, x, **kw: sent.append(x) or "%2")
        assert cli.main(["msg", "--repo", REPO, "me:7", "which of you?"]) == 2
        assert store.load_inbox(REPO) == [] and sent == []
        assert "names 2 registered sessions" in capsys.readouterr().err
