"""A message remains deliverable when its recipient is outside the sender's repo or absent."""

from __future__ import annotations

import pytest

from sessioncoord import cli, inbox, store
from sessioncoord.model import Presence

SENDER = "/sender"
OTHER = "/other"
THIRD = "/third"


@pytest.fixture
def estate(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_COORD_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("TMUX_PANE", "%1")
    monkeypatch.setattr(cli.gitio, "workspace_root", lambda path: path)
    monkeypatch.setattr(cli.tmuxio, "server_pid", lambda run=None: "999")
    store.save(Presence(session_id="sender", repo_root=SENDER, workdir=SENDER,
                        pane="%1", window="sender:1", title="writer",
                        claude_pid=101, tmux_server="999"))
    return monkeypatch


def peer(repo=OTHER, sid="peer", pane="%2", title="foreman-imbu", window="other:2"):
    store.save(Presence(session_id=sid, repo_root=repo, workdir=repo, pane=pane,
                        window=window, title=title, claude_pid=102,
                        tmux_server="999"))


def hold_budget(monkeypatch):
    monkeypatch.setattr(cli._Doorbell, "reserve", lambda self: "budget exhausted")


def test_cross_repo_recipient_files_in_its_own_inbox(estate):
    peer()
    sent = []
    estate.setattr(cli.tmuxio, "send", lambda target, text, **kw: sent.append((target, text, kw)) or "%2")
    assert cli.main(["msg", "--repo", SENDER, "foreman-imbu", "a note"]) == 0
    assert store.load_inbox(SENDER) == []
    assert [(m.recipient, m.body) for m in store.load_inbox(OTHER)] == [("peer", "a note")]
    assert sent[0][0:2] == ("%2", inbox.DOORBELL)
    assert sent[0][2]["expect_pid"] == 102


def test_cross_repo_ambiguity_refuses_with_repos_and_ids(estate, capsys):
    peer()
    peer(THIRD, "other-peer", "%3", "foreman-imbu", "third:3")
    sent = []
    estate.setattr(cli.tmuxio, "send", lambda *a, **kw: sent.append(a))
    assert cli.main(["msg", "--repo", SENDER, "foreman-imbu", "a note"]) == 2
    assert store.load_inbox(OTHER) == store.load_inbox(THIRD) == []
    assert sent == []
    err = capsys.readouterr().err
    assert f"{OTHER}: peer" in err and f"{THIRD}: other-peer" in err


def test_unregistered_over_budget_is_held(estate, capsys):
    hold_budget(estate)
    estate.setattr(cli.tmuxio, "send", lambda *a, **kw: pytest.fail("budgeted send"))
    assert cli.main(["msg", "--repo", SENDER, "✳ Foreman-Imbu", "wait for me"]) == 0
    assert [(m.recipient, m.body) for m in store.load_held()] == [
        ("foreman-imbu", "wait for me")]
    assert "held for" in capsys.readouterr().out


def test_unregistered_success_is_typed_only(estate):
    sent = []
    estate.setattr(cli.tmuxio, "send", lambda target, text, **kw: sent.append(text) or "%9")
    assert cli.main(["msg", "--repo", SENDER, "%9", "direct note"]) == 0
    assert "direct note" in sent[0]
    assert store.load_held() == []


def test_failed_direct_send_is_held_under_literal_pane(estate):
    def refuse(*a, **kw):
        raise cli.tmuxio.SendRefused("no pane")
    estate.setattr(cli.tmuxio, "send", refuse)
    assert cli.main(["msg", "--repo", SENDER, "%9", "direct note"]) == 0
    assert store.load_held()[0].recipient == "%9"


def test_inbox_without_held_mail_skips_estate_scan(estate, capsys):
    estate.setattr(store, "known_repos", lambda: pytest.fail("scanned the estate"))
    assert not (store.home() / "held.json").exists()
    assert cli.main(["inbox", "--repo", SENDER]) == 0
    assert "Inbox empty" in capsys.readouterr().out


def test_corrupt_held_mail_does_not_block_own_inbox(estate, capsys):
    path = store.home() / "held.json"
    path.write_text("{broken")
    store.save_inbox(SENDER, [inbox.Message(id="normal", sender="peer",
                                            recipient="sender", body="normal mail")])
    assert cli.main(["inbox", "--repo", SENDER]) == 0
    output = capsys.readouterr()
    assert "normal mail" in output.out
    assert len(output.err.splitlines()) == 1
    assert str(path) in output.err
    assert path.read_text() == "{broken"
    assert list(store.home().glob("held.unreadable-*.json")) == []


def test_held_mail_to_bare_pane_reaches_later_session(estate, capsys):
    hold_budget(estate)
    assert cli.main(["msg", "--repo", SENDER, "%9", "for pane nine"]) == 0
    assert [(m.recipient, m.body) for m in store.load_held()] == [
        ("%9", "for pane nine")]
    peer(pane="%9")
    estate.setenv("TMUX_PANE", "%9")
    capsys.readouterr()
    assert cli.main(["inbox", "--repo", OTHER]) == 0
    assert "for pane nine" in capsys.readouterr().out
    assert store.load_held() == []
    assert inbox.unread(store.load_inbox(OTHER), "peer") == []


def test_held_mail_peeks_then_drains_once(estate, capsys):
    hold_budget(estate)
    assert cli.main(["msg", "--repo", SENDER, "Foreman-Imbu", "held body"]) == 0
    capsys.readouterr()
    peer()
    estate.setenv("TMUX_PANE", "%2")
    assert cli.main(["inbox", "--repo", OTHER, "--peek", "--ids"]) == 0
    first = capsys.readouterr().out
    assert "held body" in first and first.startswith(cli.IDS_PREFIX)
    assert len(inbox.unread(store.load_inbox(OTHER), "peer")) == 1
    assert store.load_held() == []
    assert cli.main(["inbox", "--repo", OTHER, "--peek"]) == 0
    assert "held body" in capsys.readouterr().out
    assert cli.main(["inbox", "--repo", OTHER]) == 0
    assert "held body" in capsys.readouterr().out
    assert cli.main(["inbox", "--repo", OTHER]) == 0
    assert "Inbox empty" in capsys.readouterr().out


@pytest.mark.parametrize("target", ["foreman-imbu", "other:2", "peer"])
def test_held_mail_matches_title_window_or_session_id(estate, capsys, target):
    hold_budget(estate)
    assert cli.main(["msg", "--repo", SENDER, target, "found later"]) == 0
    peer()
    estate.setenv("TMUX_PANE", "%2")
    capsys.readouterr()
    assert cli.main(["inbox", "--repo", OTHER, "--peek", "--ids"]) == 0
    out = capsys.readouterr().out
    assert "found later" in out
    ids = out.splitlines()[0][len(cli.IDS_PREFIX):]
    assert cli.main(["inbox", "--repo", OTHER, "--ack", ids]) == 0
    assert inbox.unread(store.load_inbox(OTHER), "peer") == []
    assert cli.main(["inbox", "--repo", OTHER]) == 0
    assert "Inbox empty" in capsys.readouterr().out


def test_two_live_sessions_sharing_name_get_nothing(estate, capsys):
    hold_budget(estate)
    assert cli.main(["msg", "--repo", SENDER, "foreman-imbu", "private note"]) == 0
    peer()
    peer(THIRD, "other-peer", "%3", "foreman-imbu", "third:3")
    for repo, pane in [(OTHER, "%2"), (THIRD, "%3")]:
        estate.setenv("TMUX_PANE", pane)
        assert cli.main(["inbox", "--repo", repo, "--quiet"]) == 0
        out = capsys.readouterr().out
        assert "ambiguous" in out and "private note" not in out
        assert store.load_inbox(repo) == []
    assert len(store.load_held()) == 1
