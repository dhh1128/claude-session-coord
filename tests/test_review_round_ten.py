"""Round ten. Two findings, both about the gate naming the wrong tree.

Both are the same mistake in different clothes: the analyser was confident about a tree it had
no right to be confident about. `git -C/repo add -A` uses git's attached short-option form,
which the exact-match test skipped as an unknown option — so the write was scoped to the
caller's cwd. And when the tree genuinely could not be named, the hook asked its question with
no repository hint at all, which made `cmd_check` resolve the repo from the caller's cwd: for
`cd /other/repo && git add -A` that asks THIS repo's board about a collision in a different
one. Conservative within the wrong repository is not conservative.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
from pathlib import Path

import pytest


def _gate():
    hook = Path(__file__).resolve().parents[1] / "hooks" / "session-coord-gate-check"
    spec = importlib.util.spec_from_loader(
        "gate10", importlib.machinery.SourceFileLoader("gate10", str(hook)))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gate = _gate()


class TestAttachedShortOption:
    @pytest.mark.parametrize("command,expected", [
        ("git -C/repo add -A", "/repo"),
        ("git -C /repo add -A", "/repo"),
        ("git -C=/repo add -A", "/repo"),
    ])
    def test_every_spelling_of_dash_c_names_the_same_tree(self, command, expected):
        # `-C/repo` was skipped as an unknown option and the write scoped to the caller's cwd,
        # so a safe worktree could stage the shared checkout with the gate reporting clean.
        assert gate.analyse(command, "/repo/.worktrees/mine")[1] == expected

    def test_a_relative_attached_form_resolves_against_the_cwd(self):
        assert gate.analyse("git -C../.. add -A", "/repo/a/b")[1] == "/repo/a/b/../.."

    def test_it_is_still_recognised_as_a_write(self):
        assert gate.analyse("git -C/repo add -A", "/repo/.worktrees/mine")[0]


class TestCandidateTreesForAnUnknownTarget:
    def test_a_cd_target_is_offered_as_a_candidate(self):
        # The board for /other/repo is the one that has to be asked, and the caller's cwd
        # cannot reach it.
        assert "/other/repo" in gate.candidate_trees(
            "cd /other/repo && git add -A", "/repo/.worktrees/mine")

    def test_so_is_a_work_tree_redirect(self):
        assert "/shared" in gate.candidate_trees(
            "git --work-tree=/shared add -A", "/repo/.worktrees/mine")

    def test_and_an_attached_dash_c(self):
        assert "/other" in gate.candidate_trees(
            "git -C/other --work-tree=/x add -A", "/repo/.worktrees/mine")

    def test_an_unexpandable_variable_is_not_a_candidate(self):
        # Joining a literal `$TREE` onto the cwd would query a directory that does not exist,
        # which is a worse answer than no answer.
        assert gate.candidate_trees("git --work-tree=$TREE add -A", "/repo") == []

    def test_the_callers_own_cwd_is_not_listed_as_a_candidate(self):
        # It is already asked separately; listing it again would double every query.
        assert gate.candidate_trees("cd /repo && git add -A", "/repo") == []

    def test_an_unparseable_command_yields_no_candidates_rather_than_raising(self):
        assert gate.candidate_trees("git add -A 'unterminated", "/repo") == []


class TestPullIsAWrite:
    """`git pull` is `fetch` plus `merge`, and the merge half lands in the working tree.

    The most embarrassing gap in eleven rounds: the 2026-09-17 incident WAS a session merging
    origin/main into a branch two others were committing to, and `git pull` is the ordinary way
    that happens. The gate built from that incident did not gate it.
    """

    @pytest.mark.parametrize("command", [
        "git pull",
        "git pull --rebase",
        "git pull origin main",
        "git -C /repo pull --ff-only",
    ])
    def test_it_is_gated(self, command):
        assert gate.analyse(command, "/repo")[0], command

    def test_but_a_bare_fetch_is_not(self):
        # fetch touches no file in the working tree, so gating it would be a false refusal on
        # one of the commonest read-only git commands there is.
        assert not gate.analyse("git fetch --prune origin", "/repo")[0]
