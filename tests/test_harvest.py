"""What a session leaves behind when it closes.

Daniel, 2026-09-17, on why the coordination round still wasn't enough: *"The major work
products get captured, but minor details are left dangling, creating hygiene problems."* Window
8 closed with a tick that was done but still open, a contradiction between two sessions
unfiled, and a Workspace setting he had changed recorded nowhere.

The honest limit is pinned here too. An earlier attempt at this reported all eleven of one
session's tags as undischarged, which was obviously wrong -- it counted citations as mints. So
this is a net that narrows a thousand messages to a handful, and it is explicitly not a
verdict. A harvest that claims to decide would be trusted, and it would be wrong.
"""

from __future__ import annotations

from sessioncoord.harvest import Turn, candidates, minted_tags


def a(text):
    return Turn(role="assistant", text=text)


def u(text):
    return Turn(role="user", text=text)


MINT = "  [ P-YV9A ]  Path traversal in `acme/did-webs`, a public repo, reachable today."


class TestMintedTags:
    def test_finds_a_tag_in_the_padded_minting_form(self):
        assert [t.tag for t in minted_tags([a(MINT)])] == ["P-YV9A"]

    def test_captures_the_text_so_the_candidate_stands_on_its_own(self):
        assert "did-webs" in minted_tags([a(MINT)])[0].text

    def test_a_tight_citation_is_not_a_mint(self):
        # This is the bug that made the first version report everything as undischarged.
        turns = [a(MINT), a("As noted, [P-YV9A] the traversal is still open.")]
        assert [t.tag for t in minted_tags(turns)] == ["P-YV9A"]

    def test_ignores_a_bracketed_markdown_link(self):
        assert minted_tags([a("See [acme/infra#69](https://github.com/x) for detail.")]) == []

    def test_reads_the_kind_off_the_prefix(self):
        turns = [a("  [ A-2J3Z ]  Check the inactive-account toggle in the Google console.")]
        assert minted_tags(turns)[0].kind == "A"


class TestCandidates:
    def test_a_question_daniel_never_mentioned_is_a_candidate(self):
        turns = [a("  [ Q-ZNYN ]  Push and merge the PR carrying three sessions' work?")]
        assert [c.tag for c in candidates(turns)] == ["Q-ZNYN"]

    def test_a_question_daniel_answered_drops_out(self):
        # He typed "go" at Q-ZNYN. Whatever else is loose, that one is not.
        turns = [
            a("  [ Q-ZNYN ]  Push and merge the PR carrying three sessions' work?"),
            u("go on q-znyn"),
        ]
        assert candidates(turns) == []

    def test_matches_the_shortened_lowercase_form_he_actually_types(self):
        turns = [a("  [ Q-ZNYN ]  Push and merge the PR carrying three sessions' work?"),
                 u("q-zn yes")]
        assert candidates(turns) == []

    def test_an_errand_he_reported_doing_drops_out(self):
        turns = [a("  [ A-2J3Z ]  Check the inactive-account toggle in Google."),
                 u("did the A-2J3Z check, toggle was off")]
        assert candidates(turns) == []

    def test_findings_are_candidates_because_they_want_a_tick_not_an_answer(self):
        # F/P/R never needed a reply, so "Daniel never mentioned it" is the normal case. They
        # are loose because nothing durable recorded them, which is what window 8 got wrong.
        turns = [a(MINT)]
        c = candidates(turns)[0]
        assert c.kind == "P" and "tick" in c.suggestion

    def test_asks_are_routed_to_the_queue_rather_than_to_a_tick(self):
        turns = [a("  [ Q-ZNYN ]  Push and merge the PR carrying three sessions' work?")]
        assert "ask" in candidates(turns)[0].suggestion

    def test_the_least_discussed_candidates_come_first(self):
        turns = [
            a("  [ P-AAAA ]  Mentioned once and never again, so probably forgotten."),
            a("  [ P-BBBB ]  Discussed at length below, so more likely already handled."),
            a("More on [P-BBBB] here."),
            a("And again [P-BBBB] here."),
        ]
        assert [c.tag for c in candidates(turns)][0] == "P-AAAA"

    def test_reports_reference_counts_so_a_reader_can_overrule_the_ordering(self):
        turns = [a("  [ P-BBBB ]  A finding that was then discussed."),
                 a("More on [P-BBBB].")]
        assert candidates(turns)[0].references == 1

    def test_a_session_that_minted_nothing_harvests_nothing(self):
        assert candidates([a("Ordinary prose with no tags in it at all.")]) == []


class TestItIsANetNotAVerdict:
    def test_every_candidate_carries_the_caveat_in_its_own_record(self):
        # So it cannot be quoted as a decision after being copied out of context.
        assert candidates([a(MINT)])[0].verdict == "candidate"
