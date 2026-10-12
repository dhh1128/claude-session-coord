"""Registration is a snapshot, and a stale one makes the board confidently wrong.

`me:6` on 2026-09-18 had taken a worktree and moved into it with `cd` inside Bash rather than
with the `EnterWorktree` tool. A shell `cd` does not change the SESSION's working directory, and
the session's directory is what registration records and what the gate is handed. So the board
said it was in the shared main checkout, the gate refused its commit on that basis, and it was
shown a remedy it had already followed. It spent an hour on it and escalated.

The board being wrong is worse than the board being empty: an unregistered session is visibly
absent, while a misregistered one is confidently misplaced, and the gate acts on it.

This warns at the one moment both values are visible at once.
"""

from __future__ import annotations

import pytest
from sessioncoord import cli, store
from sessioncoord.model import Presence

REPO = "/repo"
WT = "/repo/.worktrees/mine"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_COORD_HOME", str(tmp_path / "state"))
    return tmp_path


@pytest.fixture
def world(monkeypatch):
    monkeypatch.setattr(cli.gitio, "repo_root", lambda p, run=None: REPO)
    monkeypatch.setattr(cli.gitio, "branch", lambda p, run=None: "main")
    monkeypatch.setattr(cli.gitio, "facts", lambda r, run=None: None)
    monkeypatch.setattr(cli.tmuxio, "claude_pids", lambda run=None: {"%1": 111})
    monkeypatch.setattr(cli.tmuxio, "server_pid", lambda run=None: "999")
    monkeypatch.setattr(cli.tmuxio, "window_of", lambda p, run=None: "me:1")


def test_it_warns_when_the_recorded_tree_is_not_the_live_one(home, world, monkeypatch, capsys):
    # --cwd says the main checkout (what a session that never used EnterWorktree reports),
    # while the command is actually running in a worktree.
    monkeypatch.setattr(cli.gitio, "workdir",
                        lambda p, run=None: WT if p not in (REPO,) else REPO)
    monkeypatch.setattr(cli.os, "getcwd", lambda: WT)
    cli.main(["register", "--repo", REPO, "--session", "s1", "--pane", "%1", "--pid", "111",
              "--cwd", REPO])
    err = capsys.readouterr().err
    assert "recorded as working in" in err
    assert "EnterWorktree" in err and "`cd`" in err


def test_it_is_silent_when_they_agree(home, world, monkeypatch, capsys):
    monkeypatch.setattr(cli.gitio, "workdir", lambda p, run=None: WT)
    monkeypatch.setattr(cli.os, "getcwd", lambda: WT)
    cli.main(["register", "--repo", REPO, "--session", "s1", "--pane", "%1", "--pid", "111",
              "--cwd", WT])
    assert "recorded as working in" not in capsys.readouterr().err


def test_the_record_still_saves_despite_the_warning(home, world, monkeypatch):
    # A warning, not a refusal. Refusing would leave the session unregistered, which is the
    # state the whole package is worst at.
    monkeypatch.setattr(cli.gitio, "workdir",
                        lambda p, run=None: WT if p not in (REPO,) else REPO)
    monkeypatch.setattr(cli.os, "getcwd", lambda: WT)
    cli.main(["register", "--repo", REPO, "--session", "s1", "--pane", "%1", "--pid", "111",
              "--cwd", REPO])
    assert [p.session_id for p in store.load_all(REPO)] == ["s1"]


def test_the_gate_refusal_names_the_cd_cause_and_the_conductor():
    import importlib.machinery
    import importlib.util
    from pathlib import Path
    hook = Path(__file__).resolve().parents[1] / "hooks" / "session-coord-gate-check"
    spec = importlib.util.spec_from_loader(
        "gate_msg", importlib.machinery.SourceFileLoader("gate_msg", str(hook)))
    g = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(g)
    import io
    import json as _json
    import sys as _sys

    captured = io.StringIO()
    real = _sys.stdout
    _sys.stdout = captured
    try:
        g.deny("REASON")
    finally:
        _sys.stdout = real
    # The deny() helper wraps whatever it is given; the wording under test lives at the call
    # site, so assert against the source rather than a synthetic call.
    src = hook.read_text()
    assert "EnterWorktree" in src and "conductor" in src
    assert "say so to Daniel and have him confirm" not in src
