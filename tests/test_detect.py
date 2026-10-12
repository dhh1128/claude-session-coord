"""What the board notices, and -- just as load-bearing -- what it stays quiet about.

Every detector here traces to something that actually happened in `acme/infra` on
2026-09-17. The silence tests matter as much as the firing ones: the common case is one
session alone in a repo, and a tool that says anything at all in that case will be muted,
after which it protects nobody.
"""

from __future__ import annotations

import fixtures
from sessioncoord.detect import check
from sessioncoord.model import Board, CONDUCTOR, RepoFacts


def codes(findings):
    return [f.code for f in findings]


class TestSharedTree:
    def test_fires_on_the_2026_09_17_tangle(self):
        # Four sessions, one working tree. The near-miss: infra:6 held infra:11's
        # uncommitted work and commits with `git add -A`.
        found = check(fixtures.infra_tangle())
        assert "shared-tree" in codes(found)

    def test_is_a_block_not_a_warning(self):
        # Daniel's rule calls this the hazard the whole worktree default exists to prevent,
        # so it outranks everything else the board can say.
        f = next(f for f in check(fixtures.infra_tangle()) if f.code == "shared-tree")
        assert f.severity == "block"

    def test_names_every_session_in_the_tree_so_he_knows_which_windows(self):
        f = next(f for f in check(fixtures.infra_tangle()) if f.code == "shared-tree")
        # CHANGED 2026-10-10: a label is the stable name with the live window after it, since
        # windows renumber when one closes. The window is still in it, which is what this asks.
        assert {x.split("(now ")[-1].rstrip(")") for x in f.sessions} == {
            "infra:6", "infra:8", "infra:10", "infra:11"}

    def test_silent_once_each_session_has_its_own_worktree(self):
        assert "shared-tree" not in codes(check(fixtures.tidy_board()))

    def test_silent_for_a_lone_session_in_the_main_checkout(self):
        # One session sharing a tree with nobody is the normal case, not a finding.
        board = Board.of(fixtures.INFRA, [fixtures.presence("solo", "w:1", "%1", 1)])
        assert "shared-tree" not in codes(check(board))


class TestSharedBranch:
    def test_fires_when_two_worktrees_sit_on_one_branch(self):
        board = Board.of(fixtures.INFRA, [
            fixtures.presence("a", "w:1", "%1", 1, workdir=f"{fixtures.INFRA}/.worktrees/x",
                              branch="shared"),
            fixtures.presence("b", "w:2", "%2", 2, workdir=f"{fixtures.INFRA}/.worktrees/y",
                              branch="shared"),
        ])
        assert "shared-branch" in codes(check(board))

    def test_does_not_double_report_sessions_already_caught_sharing_a_tree(self):
        # Four sessions in one checkout are necessarily on one branch -- a working tree holds
        # exactly one. Reporting both would make the block look like two problems.
        found = codes(check(fixtures.infra_tangle()))
        assert "shared-tree" in found and "shared-branch" not in found


class TestUnworktreed:
    def test_fires_on_a_session_in_the_main_checkout_while_others_are_live(self):
        board = Board.of(fixtures.INFRA, [
            fixtures.presence("a", "w:1", "%1", 1, workdir=fixtures.INFRA, branch="main"),
            fixtures.presence("b", "w:2", "%2", 2, workdir=f"{fixtures.INFRA}/.worktrees/y",
                              branch="y"),
        ])
        f = next(f for f in check(board) if f.code == "unworktreed")
        # CHANGED 2026-10-10: a label is the stable name with the live window after it, since
        # windows renumber when one closes. The window is still in it, which is what this asks.
        assert f.sessions == ("a (now w:1)",)
        assert "EnterWorktree" in f.remedy  # a running session can relocate without restarting

    def test_silent_when_that_session_is_the_only_one_here(self):
        board = Board.of(fixtures.INFRA, [
            fixtures.presence("a", "w:1", "%1", 1, workdir=fixtures.INFRA, branch="main")])
        assert "unworktreed" not in codes(check(board))


