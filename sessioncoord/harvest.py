"""Loose ends at session close: the net that narrows a thousand messages to a handful.

Daniel's complaint on 2026-09-17 was that coordination captured the major work products and
left the minor ones dangling. Window 8 closed carrying a tick that was done but still open, a
contradiction between two sessions that neither had seen, and a Workspace change he had made
that was recorded nowhere. None of those needed judgement to *find*; they needed somebody to
look, and nothing ran at close.

The limit is deliberate and stated in the output. An earlier attempt reported all eleven of one
session's tags as undischarged, because it counted tight-form citations as fresh mints. This
version fixes that particular bug and still does not claim to decide: `verdict` is the constant
string "candidate", so a record cannot be quoted as a conclusion once it is copied out of
context. Narrowing is the whole job.

Pure over `Turn`s; `load_transcript` is the only part that reads a file.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Iterable, Sequence

# The padded minting form from chat-voice: two-space indent, spaces inside the brackets. That
# padding is exactly what distinguishes a definition from the tight `[P-YV9A]` citation form,
# which is why it can be matched structurally rather than guessed at.
MINT_RE = re.compile(r"^\s{1,4}\[\s([A-Z])-([0-9A-HJKMNP-TV-Z]{4})\s\]\s+(.*)$", re.MULTILINE)
CITE_RE = re.compile(r"\[([A-Z])-([0-9A-HJKMNP-TV-Z]{4})\]")

ASK_KINDS = {"A", "Q"}


@dataclass(frozen=True)
class Turn:
    role: str  # "user" | "assistant"
    text: str


@dataclass(frozen=True)
class Tag:
    tag: str
    kind: str
    text: str


@dataclass(frozen=True)
class Candidate:
    tag: str
    kind: str
    text: str
    references: int
    suggestion: str
    verdict: str = "candidate"


def minted_tags(turns: Iterable[Turn]) -> list[Tag]:
    """Tags this session defined, in mint order, each with the sentence that defined it."""
    out: list[Tag] = []
    seen: set[str] = set()
    for t in turns:
        if t.role != "assistant":
            continue
        for kind, suffix, text in MINT_RE.findall(t.text):
            tag = f"{kind}-{suffix}"
            if tag in seen:
                continue
            seen.add(tag)
            out.append(Tag(tag=tag, kind=kind, text=text.strip()))
    return out


def _daniel_touched(tag: str, turns: Sequence[Turn]) -> bool:
    """Did he engage with this tag in his own words?

    Matched on the first three characters after the prefix as well as the whole thing, because
    chat-voice says he types back "often only the first two or three characters", in whatever
    case he likes. A false positive here loses a candidate; a false negative just leaves one
    more line for a human to skim, so this leans toward matching.
    """
    kind, suffix = tag.split("-")
    pattern = re.compile(rf"\b{re.escape(kind)}-{re.escape(suffix[:2])}[0-9A-Z]{{0,2}}\b",
                         re.IGNORECASE)
    return any(pattern.search(t.text) for t in turns if t.role == "user")


def candidates(turns: Sequence[Turn]) -> list[Candidate]:
    """Loose ends worth a human's eye, least-discussed first.

    An ask Daniel engaged with is discharged, whatever came of it -- he has it. A finding is a
    candidate regardless, because nothing about being discussed makes it durable; the whole
    point is that it should be a tick before the window closes.
    """
    turns = list(turns)
    out: list[Candidate] = []
    for i, tag in enumerate(minted_tags(turns)):
        if _daniel_touched(tag.tag, turns):
            continue
        # Tight-form citations only. The padded mint does not match CITE_RE, which is the bug
        # the earlier version had -- it counted a definition as its own discharge.
        refs = sum(
            len([m for m in CITE_RE.findall(t.text) if f"{m[0]}-{m[1]}" == tag.tag])
            for t in turns
        )
        if tag.kind in ASK_KINDS:
            suggestion = (
                "file in the ask queue (`session-coord ask add`) so it survives this window"
            )
        else:
            suggestion = "record it in your task tracker so it outlives this session"
        out.append(Candidate(tag=tag.tag, kind=tag.kind, text=tag.text,
                             references=refs, suggestion=suggestion))
    return sorted(out, key=lambda c: (c.references, c.tag))


def render(cands: Sequence[Candidate]) -> str:
    """The harvest as a human reads it, caveat included rather than implied."""
    if not cands:
        return "Nothing loose: every tag this session minted was either answered or cited.\n"
    lines = [
        f"{len(cands)} loose end(s). This is a net, not a verdict -- it narrows the field to",
        "something readable and does not decide anything. Read them.",
        "",
    ]
    for c in cands:
        seen = "never referred to again" if not c.references else f"{c.references} later mention(s)"
        lines.append(f"  {c.tag}  ({seen})")
        lines.append(f"      {c.text}")
        lines.append(f"      -> {c.suggestion}")
        lines.append("")
    return "\n".join(lines)


def load_transcript(path: str, limit: int = 4000) -> list[Turn]:
    """Read a Claude Code transcript into turns. Tolerant: a bad line is skipped, not fatal."""
    turns: list[Turn] = []
    try:
        fh = open(path)
    except OSError:
        return turns
    with fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            # The docstring promised a bad line is skipped; a valid JSON scalar or array was
            # not "bad" to json and then raised AttributeError here, aborting the whole
            # harvest -- which runs at SessionEnd, where nobody would see why it stopped.
            if not isinstance(rec, dict):
                continue
            role = rec.get("type")
            if role not in ("user", "assistant"):
                continue
            message = rec.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                text = "\n".join(
                    b.get("text", "") for b in content
                    if isinstance(b, dict) and b.get("type") == "text"
                )
            else:
                continue
            if text.strip():
                turns.append(Turn(role=role, text=text))
            if len(turns) >= limit:
                break
    return turns
