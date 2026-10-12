"""The single ordered queue of things Daniel owes an answer to, across every session here.

Two observed failures shape this module, both from 2026-09-17.

He was asked to approve `@idymqyp3` and to adjudicate `Q-8RJN` -- identifiers that meant
something to the sessions and nothing to him, so he had to go and reconstruct the context
before he could answer. Hence `MIN_SELF_CONTAINED`: an ask that cannot be understood without a
lookup is rejected at the point it is filed, not discovered at the point he reads it.

And with several outstanding he could not tell which came first, which he named as the thing
that drives him crazy. Hence the topological ordering: whatever unblocks the most goes at the
top, and an ask nobody is waiting on sinks.

Pure. Persistence is `store`'s job.
"""

from __future__ import annotations

from typing import Iterable, Sequence

from .model import Ask

KINDS = {"A", "Q"}

# An ask has to carry its own content. This threshold is a blunt proxy for that -- it will not
# catch "Approve the node" dressed up to length -- but it does catch the shape that actually
# failed, which was a bare identifier and a verb.
MIN_SELF_CONTAINED = 40


class AskError(ValueError):
    """Something wrong with an ask, phrased for the session that filed it."""


def add(queue: Sequence[Ask], ask: Ask) -> list[Ask]:
    """Append an ask, refusing the two shapes that cost him time."""
    if ask.kind not in KINDS:
        raise AskError(
            f"kind {ask.kind!r} is not one of {sorted(KINDS)}: A is something the user must go "
            "and do outside the chat, Q is something a typed reply discharges."
        )
    if any(a.tag.upper() == ask.tag.upper() for a in queue):
        raise AskError(f"{ask.tag} is already in the queue; tags must be unique to be citable.")
    if len(ask.text.strip()) < MIN_SELF_CONTAINED:
        raise AskError(
            f"{ask.tag} does not stand on its own: {ask.text!r}. The user does not share "
            "your context -- restate the subject in the ask itself rather than naming an id "
            "they would have to go and look up."
        )
    return list(queue) + [ask]


def _open(queue: Iterable[Ask]) -> list[Ask]:
    return [a for a in queue if a.state == "open"]


def ordered(queue: Iterable[Ask]) -> list[Ask]:
    """Open asks, blockers before the things they block, worst-first within a tier.

    Total over every representable list, which is stronger than it sounds. Keying the working
    maps by tag collapsed duplicates and silently dropped one, so `add()` was the only thing
    standing between a hand-edited or concurrently-clobbered file and a lost ask. Indices are
    used instead, so the output is always a permutation of the input.

    Cycles are broken rather than raised -- two sessions can each believe the other goes first,
    and dropping either would hide a real disagreement -- and every item still appears.
    """
    items = _open(queue)
    by_tag: dict[str, list[int]] = {}
    for i, a in enumerate(items):
        by_tag.setdefault(a.tag.upper(), []).append(i)

    # Index -> indices it waits on. A dependency naming a tag that is absent or already
    # answered is not a block; a duplicated tag blocks on every holder of that tag.
    blockers: dict[int, set[int]] = {}
    for i, a in enumerate(items):
        deps: set[int] = set()
        for d in a.depends_on:
            deps.update(j for j in by_tag.get(d.upper(), []) if j != i)
        blockers[i] = deps

    unblocks: dict[int, int] = {i: 0 for i in range(len(items))}
    for deps in blockers.values():
        for d in deps:
            unblocks[d] += 1

    placed: list[Ask] = []
    remaining = dict(blockers)
    while remaining:
        ready = [i for i, deps in remaining.items() if not (deps & set(remaining))]
        if not ready:  # a cycle; break it on whatever unblocks most, then oldest
            ready = [max(remaining, key=lambda i: (unblocks[i], -i))]
        ready.sort(key=lambda i: (-unblocks[i], items[i].created, items[i].tag, i))
        for i in ready:
            placed.append(items[i])
            remaining.pop(i, None)
    return placed


def _resolve(queue: Sequence[Ask], tag: str) -> Ask:
    """Find an open ask by tag or unique prefix, the way he actually types them."""
    t = tag.strip().upper()
    hits = [a for a in _open(queue) if a.tag.upper() == t]
    if not hits:
        hits = [a for a in _open(queue) if a.tag.upper().startswith(t)]
    if not hits:
        raise AskError(f"no open ask matches {tag!r}.")
    if len(hits) > 1:
        raise AskError(
            f"{tag!r} is ambiguous between {', '.join(sorted(a.tag for a in hits))}; "
            "never guess which one was meant."
        )
    return hits[0]


def answer(queue: Sequence[Ask], tag: str, text: str) -> list[Ask]:
    """Record his reply and close the ask."""
    target = _resolve(queue, tag)
    return [
        a.__class__(**{**a.to_dict(), "state": "answered", "answer": text})
        if a.tag == target.tag else a
        for a in queue
    ]


def dissolve_resolved(queue: Sequence[Ask], tags: Iterable[str]) -> list[Ask]:
    """Retire asks that stopped being questions, without spending his attention on them.

    `Q-8RJN` asked where a session should write, given its inputs existed only on an unpushed
    branch. The moment that branch landed on `main` the question had no content left. Nobody
    should have to tell him about a decision that no longer exists -- but a session must
    nominate the ask explicitly, because silently dropping his queue items would be worse than
    leaving them.
    """
    want = {t.upper() for t in tags}
    return [
        a.__class__(**{**a.to_dict(), "state": "dissolved"})
        if a.tag.upper() in want and a.state == "open" else a
        for a in queue
    ]


def render(queue: Iterable[Ask]) -> str:
    """The queue as he reads it: one list, in the order things actually have to happen.

    Earlier this split into all questions then all errands, which read nicely and was wrong.
    If a question depends on an errand -- "once you have run the deploy, which host wins?" --
    the split printed the question above the errand that gates it, telling him to answer
    something he cannot answer yet. That inverted the queue's one promise, and both PR #3
    reviewers found it independently.

    So the order is `ordered()`'s, unmodified, and the A/Q distinction moves to a per-item
    marker. He still gets the thing that matters about the split -- whether a reply discharges
    it or whether he has to go and do something -- without the ordering lie.
    """
    items = ordered(queue)
    if not items:
        return "No open asks."
    open_tags = {a.tag.upper() for a in items}
    out: list[str] = ["Open asks, in the order they have to happen.",
                      "  [Q] a reply from you discharges it.  "
                      "[A] you have to go and do something outside the chat.", ""]
    for a in items:
        where = f" [{a.window}]" if a.window else ""
        waiting = [d for d in a.depends_on if d.upper() in open_tags]
        blocked = f"  (waiting on {', '.join(waiting)})" if waiting else ""
        out.append(f"  [{a.kind}] {a.tag}{where}  {a.text.strip()}{blocked}")
    return "\n".join(out).rstrip() + "\n"