class TestClaimOverlap:
    def test_catches_two_sessions_wanting_the_same_file(self):
        # ci.yml's gate list: a single hand-edited line every workstream appends to. It broke
        # twice on 2026-09-17 and three of four sessions touched it.
        f = next(f for f in check(fixtures.tidy_board()) if f.code == "claim-overlap")
        assert "ci.yml" in f.detail
        # CHANGED 2026-10-10: a label is the stable name with the live window after it, since
        # windows renumber when one closes. The window is still in it, which is what this asks.
        assert {x.split("(now ")[-1].rstrip(")") for x in f.sessions} == {"infra:6", "infra:8"}

    def test_a_directory_claim_covers_a_file_inside_it(self):
        board = Board.of(fixtures.INFRA, [
            fixtures.presence("a", "w:1", "%1", 1, workdir=f"{fixtures.INFRA}/.worktrees/a",
                              branch="a", claims=("docs/cyber/**",)),
            fixtures.presence("b", "w:2", "%2", 2, workdir=f"{fixtures.INFRA}/.worktrees/b",
                              branch="b", claims=("docs/cyber/census.md",)),
        ])
        assert "claim-overlap" in codes(check(board))

    def test_neighbouring_directories_do_not_collide(self):
        # `docs/cyberpunk` must not read as inside `docs/cyber`. Prefix matching on raw
        # strings gets this wrong; the boundary has to be a path separator.
        board = Board.of(fixtures.INFRA, [
            fixtures.presence("a", "w:1", "%1", 1, workdir=f"{fixtures.INFRA}/.worktrees/a",
                              branch="a", claims=("docs/cyber/**",)),
            fixtures.presence("b", "w:2", "%2", 2, workdir=f"{fixtures.INFRA}/.worktrees/b",
                              branch="b", claims=("docs/cyberpunk/x.md",)),
        ])
        assert "claim-overlap" not in codes(check(board))

    def test_silent_when_nobody_declared_anything(self):
        board = Board.of(fixtures.INFRA, [
            fixtures.presence("a", "w:1", "%1", 1, workdir=f"{fixtures.INFRA}/.worktrees/a",
                              branch="a"),
            fixtures.presence("b", "w:2", "%2", 2, workdir=f"{fixtures.INFRA}/.worktrees/b",
                              branch="b"),
        ])
        assert "claim-overlap" not in codes(check(board))


class TestConductor:
    def test_asks_for_one_when_several_sessions_are_live_and_none_holds_it(self):
        # The 2026-09-17 lesson: three well-behaved sessions each escalated to Daniel
        # correctly, and the aggregate was a badly-behaved system, because he was the only
        # channel between them. The missing thing was a role, not better behaviour.
        assert "no-conductor" in codes(check(fixtures.infra_tangle()))

    def test_silent_once_a_session_holds_the_role(self):
        assert "no-conductor" not in codes(check(fixtures.tidy_board()))

    def test_silent_for_a_single_session(self):
        assert "no-conductor" not in codes(check(fixtures.solo_board()))


class TestStaleDefaultBranch:
    def test_notes_a_local_default_behind_its_remote(self):
        board = Board.of(fixtures.INFRA, list(fixtures.tidy_board().sessions),
                         facts=RepoFacts(repo_root=fixtures.INFRA, local_default_sha="old",
                                         remote_default_sha="new"))
        assert "stale-default" in codes(check(board))

    def test_says_nothing_when_git_could_not_tell_us(self):
        # Offline, or a repo with no remote. Unknown is not a finding; crying wolf here trains
        # him to skim the whole board.
        board = Board.of(fixtures.INFRA, list(fixtures.tidy_board().sessions),
                         facts=RepoFacts(repo_root=fixtures.INFRA))
        assert "stale-default" not in codes(check(board))


class TestTheQuietCase:
    def test_a_lone_session_in_its_own_worktree_produces_nothing_at_all(self):
        # This is the case that must cost nothing, because it is almost every session. If the
        # board ever speaks here, the tool gets muted and protects nobody.
        assert check(fixtures.solo_board()) == []

    def test_an_empty_board_is_not_an_error(self):
        assert check(Board.of(fixtures.INFRA, [])) == []


class TestOrdering:
    def test_blocks_come_before_warnings_and_warnings_before_notes(self):
        found = check(fixtures.infra_tangle())
        assert [f.rank for f in found] == sorted(f.rank for f in found)
