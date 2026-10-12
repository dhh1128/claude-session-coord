"""Mutual exclusion for operations that mutate a repo's shared state.

Worth recording why this is a lock when presence deliberately is not. Presence needed a
liveness fact, and `flock` was wrong for it twice over: the Bash tool spawns a fresh shell per
call so nothing can hold a lock across a turn, and a lock nobody blocks on is just a file. A
merge is the opposite case. `pr-merge run` is one long-lived process, so it can hold a lock for
its whole duration -- and here you genuinely want the second caller to wait rather than
proceed, which is exactly what a lock provides and a presence record does not.

So Daniel's original instinct about `flock` was right; it was attached to the wrong operation.
"""

from __future__ import annotations

import os

import pytest
from sessioncoord import locks, store


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_COORD_HOME", str(tmp_path / "state"))
    return tmp_path


REPO = "/home/user/code/acme/infra"


class TestExclusion:
    def test_one_holder_at_a_time(self, home):
        with locks.hold(REPO, "merge"):
            with pytest.raises(locks.Busy):
                with locks.hold(REPO, "merge", wait=0):
                    pass

    def test_released_when_the_block_exits(self, home):
        with locks.hold(REPO, "merge"):
            pass
        with locks.hold(REPO, "merge", wait=0):
            pass  # no Busy

    def test_released_even_when_the_holder_raises(self, home):
        # A merge that dies mid-run must not wedge every later merge in the repo. The kernel
        # drops the lock when the fd closes, which is the property a pid file would not have.
        with pytest.raises(RuntimeError):
            with locks.hold(REPO, "merge"):
                raise RuntimeError("CI never went green")
        with locks.hold(REPO, "merge", wait=0):
            pass

    def test_different_names_do_not_contend(self, home):
        with locks.hold(REPO, "merge"):
            with locks.hold(REPO, "worktree", wait=0):
                pass

    def test_different_repos_do_not_contend(self, home):
        with locks.hold(REPO, "merge"):
            with locks.hold("/home/user/code/acme/witness", "merge", wait=0):
                pass


class TestHolderIsNamed:
    def test_the_waiting_session_is_told_who_holds_it(self, home):
        # "Busy, try later" makes a session guess. Naming the window means Daniel can look.
        with locks.hold(REPO, "merge", owner="infra:11 (merging #69)"):
            with pytest.raises(locks.Busy, match="infra:11"):
                with locks.hold(REPO, "merge", wait=0):
                    pass

    def test_status_reports_the_holder_without_taking_the_lock(self, home):
        with locks.hold(REPO, "merge", owner="infra:11"):
            assert "infra:11" in (locks.holder(REPO, "merge") or "")

    def test_status_is_none_when_free(self, home):
        assert locks.holder(REPO, "merge") is None

    def test_a_stale_owner_note_does_not_read_as_held(self, home):
        # The note outlives the lock by design -- it is written inside and not erased on exit,
        # so a crash leaves a breadcrumb. It must never be mistaken for the lock itself.
        with locks.hold(REPO, "merge", owner="infra:11"):
            pass
        assert locks.holder(REPO, "merge") is None


class TestRun:
    def test_runs_the_command_and_returns_its_exit_code(self, home):
        assert locks.run(REPO, "merge", ["true"], wait=0) == 0
        assert locks.run(REPO, "merge", ["false"], wait=0) == 1

    def test_refuses_rather_than_running_when_held(self, home):
        marker = os.path.join(str(home), "ran")
        with locks.hold(REPO, "merge"):
            with pytest.raises(locks.Busy):
                locks.run(REPO, "merge", ["touch", marker], wait=0)
        assert not os.path.exists(marker)


class TestLockFileLocation:
    def test_lives_under_the_repo_state_dir_not_in_the_repo(self, home):
        with locks.hold(REPO, "merge"):
            pass
        assert (store.state_dir(REPO) / "locks" / "merge.lock").exists()
