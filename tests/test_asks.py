"""The one queue Daniel reads.

Two failures on 2026-09-17 produced this module. He was asked about `@idymqyp3` and `Q-8RJN`
as though he already knew what they were -- context the sessions shared with each other and
not with him. And with several asks outstanding he could not tell which came first: *"You
haven't clarified whether I'm supposed to give you the one-word answer to q-znyn first, or
take something to window 10 first. This is the kind of thing that drives me crazy."*

So the queue enforces two properties: an ask must stand on its own, and the list must be
ordered by what unblocks what.
"""

from __future__ import annotations

import pytest
from sessioncoord.asks import (
    AskError,
    add,
    answer,
    dissolve_resolved,
    ordered,
    render,
)
from sessioncoord.model import Ask


def mk(tag, kind="Q", text="Do the thing, which is a whole sentence about the thing.",
       session_id="s1", window="infra:11", created="2026-09-17T20:00:00", **kw):
    return Ask(tag=tag, kind=kind, text=text, session_id=session_id, window=window,
               created=created, **kw)


class TestAdd:
    def test_rejects_an_ask_that_cannot_stand_on_its_own(self):
        # "Approve @idymqyp3" is exactly the shape that failed: meaningless to the reader.
        with pytest.raises(AskError, match="stand on its own"):
            add([], mk("Q-0001", text="Approve @idymqyp3"))

    def test_accepts_an_ask_that_restates_its_subject(self):
        q = mk("Q-0001", text="Approve the DNS-backup intent node `@idymqyp3`, which argues "
                              "for capturing the apex zone into CI rather than by hand.")
        assert len(add([], q)) == 1

    def test_refuses_a_duplicate_tag(self):
        q = mk("Q-0001")
        with pytest.raises(AskError, match="already"):
            add(add([], q), mk("Q-0001", text="A different question entirely, spelled out."))

    def test_rejects_an_unknown_kind(self):
        with pytest.raises(AskError, match="kind"):
            add([], mk("X-0001", kind="X"))


class TestOrdering:
    def test_an_ask_that_unblocks_others_comes_first(self):
        # Q-ZNYN ("do I merge the PR carrying three sessions' work?") gated everything else
        # that day. It should have been the only thing on his screen.
        gate = mk("Q-ZNYN", text="Push and merge `merge-verify` as one PR carrying three "
                                 "sessions' work?")
        blocked_a = mk("Q-8RJN", text="Where should the DNS-backup session write, given its "
                                      "inputs exist only on an unpushed branch?",
                       depends_on=("Q-ZNYN",))
        blocked_b = mk("A-SXMB", kind="A", text="Run the Ansible play that deploys the witness "
                                                "edge fix to the four production witnesses.",
                       depends_on=("Q-ZNYN",))
        assert [a.tag for a in ordered([blocked_a, blocked_b, gate])][0] == "Q-ZNYN"

    def test_blocked_asks_are_listed_after_what_blocks_them(self):
        gate = mk("Q-AAAA", text="The gating question, spelled out in full here.")
        blocked = mk("Q-BBBB", text="The blocked question, also spelled out in full.",
                     depends_on=("Q-AAAA",))
        assert [a.tag for a in ordered([blocked, gate])] == ["Q-AAAA", "Q-BBBB"]

    def test_independent_asks_keep_their_creation_order(self):
        first = mk("Q-AAAA", created="2026-09-17T10:00:00",
                   text="The earlier independent question, in full.")
        second = mk("Q-BBBB", created="2026-09-17T11:00:00",
                    text="The later independent question, in full.")
        assert [a.tag for a in ordered([second, first])] == ["Q-AAAA", "Q-BBBB"]

    def test_answered_asks_drop_out(self):
        done = mk("Q-AAAA", state="answered", text="Already dealt with, in full.")
        live = mk("Q-BBBB", text="Still outstanding, spelled out in full.")
        assert [a.tag for a in ordered([done, live])] == ["Q-BBBB"]

    def test_a_dependency_cycle_does_not_hang_or_drop_anything(self):
        # Two sessions can each believe the other goes first. Report both rather than
        # deadlocking or silently losing one. Asserts the exact list, not a set: the PR #3
        # review pointed out that a set comparison cannot catch a duplicate, so the old
        # version of this test would have passed on output that dropped or doubled an item.
        a = mk("Q-AAAA", text="First of a mutually blocking pair, in full.",
               depends_on=("Q-BBBB",))
        b = mk("Q-BBBB", text="Second of a mutually blocking pair, in full.",
               depends_on=("Q-AAAA",))
        got = [x.tag for x in ordered([a, b])]
        assert sorted(got) == ["Q-AAAA", "Q-BBBB"] and len(got) == 2

    def test_the_output_is_always_a_permutation_of_the_open_input(self):
        # Totality, which the tag-keyed implementation did not have. A duplicated tag -- from a
        # hand-edited file, or from the lost-update race the queue lock now prevents -- silently
        # collapsed two asks into one, and `add()` was the only thing standing in the way.
        dup = [mk("Q-AAAA", text="One ask that shares a tag with the next one."),
               mk("Q-AAAA", text="A second ask that wrongly shares that tag."),
               mk("Q-BBBB", text="An unrelated third ask, spelled out in full.")]
        assert len(ordered(dup)) == 3

    def test_a_dependency_on_something_already_answered_is_not_a_block(self):
        done = mk("Q-AAAA", state="answered", text="Already answered, in full.")
        freed = mk("Q-BBBB", text="Was waiting on the above, now free.",
                   depends_on=("Q-AAAA",))
        assert [a.tag for a in ordered([done, freed])] == ["Q-BBBB"]


