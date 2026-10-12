"""Observed states, not invented ones.

`infra_tangle()` is the real 2026-09-17 configuration of `acme/infra`, reconstructed
from the tmux pane registry and the four sessions' transcripts: window, pane, session id and
charter are the ones that actually ran. It is the headline fixture because every detector in
this package exists to have fired on it, and "would this have caught that day" should be a
test run rather than an argument.

The costs that day: one session merged `origin/main` into a branch two others were committing
to, without knowing; a second session's working tree held a third's uncommitted work and it
commits with `git add -A`; seventeen commits on one branch split 10/4/3 with three belonging
to nobody; and Daniel spent an hour carrying prompts between windows by hand.
"""

from __future__ import annotations

from sessioncoord.model import Board, CONDUCTOR, Presence, RepoFacts

INFRA = "/home/user/code/acme/infra"
SHARED_BRANCH = "drill-rename-and-trial-tenant"


def presence(session_id, window, pane, pid, workdir=INFRA, branch=SHARED_BRANCH, **kw):
    return Presence(
        session_id=session_id,
        repo_root=INFRA,
        workdir=workdir,
        branch=branch,
        pane=pane,
        window=window,
        claude_pid=pid,
        **kw,
    )


def infra_tangle() -> Board:
    """Four live sessions, one working tree, one branch. `acme/infra`, 2026-09-17 18:52Z."""
    return Board.of(
        INFRA,
        [
            presence(
                "e6803b0d-e527-4449-819c-86ab6d9bc133", "infra:6", "%53", 3157817,
                charter="Trial/freemium witness tier, Debian 13 migration.",
                claims=("tofu/**", "AGENTS.md", ".github/workflows/ci.yml"),
            ),
            presence(
                "00d342e3-6af9-4654-be1e-9f228c8616cf", "infra:8", "%60", 3401077,
                charter="DMARC/SPF tightening and the zone mail posture gate.",
                claims=("docs/manual-config/dns.md", ".github/workflows/ci.yml"),
            ),
            presence(
                "fac45ea9-e980-499a-9957-be5a28526a9f", "infra:10", "%63", 3871034,
                charter="Namecheap DNS backup and drift capture.",
                claims=("docs/manual-config/dns.md",),
            ),
            presence(
                "269bc034-71b1-4df2-a0b0-712dfeeff990", "infra:11", "%67", 4141161,
                charter="Cyber hardening: census, survey, hardening programme.",
                claims=("docs/cyber/**", "docs/inventory.md"),
            ),
        ],
        facts=RepoFacts(
            repo_root=INFRA,
            default_branch="main",
            local_default_sha="aaaaaaa",
            remote_default_sha="aaaaaaa",
            dirty_paths=("docs/inventory.md", "docs/cyber/census.md"),
        ),
    )


def tidy_board() -> Board:
    """What the same four sessions look like after the rule lands: a worktree each."""
    tangle = infra_tangle()
    topics = ["trial-tier", "mail-posture", "dns-backup", "cyber-hardening"]
    fixed = [
        s.with_(workdir=f"{INFRA}/.worktrees/{t}", branch=t, role=CONDUCTOR if i == 3 else None)
        for i, (s, t) in enumerate(zip(tangle.sessions, topics))
    ]
    # The claim collision is deliberately left in place: two sessions still want ci.yml, and a
    # worktree does nothing about that. It is a different failure with a different remedy.
    return Board.of(INFRA, fixed, facts=tangle.facts)


def solo_board() -> Board:
    """The common case, and the one that must cost nothing: one session, alone."""
    return Board.of(
        INFRA,
        [presence("solo", "infra:11", "%67", 4141161,
                  workdir=f"{INFRA}/.worktrees/cyber", branch="cyber")],
        facts=RepoFacts(repo_root=INFRA, local_default_sha="a", remote_default_sha="a"),
    )
