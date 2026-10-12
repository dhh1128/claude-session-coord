"""A session that took a worktree must never be denied for a collision it is not in.

The worst false positive in the tool, and the most self-defeating: the gate's own remedy is
"take a worktree", and a session that had taken one was blocked anyway, told the main checkout
was shared, and pointed at the remedy it had already followed. Both peer sessions hit it within
minutes, both compliant, and one could not report it because the report tripped the same bug.

Cause, verified rather than assumed -- both peers proposed a different one, that `gitio.workdir`
collapses a worktree up to the common git dir. It does not; a direct probe shows
`workdir(worktree) != workdir(main)` and simple commands scope correctly. The real cause was
mine: when `analyse` could not name a tree, the hook omitted `--workdir` and asked the repo-wide
question, whose answer includes the main checkout's shared-tree finding and therefore denies
everyone.
"""

from __future__ import annotations

import json

import pytest
from sessioncoord import cli, store
from sessioncoord.model import Presence

REPO = "/repo"
MAIN = "/repo"
MINE = "/repo/.worktrees/mine"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_COORD_HOME", str(tmp_path / "state"))
    return tmp_path


@pytest.fixture
def three_in_main_one_in_a_worktree(home, monkeypatch):
    monkeypatch.setattr(cli.gitio, "repo_root", lambda p, run=None: REPO)
    monkeypatch.setattr(cli.gitio, "branch", lambda p, run=None: "main")
    monkeypatch.setattr(cli.gitio, "facts", lambda r, run=None: None)
    monkeypatch.setattr(cli.tmuxio, "claude_pids",
                        lambda run=None: {"%1": 1, "%2": 2, "%3": 3, "%4": 4})
    monkeypatch.setattr(cli.tmuxio, "server_pid", lambda run=None: "999")
    # The real shape on 2026-09-18: me:3, me:6, me:7 sharing the main checkout, me:5 compliant.
    for sid, pane, pid, wd in [("a", "%1", 1, MAIN), ("b", "%2", 2, MAIN),
                               ("c", "%3", 3, MAIN), ("mine", "%4", 4, MINE)]:
        store.save(Presence(session_id=sid, repo_root=REPO, workdir=wd, pane=pane,
                            claude_pid=pid, window=f"w:{sid}"))
    # Identity: a path is its own workdir. This is what the peers thought was broken.
    monkeypatch.setattr(cli.gitio, "workdir", lambda p, run=None: p.rstrip("/"))


class TestTheCompliantSessionIsNotDenied:
    def test_scoping_to_its_own_worktree_returns_no_block(
            self, three_in_main_one_in_a_worktree, capsys):
        rc = cli.main(["check", "--repo", REPO, "--workdir", MINE, "--json"])
        codes = [f["code"] for f in json.loads(capsys.readouterr().out)]
        assert rc != 2 and "shared-tree" not in codes

    def test_while_the_sessions_actually_sharing_are_still_blocked(
            self, three_in_main_one_in_a_worktree, capsys):
        rc = cli.main(["check", "--repo", REPO, "--workdir", MAIN, "--json"])
        assert rc == 2
        assert "shared-tree" in [f["code"] for f in json.loads(capsys.readouterr().out)]

    def test_the_unscoped_question_is_what_denied_everyone(
            self, three_in_main_one_in_a_worktree, capsys):
        # Pinned so nobody reintroduces it as "the conservative fallback". This IS the
        # repo-wide answer, and it is why the hook must never ask it.
        cli.main(["check", "--repo", REPO, "--json"])
        assert "shared-tree" in [f["code"] for f in json.loads(capsys.readouterr().out)]


class TestWorkdirDoesNotCollapse:
    def test_a_worktree_is_its_own_workdir_even_though_the_repo_is_shared(self):
        # Both peers proposed this as the cause. Verified against the real git boundary rather
        # than accepted: repo_root SHOULD collapse (it is the board key); workdir must not.
        from sessioncoord import gitio
        wt = "/home/user/code/me/tools/.worktrees/gate-fixes"
        main = "/home/user/code/me/tools"
        import os
        if not os.path.isdir(wt):
            pytest.skip("this worktree only exists while the fix is being written")
        assert gitio.workdir(wt) != gitio.workdir(main)
        assert gitio.repo_root(wt) == gitio.repo_root(main)