class TestAnswer:
    def test_records_the_answer_and_closes_the_ask(self):
        q = mk("Q-ZNYN", text="Push and merge the PR carrying three sessions' work?")
        got = answer(add([], q), "Q-ZNYN", "go")
        assert got[0].state == "answered" and got[0].answer == "go"

    def test_resolves_a_shortened_tag_the_way_he_actually_types_them(self):
        # chat-voice: "he types back whatever case he likes, and often only the first two or
        # three characters".
        q = mk("Q-ZNYN", text="Push and merge the PR carrying three sessions' work?")
        assert answer(add([], q), "q-zn", "go")[0].state == "answered"

    def test_refuses_an_ambiguous_prefix_rather_than_guessing(self):
        qs = add(add([], mk("Q-ZNYN", text="Merge the PR carrying three sessions' work?")),
                 mk("Q-ZAAA", text="Adopt the gate list as data rather than a hand-edited line?"))
        with pytest.raises(AskError, match="ambiguous"):
            answer(qs, "q-z", "go")

    def test_refuses_an_unknown_tag(self):
        q = mk("Q-ZNYN", text="Merge the PR carrying three sessions' accumulated work?")
        with pytest.raises(AskError, match="no open ask"):
            answer(add([], q), "q-xx", "go")


class TestDissolve:
    def test_an_ask_whose_blocker_was_answered_can_be_retired_without_him(self):
        # Q-8RJN dissolved the moment merge-verify landed: the session had asked where to
        # write, and the answer stopped being a decision once the branch was on main. Nobody
        # should have to tell him about a question that no longer exists.
        gate = mk("Q-AAAA", state="answered", text="The gate, in full.")
        dependent = mk("Q-BBBB", text="Only meaningful while the gate was open.",
                       depends_on=("Q-AAAA",))
        got = dissolve_resolved([gate, dependent], {"Q-BBBB"})
        assert next(a for a in got if a.tag == "Q-BBBB").state == "dissolved"

    def test_leaves_asks_nobody_nominated_alone(self):
        gate = mk("Q-AAAA", state="answered", text="The gate, in full.")
        dependent = mk("Q-BBBB", text="Still a real question.", depends_on=("Q-AAAA",))
        got = dissolve_resolved([gate, dependent], set())
        assert next(a for a in got if a.tag == "Q-BBBB").state == "open"


class TestRender:
    def test_shows_the_window_so_he_knows_where_the_answer_goes(self):
        out = render([mk("Q-ZNYN", window="infra:11",
                         text="Push and merge the PR carrying three sessions' work?")])
        assert "infra:11" in out and "Q-ZNYN" in out

    def test_marks_what_he_can_type_at_and_what_he_must_go_and_do(self):
        # The A/Q distinction is the difference between fifteen minutes and one word, so it
        # has to be visible per item.
        out = render([mk("A-SXMB", kind="A",
                         text="Run the Ansible play that deploys the witness edge fix.")])
        assert "[A] A-SXMB" in out

    def test_never_prints_a_dependent_above_the_thing_that_blocks_it(self):
        # Found by both PR #3 reviewers. Rendering used to list every question and then every
        # errand, so a question waiting on an errand appeared ABOVE it -- telling him to answer
        # something he cannot answer until he has done the errand. That inverts the queue's one
        # promise, and it only shows up when the dependency crosses kinds.
        gate = mk("A-GATE", kind="A",
                  text="Run the deploy that decides which host actually serves the zone.")
        dependent = mk("Q-CHLD", kind="Q", depends_on=("A-GATE",),
                       text="Once the deploy has run, which host should win the tie?")
        out = render([dependent, gate])
        assert out.index("A-GATE") < out.index("Q-CHLD")

    def test_flags_a_dependent_as_waiting_so_he_can_skip_it(self):
        gate = mk("A-GATE", kind="A", text="Run the deploy that settles the question below.")
        dependent = mk("Q-CHLD", kind="Q", depends_on=("A-GATE",),
                       text="Once the deploy has run, which host should win the tie?")
        assert "waiting on A-GATE" in render([dependent, gate])

    def test_does_not_claim_a_dependent_is_blocked_once_its_blocker_is_answered(self):
        gate = mk("A-GATE", kind="A", state="answered",
                  text="Run the deploy that settles the question below.")
        freed = mk("Q-CHLD", kind="Q", depends_on=("A-GATE",),
                   text="Now that the deploy has run, which host should win the tie?")
        assert "waiting on" not in render([gate, freed])

    def test_an_empty_queue_says_so_rather_than_printing_a_header(self):
        assert render([]).strip() == "No open asks."
