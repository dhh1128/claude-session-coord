"""argparse wiring over the rest of the package. The surface hooks and the skill call.

Two contracts worth stating because callers depend on them.

Exit codes: 0 clean, 1 warnings, 2 a blocking finding, and the PreToolUse gate turns a 2 into
a refused tool call. Anything else is the CLI itself failing.

Silence: `--quiet` must print nothing at all when there is nothing to say. One session alone in
a repo is almost every session, and a tool that speaks in the common case gets muted -- after
which it protects nobody. `repo-status -q` already works this way, for the same reason.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import replace
from datetime import datetime, timezone

from . import asks as asks_mod
from . import comms, detect, gitio, harvest, locks, store, tmuxio
from . import inbox as inbox_mod
from .model import Ask, Board, CONDUCTOR, Presence


# First line of `inbox --ids`, so a caller takes bodies and ids from ONE snapshot and strips
# the machine-readable part before showing the rest to a reader.
IDS_PREFIX = "#ids "


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _resolve_repo(args) -> str | None:
    if getattr(args, "repo", None):
        # Through gitio.repo_root, NOT taken literally. `--repo "$PWD"` from inside a worktree
        # otherwise keys state on the worktree, so the session registers on a private board of
        # one, sees no peers, and is invisible to them -- silent isolation reached by using the
        # tool as documented. Found by the tagged session, in its own code first.
        explicit = os.path.realpath(os.path.expanduser(args.repo))
        return gitio.repo_root(explicit) or explicit
    # --repo-of names a PATH whose repo we want, which is not always the one containing cwd:
    # `git -C /other/repo add -A` issued from here writes to a different repository's board,
    # and asking this one answered about the wrong tree -- or exited clean, which reads as safe.
    target = getattr(args, "repo_of", None)
    if target:
        found = gitio.repo_root(os.path.expanduser(target))
        if found:
            return found
    return gitio.repo_root(os.getcwd())


def _live_board(repo: str) -> Board:
    """Sweep, then build the board from what is left. The sweep is why nothing needs tidying.

    Under the presence lock, because the sweep MUTATES: a `board` or `check` running
    concurrently with `conductor claim` could delete a record in the middle of that claim's
    read-modify-save and have it written straight back. Read-only on contention rather than
    failing -- answering from a board that is one sweep stale is much better than not
    answering, since every caller here is trying to find out who else is present.
    """
    try:
        with locks.hold(repo, "presence", wait=0.5, owner="board"):
            # Snapshot INSIDE the lock. Taken outside, a session registering between the
            # snapshot and the lock is absent from the map and gets swept -- deleting a live
            # session at the moment it starts, which is the worst possible time.
            store.sweep(repo, tmuxio.claude_pids(), tmuxio.server_pid())
    except locks.Busy:
        pass
    return Board.of(repo, _live(store.load_all(repo)), facts=gitio.facts(repo))


def _live_table():
    """The tmux pane table `_live` reads. Its own function so the test suite can replace it."""
    return tmuxio.panes()


def _live(records, table=None):
    """Records with `window` and `title` read from tmux NOW, not from when they registered.

    Windows renumber when Daniel closes one (`renumber-windows on`, which he keeps because
    double-digit windows are awkward), so a stored window is a guess about the past. On
    2026-10-10 a session in infra:6 introduced itself as infra:7, and another asked after a
    response "from infra:7" while running in it. Anything shown to a session or to Daniel goes
    through here. A pane holding a different claude than the record's is left as recorded:
    that pane is somebody else now, and the sweep is what retires the record.
    """
    table = _live_table() if table is None else table
    # None is "could not ask tmux", and the stored values are the best there is. An EMPTY table
    # is an answer -- no panes at all -- and every record in it is stale (the fix-diff pass on
    # PR #35, finding 3).
    if table is None:
        return list(records)
    out = []
    for p in records:
        info = table.get(p.pane or "")
        if info and (p.claude_pid is None or info["claude_pid"] == p.claude_pid):
            p = p.with_(window=info["window"], title=info.get("title") or p.title)
        else:
            # Its pane is gone or holds someone else, so its stored window is a number another
            # session may hold now. Kept, `msg <window>` matched it and filed into the dead
            # session's inbox (the hostile pass on PR #35, finding 2).
            # The title goes too: kept, `msg <old title>` still filed into the dead inbox
            # (finding 2). The session-id prefix is what names it now.
            # And the pane: kept, `msg %2` still matched the dead record and filed into its inbox
            # (the third pass on PR #35). A pane id addressed directly now reaches whoever
            # is in that pane, by the unregistered path.
            p = p.with_(window=None, title="", pane=None)
        out.append(p)
    return out


def _render_board(board: Board) -> str:
    lines = [f"{len(board.sessions)} sessions in {board.repo_root}"]
    for s in board.sessions:
        role = "  [conductor]" if s.role == CONDUCTOR else ""
        where = s.workdir
        if where.startswith(board.repo_root):
            where = "." + where[len(board.repo_root):] or "."
        lines.append(f"  {s.label:<14} {where}  ({s.branch or 'detached'}){role}")
        lines.append(f"      address as: {s.name}")
        if s.charter:
            lines.append(f"      {s.charter}")
        if s.claims:
            lines.append(f"      claims: {', '.join(s.claims)}")
    return "\n".join(lines) + "\n"


def _render_findings(findings) -> str:
    out = []
    for f in findings:
        out.append(f"[{f.severity}] {f.code}: {f.summary}")
        if f.detail:
            out.append(f"    {f.detail}")
        if f.remedy:
            out.append(f"    -> {f.remedy}")
    return "\n".join(out) + "\n" if out else ""


def _rc(findings) -> int:
    if any(f.severity == "block" for f in findings):
        return 2
    return 1 if findings else 0


# ---- commands ------------------------------------------------------------------------

def cmd_register(args) -> int:
    repo = _resolve_repo(args)
    if repo is None:
        print("session-coord: not a git repo; nothing registered.", file=sys.stderr)
        return 0
    # Validate BEFORE anything destructive. `store.save` validates too, but by then the
    # superseded record has already been unlinked, so a malformed id deleted a live session's
    # registration and then reported an error -- leaving the pane unregistered and invisible,
    # which is the one state this tool must never produce.
    try:
        store.check_name(args.session, "session id")
    except store.UnsafeName as e:
        print(f"session-coord: {e}", file=sys.stderr)
        return 2

    # The whole presence lifecycle runs under the same lock `conductor claim` takes. Without
    # it, a SessionStart that loaded role=None before a claim could save afterwards and erase
    # the new conductor -- leaving `conductor claim`'s success message false, which is worse
    # than the claim simply failing. A /clear races the same way against its own supersede.
    try:
        with locks.hold(repo, "presence", wait=args.wait, owner=f"register {args.session[:8]}"):
            return _register_locked(args, repo)
    except locks.Busy as e:
        print(f"session-coord: {e}", file=sys.stderr)
        return 75


def _title_of(pane: str | None) -> str:
    table = tmuxio.panes() if pane else None
    return ((table or {}).get(pane or "") or {}).get("title", "")


def _register_locked(args, repo: str) -> int:
    server = tmuxio.server_pid()
    store.sweep(repo, tmuxio.claude_pids(), server)
    existing = {p.session_id: p for p in store.load_all(repo)}
    prior = existing.get(args.session)
    # Explicit argument, then what this session was already recorded in, then the environment.
    # Prior beats the environment because a re-register from an ordinary CLI call (the
    # documented charter/claim update) inherits whatever pane that shell happens to sit in,
    # which would silently move the record to a pane the session is not in and make the next
    # sweep delete it.
    pane = args.pane or (prior.pane if prior else None) or os.environ.get("TMUX_PANE")

    # A /clear mints a NEW session id while the same claude process stays in the same pane.
    # Without this, the old record is still genuinely live by the liveness predicate, the new
    # one is added beside it, and one conversation shows up as two sessions in one working
    # tree -- which the shared-tree detector then reports as a blocking collision with itself.
    # Inherit the departing record rather than merely deleting it, so a charter and claims
    # survive the clear that Daniel did not intend to reset.
    superseded = [
        p for p in existing.values()
        if p.session_id != args.session and p.pane == pane and pane
        and p.claude_pid is not None and p.claude_pid == args.pid
    ]
    for old in superseded:
        # NO MAIL REBIND HERE, and its absence is deliberate rather than an oversight.
        #
        # A /clear does strand unread mail: it is filed against the departed session id, so
        # `inbox` never returns it. That is a real gap, recorded on PR #11 as `P-R1Z5`, and it
        # predates the delivery hook.
        #
        # A rebind was written here and removed. It could not fail safely. Registration must
        # never fail over mail -- an unregistered pane is invisible to every peer, which is
        # strictly worse -- so a failed rebind fell through to this `remove` and orphaned the
        # mail it existed to save. And resolving the recipient before taking the inbox lock let
        # a concurrent /clear file a message under an id this loop had just deleted. Two
        # defects, both inside the fix, both found in the round after it landed.
        #
        # Carrying mail across an id change needs a decision, not a patch: either the old
        # record survives until the mail has moved, or registration retries, or `inbox`
        # resolves superseded ids at read time. Leaving the gap is worse than nothing only if
        # you believe a rebind that silently loses mail is better than one that visibly never
        # ran. See A-W552.
        store.remove(repo, old.session_id)
    inherited = prior or (superseded[0] if superseded else None)

    cwd = os.path.realpath(os.path.expanduser(args.cwd or os.getcwd()))
    p = Presence(
        session_id=args.session,
        repo_root=repo,
        workdir=gitio.workdir(cwd),
        branch=gitio.branch(cwd),
        pane=pane,
        window=args.window or tmuxio.window_of(pane or "") or (
            inherited.window if inherited else None),
        title=_title_of(pane) or (inherited.title if inherited else ""),
        # A re-register without --pid must not blank the pid. The documented charter/claim
        # update in SKILL.md omits it, so this path is the common one, and a null pid makes
        # the next sweep treat a running session as dead.
        claude_pid=args.pid if args.pid is not None else (
            inherited.claude_pid if inherited else None),
        charter=args.charter or (inherited.charter if inherited else ""),
        claims=tuple(args.claim) if args.claim else (inherited.claims if inherited else ()),
        role=inherited.role if inherited else None,
        started=inherited.started if inherited else _now(),
        updated=_now(),
        transcript=args.transcript or (inherited.transcript if inherited else ""),
        tmux_server=server or "",
    )
    try:
        store.save(p)
    except store.UnsafeName as e:
        print(f"session-coord: {e}", file=sys.stderr)
        return 2
    # Registration is a snapshot. If the recorded directory is not the tree this command is
    # actually running in, the board is confidently wrong -- and the gate acts on the board, so
    # a session that moved with `cd` gets refused while believing it complied. Say so here, at
    # the only moment we can see both values at once.
    live_tree = gitio.workdir(os.getcwd())
    if live_tree and live_tree.rstrip("/") != p.workdir.rstrip("/"):
        print(
            f"session-coord: recorded as working in {p.workdir}, but this command is running in "
            f"{live_tree}.\n"
            "  The board -- and the PreToolUse gate that reads it -- will treat you as being in "
            "the recorded one.\n"
            "  If you moved with `cd`, that is why: use the EnterWorktree tool instead, which "
            "moves the session itself, then re-register.",
            file=sys.stderr)

    board = Board.of(repo, _live(store.load_all(repo)), facts=gitio.facts(repo))
    findings = detect.check(board)
    if len(board.sessions) > 1 or findings:
        sys.stdout.write(_render_board(board))
        sys.stdout.write(_render_findings(findings))
    return 0


def cmd_board(args) -> int:
    repo = _resolve_repo(args)
    if repo is None:
        print("session-coord: not a git repo.", file=sys.stderr)
        return 0
    board = _live_board(repo)
    if args.json:
        print(json.dumps([json.loads(s.to_json()) for s in board.sessions], indent=2))
        return 0
    if args.quiet and len(board.sessions) < 2:
        return 0
    sys.stdout.write(_render_board(board))
    return 0


def cmd_check(args) -> int:
    repo = _resolve_repo(args)
    if repo is None:
        print("session-coord: not a git repo.", file=sys.stderr)
        return 0
    findings = detect.check(_live_board(repo))
    if args.workdir:
        # Scope to the caller's own tree. The PreToolUse gate asks this question on behalf of
        # ONE session, and repo-wide findings would refuse a session sitting safely in its own
        # worktree because two OTHER sessions share the main checkout -- denying exactly the
        # sessions that followed the rule. A finding with no `workdirs` is repo-wide and still
        # applies to everyone.
        # Through gitio.workdir, because registration stores the worktree ROOT while a hook's
        # cwd can be any subdirectory of it. Comparing the raw path found no match, dropped the
        # shared-tree blocker, and allowed the staging command -- fail-open, again.
        here = gitio.workdir(os.path.expanduser(args.workdir)).rstrip("/")
        findings = [f for f in findings if not f.workdirs or here in f.workdirs]
    if args.json:
        print(json.dumps([{"code": f.code, "severity": f.severity, "summary": f.summary,
                           "detail": f.detail, "remedy": f.remedy,
                           "sessions": list(f.sessions),
                           "workdirs": list(f.workdirs)} for f in findings], indent=2))
        return _rc(findings)
    rc = _rc(findings)
    # `--quiet` says "only blocking findings", so filter what is RENDERED while keeping the
    # full list for the exit code. Printing a block's accompanying warnings under --quiet broke
    # the option's own contract, and the gate is the caller that relies on it being terse.
    shown = [f for f in findings if f.severity == "block"] if args.quiet else findings
    if shown:
        sys.stdout.write(_render_findings(shown))
    # Rides on `check` rather than being its own command, because this is what a session
    # already runs when orienting. Never under --quiet: that is the PreToolUse gate's contract
    # for "blocking findings only", and anything else there is noise on every tool call.
    if not args.quiet:
        who = _me(repo)
        if who:
            # `check` is the orientation command and a PreToolUse dependency. It must survive a
            # damaged budget log: raising here turned an unreadable file into a traceback from
            # the one command a session runs to find out what is wrong.
            try:
                log = store.load_sends(repo)
            except store.UnreadableSends as e:
                print(f"comms budget: unavailable -- {e}")
                log = None
            if log is not None:
                line = comms.budget_line(log, who.session_id, time.time())
                if line:
                    print(line)
    return rc


def cmd_deregister(args) -> int:
    """Remove one session's record, by id. What SessionEnd calls; a sweep cannot do it."""
    repo = _resolve_repo(args)
    if repo is None:
        return 0
    try:
        with locks.hold(repo, "presence", wait=args.wait, owner="deregister"):
            if store.remove(repo, args.session):
                print(f"deregistered {args.session}")
    except locks.Busy as e:
        print(f"session-coord: {e}", file=sys.stderr)
        return 75
    except store.UnsafeName as e:
        print(f"session-coord: {e}", file=sys.stderr)
        return 2
    return 0


def cmd_sweep(args) -> int:
    repo = _resolve_repo(args)
    if repo is None:
        return 0
    try:
        with locks.hold(repo, "presence", wait=args.wait, owner="sweep"):
            dropped = store.sweep(repo, tmuxio.claude_pids(), tmuxio.server_pid())
    except locks.Busy as e:
        print(f"session-coord: {e}", file=sys.stderr)
        return 75
    for p in dropped:
        print(f"dropped {p.session_id} ({p.label}) -- session is gone")
    return 0


def cmd_conductor(args) -> int:
    repo = _resolve_repo(args)
    if repo is None:
        print("session-coord: not a git repo.", file=sys.stderr)
        return 0
    if args.action == "show":
        holder = next((p for p in _live(store.load_all(repo)) if p.role == CONDUCTOR), None)
        print(f"conductor: {holder.label}" if holder
              else "conductor: nobody holds it. Claim it if you have the widest view here.")
        return 0
    if args.action == "claim" and not args.session:
        print("session-coord: `conductor claim` needs --session.", file=sys.stderr)
        return 2
    # The role is one snapshot followed by several writes, so it needs the same treatment as
    # the ask queue: two sessions could each read "no conductor", each set itself, and both
    # records end up marked -- after which `Board.conductor` silently picks one and peers
    # route the same question to different arbiters. Two conductors is worse than none.
    try:
        with locks.hold(repo, "presence", wait=args.wait, owner=f"conductor {args.action}"):
            records = store.load_all(repo)
            if args.action == "release":
                for p in records:
                    if p.role == CONDUCTOR:
                        store.save(p.with_(role=None, updated=_now()))
                print("conductor: released.")
                return 0
            if not any(p.session_id == args.session for p in records):
                print(f"session-coord: {args.session} is not registered here.", file=sys.stderr)
                return 2
            for p in records:
                want = CONDUCTOR if p.session_id == args.session else None
                if p.role != want:
                    store.save(p.with_(role=want, updated=_now()))
    except locks.Busy as e:
        print(f"session-coord: {e}", file=sys.stderr)
        return 75
    print(f"conductor: {args.session}. Peers route sequencing questions here, not to Daniel.")
    return 0


def cmd_ask(args) -> int:
    repo = _resolve_repo(args)
    if repo is None:
        print("session-coord: not a git repo.", file=sys.stderr)
        return 0
    # argparse cannot express "required for these actions only", so validate here rather than
    # letting a missing value reach .strip() on None and surface as a traceback.
    missing = {
        "add": [n for n, v in (("--tag", args.tag), ("--kind", args.kind),
                               ("--text", args.text)) if not v],
        "answer": [n for n, v in (("--tag", args.tag),) if not v],
        "dissolve": [n for n, v in (("--tag", args.tag),) if not v],
        "list": [],
    }[args.action]
    if missing:
        print(f"session-coord: `ask {args.action}` needs {', '.join(missing)}.", file=sys.stderr)
        return 2

    if args.action == "list":  # read-only; no lock, so a list never waits on a writer
        try:
            sys.stdout.write(asks_mod.render(store.load_asks(repo)))
        except store.UnreadableQueue as e:
            print(f"session-coord: {e}", file=sys.stderr)
            return 2
        return 0
    try:
        # The whole load/transform/save under one lock. An atomic rename stops a torn read; it
        # does nothing about two sessions both loading the same list, each appending, and the
        # second save dropping the first one's ask. Both PR #3 reviewers found that.
        with locks.hold(repo, "asks", wait=args.wait, owner=f"ask {args.action}"):
            # Refuse rather than overwrite. The whole point of UnreadableQueue is that this
            # transaction would otherwise save a fresh list over a file it failed to parse.
            queue = store.load_asks(repo)
            if args.action == "add":
                ask = Ask(tag=args.tag, kind=args.kind, text=args.text,
                          session_id=args.session or "", window=args.window or "",
                          created=_now(), depends_on=tuple(args.depends_on or ()))
                store.save_asks(repo, asks_mod.add(queue, ask))
                print(f"filed {args.tag}")
            elif args.action == "answer":
                store.save_asks(repo, asks_mod.answer(queue, args.tag, args.text or ""))
                print(f"answered {args.tag}")
            else:
                store.save_asks(repo, asks_mod.dissolve_resolved(queue, [args.tag]))
                print(f"dissolved {args.tag}")
    except store.UnreadableQueue as e:
        print(f"session-coord: {e}\nRefusing to mutate it -- a write here would replace the "
              "asks it could not read. Repair or delete the file, then retry.", file=sys.stderr)
        return 2
    except asks_mod.AskError as e:
        print(f"session-coord: {e}", file=sys.stderr)
        return 2
    except locks.Busy as e:
        print(f"session-coord: {e}", file=sys.stderr)
        return 75
    return 0



class _Doorbell:
    """Reserve a doorbell, ring it, and give it back if the ring does not happen.

    ONE GATE FOR BOTH SEND PATHS, and the reservation, the decision and the ring all sit inside
    a single estate-wide lock. Three separate defects made this a class rather than two inline
    blocks:

    * The check and the ring were not atomic, so two `msg` calls from one pane could each see
      fewer than three sends and both ring. `O_APPEND` stops log lines being lost; it does not
      make read-then-decide-then-act atomic.
    * The budget lived only in the registered-recipient branch, so a registered sender
      addressing an unregistered target or a bare pane typed directly with no accounting at
      all -- a complete bypass of the limit by choosing a different recipient.
    * The lock has to be estate-wide, because the budget is. A per-repo lock would fail to
      serialise the one pair that needs it: two sends from the same session in different repos.
    """

    def __init__(self, repo, sender_id, target, reply, override):
        self.repo = repo
        self.sender_id = sender_id
        self.target = target
        self.reply = reply
        self.override = override
        self.charge = None
        self.left = None

    def reserve(self):
        """Take the budget, or return a refusal string. None means go ahead."""
        if not self.sender_id:
            return None  # unattributable: not budgeted, per the documented exception
        now = time.time()
        try:
            with locks.hold(locks.ESTATE, "doorbell", wait=15.0,
                            owner=f"msg {self.sender_id[:8]}"):
                try:
                    log = store.load_sends(self.repo)
                    # A TORN TAIL MUST BE REPAIRED BEFORE APPENDING. `load_sends` tolerates an
                    # unterminated final record as a half-written append -- but appending after
                    # those bytes concatenates the two, so the ring recorded here is
                    # unparseable on the next read and the charge silently disappears. The
                    # terminating newline costs nothing and makes the tail a complete, skipped
                    # record rather than a splice point.
                    store.terminate_sends(self.repo)
                except store.UnreadableSends as e:
                    # Fail closed -- but an explicit --override still passes, because the whole
                    # point of the flag is to proceed when the tool cannot decide for you, and
                    # a storage fault is exactly that case.
                    if self.override:
                        log = None
                    else:
                        return (f"{e}\nThe doorbell budget cannot be checked, so it is assumed "
                                "spent. Repair or remove that file, or pass --override if this "
                                "must interrupt now.")
                if log is not None:
                    recent = comms.spent(log, self.sender_id, now)
                    if len(recent) >= comms.BUDGET_PER_HOUR and not self.override:
                        return comms.refusal(recent, now)
                self.charge = comms.Sent(session_id=self.sender_id, target=self.target, at=now,
                                         reply=self.reply, override=self.override,
                                         id=f"{now:.6f}-{os.getpid()}")
                store.record_send(self.repo, self.charge)
                try:
                    self.left = comms.remaining(store.load_sends(self.repo), self.sender_id, now)
                except store.UnreadableSends:
                    self.left = None
        except locks.Busy as e:
            return f"another send is holding the doorbell budget: {e}"
        except OSError as e:
            return (f"the doorbell budget could not be recorded ({e}), and ringing without "
                    "recording would remove the limit silently.")
        return None

    # NO give_back, DELIBERATELY, and this is a reduction rather than an omission.
    #
    # The budget used to be refunded when the ring was refused, so that an unreachable peer
    # cost nothing. That nicety produced three review findings in a row and each fix made it
    # more elaborate: charging after the ring left a window where an interruption was free;
    # charging before it and refunding needed an append-only reversal; and the refund itself
    # was wrong, because `SendRefused` covers BOTH "nothing was typed" and "the text was typed
    # but the submit keystroke failed" -- refunding the second leaves a doorbell sitting in the
    # peer's prompt, uncharged, which interrupts them the moment they submit.
    #
    # Charging on ATTEMPT removes all three at once. There is no window, no reversal, and no
    # outcome to disambiguate, and it is also the more honest price: the message is filed and
    # delivered either way, so what was actually spent is an attempt to interrupt.
    #
    # It costs a sender one doorbell an hour when a peer is unreachable. That is visible in the
    # log and self-correcting within the hour, which is the direction this module has learned
    # to fail in.

    def tail(self):
        if self.charge is None:
            return ""
        if self.left is None:
            return "  [budget unreadable; it was charged]"
        return f"  [{self.left} of {comms.BUDGET_PER_HOUR} doorbells left this hour]"


def cmd_msg(args) -> int:
    # Look the target up on the board and pass its recorded pid. `send` documents expect_pid as
    # the guard against pane-id reuse, and the CLI was not using its own guard -- so a window
    # reassigned between the two resolutions would have been typed into as though it were the
    # intended peer. Falls back to no pid only when the target is not a registered session
    # here, which is the case where there is nothing better to check against.
    expect, target = args.pid, args.target
    recipient = None
    repo = _resolve_repo(args)
    if repo:
        # EVERY match, not the first one. A window label can name two registered sessions -- a
        # split window is the ordinary way -- and taking the first meant the body was filed into
        # one of them arbitrarily before `send` performed its own ambiguity check. The send then
        # refused, correctly, and the message sat in a stranger's inbox: refused and delivered
        # to the wrong session at once. Resolve to exactly one before anything is written.
        # LIVE windows and titles (`_live`): a stored window names whoever held that number
        # when the record was written, which after a close is a different session.
        want = tmuxio.clean_title(args.target).lower()
        hits = [p for p in _live(store.load_all(repo))
                if args.target in (p.pane, p.window, p.session_id)
                or (want and p.title.lower() == want)]
        if len(hits) > 1:
            where = ", ".join(sorted(f"{p.session_id} ({p.pane})" for p in hits))
            print(f"session-coord: {args.target} names {len(hits)} registered sessions "
                  f"({where}), so it does not name one. Nothing was filed and nothing was "
                  "sent; address a pane id or a session id.", file=sys.stderr)
            return 2
        for p in hits:
            recipient = p
            # A record from a previous tmux server is not this peer, whatever its pid
            # says: a restart reuses %N ids and pids get recycled, so expect_pid alone
            # would pass and the message would go to whoever inherited the pane.
            here = tmuxio.server_pid()
            if here and p.tmux_server and p.tmux_server != here:
                print(f"session-coord: {args.target} is registered against tmux server "
                      f"{p.tmux_server}, but this box is running {here}. That record is "
                      "stale -- the pane id means nothing across a restart. Nothing sent.",
                      file=sys.stderr)
                return 2
            if expect is None:
                if p.claude_pid is None:
                    # A registered record with no pid cannot guard against pane reuse, and
                    # sending anyway contradicted the comment below. Refuse: a known peer
                    # deserves MORE care than an unknown one, not less.
                    print(f"session-coord: {args.target} is registered here but has no "
                          "recorded claude pid, so a pane-reuse check is impossible. "
                          "Pass --pid explicitly if you are sure.", file=sys.stderr)
                    return 2
                expect = p.claude_pid
            # A session id is advertised as a target but means nothing to tmux, so it has
            # to be translated here or `send` refuses every one of them.
            if args.target not in (p.window,) and p.pane:
                target = p.pane
    me = _me(repo)
    sender = args.sender or (me.label if me else "")

    # Resolved BEFORE filing, for two reasons Copilot found on PR #12.
    #
    # The marker has to be on the body that is STORED, not only on the one that is typed. It was
    # not: `compose` was written, tested and imported, and then never called on the registered-
    # peer path, which is the normal path -- so the recipient's inbox never carried
    # `[no reply needed]` and the opt-in contract was not enforced anywhere that mattered. A
    # unit test of a pure function cannot notice that nothing calls it.
    #
    # And identity is taken from the PANE, never from the argument. Trusting `--session` meant
    # the three-doorbell limit was bypassable by inventing an id: `--session fresh` got a clean
    # budget, and naming a peer spent theirs. An explicit value is now only accepted when it
    # agrees with the pane's own record.
    try:
        reply = comms.check_reply(getattr(args, "reply", comms.REPLY_NONE))
    except comms.CommsError as e:
        print(f"session-coord: {e}", file=sys.stderr)
        return 2
    # The SENDER is resolved estate-wide, unlike `me` above, which is the target board's view
    # and is what names this session to the recipient. Two different questions: who is writing
    # (budget) and how they appear on the board the message lands on (attribution).
    sender_rec = _me_anywhere(repo)
    claimed = (getattr(args, "session", "") or "").strip()
    if claimed and sender_rec and claimed != sender_rec.session_id:
        print(f"session-coord: --session {claimed} is not this pane's session "
              f"({sender_rec.session_id}). The doorbell budget is per session, so it is taken "
              "from the pane rather than from an argument. Drop --session.", file=sys.stderr)
        return 2
    sender_id = sender_rec.session_id if sender_rec else ""
    override = (getattr(args, "override", "") or "").strip()

    # File the body in the recipient's inbox FIRST, then ring. The message is DELIVERED the
    # moment it is filed; the doorbell only shortens how long it sits unread. That ordering is
    # what turns a busy or unreachable peer into a delay rather than a failure -- before it,
    # three consecutive sends to one peer were refused and the thing that needed saying went
    # nowhere at all.
    if recipient is not None and repo:
        # Filed mail is read by a terminal exactly as typed mail is, so it is filtered exactly
        # as `tmuxio._clean` filters a direct send -- newlines and tabs allowed, because prose
        # is the point of the queue, and nothing else. Before this the queue was the way AROUND
        # the guard: the one path that never types is the one that never checked.
        try:
            inbox_mod.validate(args.text, args.subject or "")
        except inbox_mod.Unsafe as e:
            print(f"session-coord: {e}", file=sys.stderr)
            return 2
        try:
            with locks.hold(repo, "inbox", wait=15.0, owner=f"msg from {sender or '?'}"):
                try:
                    queue = store.load_inbox(repo)
                except store.UnreadableQueue as e:
                    # Preserve, then proceed. Overwriting would destroy mail nobody has read;
                    # refusing would leave a peer unreachable behind a corrupt file nobody is
                    # looking at. Renaming is the only move that keeps both.
                    kept = store.quarantine_inbox(repo)
                    print(f"session-coord: {e}\nMoved it to {kept} and started a fresh inbox; "
                          "nothing in it was lost, but nobody has read it either.",
                          file=sys.stderr)
                    queue = []
                queue = list(queue) + [inbox_mod.Message(
                    id=f"{_now()}-{os.getpid()}-{len(queue)}",
                    sender=sender or "a peer", recipient=recipient.session_id,
                    sender_name=me.name if me else "",
                    body=args.text, created=_now(), subject=args.subject or "",
                    reply=reply)]
                store.save_inbox(repo, queue)
                ring = inbox_mod.needs_doorbell(queue, recipient.session_id, _now())
        except locks.Busy as e:
            print(f"session-coord: {e}", file=sys.stderr)
            return 75
        if not ring:
            print(f"filed for {recipient.label}; it already has unread mail, so no doorbell "
                  "rung")
            return 0

        # THE BUDGET GATES THE DOORBELL, NEVER THE FILING. Filing costs the recipient nothing
        # until it looks; ringing makes it spend a turn reading and usually another replying.
        # So what is rationed is interrupting a peer -- a far narrower claim than telling one
        # something -- and an over-budget message is still delivered, just quietly.
        bell = _Doorbell(repo, sender_id, args.target, reply, override)
        refused = bell.reserve()
        if refused:
            print(f"filed for {recipient.label}, and NOT rung: {refused}", file=sys.stderr)
            return 0
        try:
            pane = tmuxio.send(target, inbox_mod.DOORBELL, expect_pid=expect, sender="",
                               force=args.force, spill_over=0)
        except tmuxio.SendRefused as e:
            print(f"filed for {recipient.label}, but the doorbell could not be rung: {e}",
                  file=sys.stderr)
            return 0
        print(f"filed for {recipient.label} and rang ({pane}){bell.tail()}")
        return 0

    # No presence record for the target -- an unregistered peer, or a bare pane id. There is no
    # inbox to file into, so fall back to typing the message itself.
    #
    # BUDGETED TOO, and this was a complete bypass before: the limit lived only in the branch
    # above, so a registered session could interrupt as often as it liked simply by addressing
    # an unregistered target or a bare pane id. Worse here than there, because this path types
    # the whole message rather than a doorbell, and nothing is filed -- so refusing means the
    # message is not delivered at all, and the refusal has to say so.
    bell = _Doorbell(repo, sender_id, args.target, reply, override)
    refused = bell.reserve()
    if refused:
        print(f"session-coord: NOT sent, and nothing was filed -- {args.target} is not a "
              f"registered session, so there is no inbox to leave it in: {refused}",
              file=sys.stderr)
        return 2
    try:
        pane = tmuxio.send(target, comms.compose(args.text, reply), expect_pid=expect,
                           sender=sender, force=args.force)
    except tmuxio.SendRefused as e:
        print(f"session-coord: {e}", file=sys.stderr)
        return 2
    print(f"sent to {args.target} ({pane}) -- unregistered, so typed rather than "
          f"filed{bell.tail()}")
    return 0


def cmd_lock(args) -> int:
    """Serialize an operation that mutates the repo's shared state.

    Exists for `pr-merge`: its merge-and-cleanup tail touches `.git` in the main checkout, and
    under `strict_required_status_checks` the first merge to land makes every other open PR
    stale. Wrapping only that tail -- never the CI wait -- serializes what races and leaves the
    parallelism that is worth having.
    """
    repo = _resolve_repo(args)
    if repo is None:
        print("session-coord: not a git repo.", file=sys.stderr)
        return 0
    if args.status:
        who = locks.holder(repo, args.name)
        print(f"{args.name}: held by {who}" if who else f"{args.name}: free")
        return 0
    if not args.command:
        print("session-coord: nothing to run; pass a command after `--`.", file=sys.stderr)
        return 2
    try:
        return locks.run(repo, args.name, args.command, wait=args.wait, owner=args.owner or "")
    except locks.Busy as e:
        print(f"session-coord: {e}", file=sys.stderr)
        return 75  # EX_TEMPFAIL: retryable, and distinguishable from the command's own codes


def _me(repo: str | None) -> Presence | None:
    """This session's own presence record, found by pane."""
    if not repo:
        return None
    pane = os.environ.get("TMUX_PANE")
    return next((p for p in _live(store.load_all(repo)) if pane and p.pane == pane), None)


def _me_anywhere(repo: str | None) -> Presence | None:
    """This session's record, searched across the ESTATE rather than one repo's board.

    The doorbell budget is per session and spans repositories, so the identity behind it has to
    as well. `_me(repo)` looks only at the TARGET's board, so a session registered in repo A
    sending into repo B found no record, passed an empty id, and was treated as unattributable
    -- which skips accounting entirely. Repeating that across repos bypassed the whole limit,
    and it needed no flag: just a `--repo` pointing somewhere this pane is not registered.

    The local board is checked first because it is the common case and one read; the estate
    scan is the fallback, and it is bounded by the number of repos that have ever registered a
    session, not by anything a caller controls.
    """
    server = tmuxio.server_pid()

    def mine(p) -> bool:
        # A PANE ID MEANS NOTHING ACROSS A TMUX RESTART: %N ids are reused, so a record from a
        # previous incarnation can match this pane and absorb the charge -- billing a stale
        # session while the current one keeps its full budget. `cmd_msg` already refuses a
        # target on those grounds; the sender needed the same check and did not have it.
        #
        # An empty `tmux_server` is a record written before that field existed, and is accepted
        # for compatibility rather than treated as a mismatch.
        # An UNKNOWN server is not a wildcard. `not server` made every record with this pane
        # look like the sender when tmux could not be observed, so a charge landed on whoever
        # held the pane in a previous incarnation -- attributing somebody else's budget, which
        # is the opposite of the fail-closed rule this predicate exists to enforce. No server
        # means no identity: the send becomes unattributable and therefore unbudgeted, which is
        # the documented exception rather than a new one.
        #
        # An empty `tmux_server` on the RECORD is different and still accepted: those were
        # written before the field existed, and rejecting them would unbudget every
        # pre-upgrade session.
        if not server:
            return False
        return p.pane == pane and (not p.tmux_server or p.tmux_server == server)

    pane = os.environ.get("TMUX_PANE")
    here = _me(repo)
    if here is not None and mine(here):
        return here
    if not pane:
        return None
    for other in store.known_repos():
        if repo and other == repo:
            continue
        found = next((p for p in store.load_all(other) if mine(p)), None)
        if found is not None:
            return found
    return None


def _you(repo: str, session_id: str) -> str:
    """"you are X, now in window Y", read live, for the top of a delivered inbox."""
    rec = next((p for p in _live(store.load_all(repo)) if p.session_id == session_id), None)
    if rec is None:
        return ""
    where = f", now in window {rec.window}" if rec.window else ""
    return (f"You are {rec.name}{where} (pane {rec.pane}). Window numbers shift when Daniel "
            "closes a window, so name sessions, yourself included, by name.")


def cmd_inbox(args) -> int:
    """Drain this session's unread messages.

    What the doorbell tells a peer to run. Prints full bodies rather than a summary -- this IS
    the delivery, and a session that has to go and open a file has not received anything yet.
    """
    repo = _resolve_repo(args)
    if repo is None:
        print("session-coord: not a git repo.", file=sys.stderr)
        return 0
    me = _me(repo)
    who = args.session or (me.session_id if me else "")
    if not who:
        print("session-coord: cannot tell which session this is; pass --session.",
              file=sys.stderr)
        return 2
    try:
        with locks.hold(repo, "inbox", wait=args.wait, owner="inbox"):
            try:
                queue = store.load_inbox(repo)
            except store.UnreadableQueue as e:
                # A reader never repairs and never writes. Say what is wrong and where, so the
                # recipient knows mail may be waiting rather than concluding the inbox is empty.
                print(f"session-coord: {e}\nYour mail may be in there; nothing was changed.",
                      file=sys.stderr)
                return 2
            mine = inbox_mod.unread(queue, who)
            # `--ack` acknowledges exactly the messages that were delivered, by id, rather
            # than "whatever is unread now". The delivery hook reads without clearing, writes
            # the mail into the turn, and only then acknowledges -- and between those two
            # calls a peer can file something new. Clearing by recipient would mark that new
            # message read although it reached nobody: mail lost in the gap between two
            # processes, which is the same delivery-versus-bookkeeping failure one level out.
            if args.ack:
                wanted = {i for spec in args.ack for i in spec.split(",") if i}
                mine = [m for m in mine if m.id in wanted]
                if mine:
                    store.save_inbox(repo, inbox_mod.mark_read(queue, [m.id for m in mine]))
                if args.ids:
                    print("\n".join(m.id for m in mine))
                return 0
            if args.ids:
                # ONE SNAPSHOT, under the lock already held. An earlier version had the hook
                # call this a second time to learn the ids of what it had just read, and those
                # two peeks were separate processes: a message filed between them appeared in
                # the id list but not in the bodies, so the acknowledgement marked it read
                # although it had been delivered to nobody. That is the same defect the
                # acknowledgement-by-id was introduced to fix, reintroduced by the fix.
                #
                # So the ids ride with the bodies, on a sentinel first line the caller strips.
                # Two peeks cannot be made consistent by trying harder; there has to be one.
                sys.stdout.write(IDS_PREFIX + " ".join(m.id for m in mine) + "\n")
                sys.stdout.write(inbox_mod.render(mine, marked_read=False, you=_you(repo, who)))
                sys.stdout.flush()
                return 0
            # Render and WRITE before the queue is marked read, while still holding the lock.
            # Committing first meant a broken pipe, a closed stdout or an interrupt between the
            # save and the write left the messages unreadable from the queue although nothing
            # had been delivered -- the delivery guarantee inverted by its own bookkeeping.
            if not (args.quiet and not mine):
                sys.stdout.write(inbox_mod.render(mine, marked_read=bool(mine) and
                                                  not args.peek, you=_you(repo, who)))
                sys.stdout.flush()
            if mine and not args.peek:
                store.save_inbox(repo, inbox_mod.mark_read(queue, [m.id for m in mine]))
    except locks.Busy as e:
        print(f"session-coord: {e}", file=sys.stderr)
        return 75
    return 0


def cmd_harvest(args) -> int:
    repo = _resolve_repo(args)
    path = args.transcript
    # SKILL.md documents `session-coord harvest --quiet` with no --session, so falling back to
    # the environment is what makes the DOCUMENTED command work rather than report "no
    # transcript". Same class of bug as the --pid one: the broken path was the one the
    # documentation told people to take.
    session = args.session or os.environ.get("CLAUDE_SESSION_ID") or ""
    if not path and repo:
        rec = next((p for p in store.load_all(repo) if p.session_id == session), None)
        if rec is None and not session:
            pane = os.environ.get("TMUX_PANE")
            rec = next((p for p in store.load_all(repo) if pane and p.pane == pane), None)
        path = rec.transcript if rec else ""
    if not path:
        print("session-coord: no transcript to harvest.", file=sys.stderr)
        return 0
    cands = harvest.candidates(harvest.load_transcript(path))
    if args.json:
        print(json.dumps([c.__dict__ for c in cands], indent=2))
        return 0
    if args.quiet and not cands:
        return 0
    sys.stdout.write(harvest.render(cands))
    return 0


# ---- wiring --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="session-coord",
        description="Keep several Claude sessions in one repo from tangling.")
    sub = p.add_subparsers(dest="cmd", required=True)

    def repo_arg(sp):
        sp.add_argument("--repo", help="repo root (default: the one containing cwd)")

    reg = sub.add_parser("register", help="record this session's presence in a repo")
    repo_arg(reg)
    reg.add_argument("--session", required=True)
    reg.add_argument("--pane")
    reg.add_argument("--window")
    reg.add_argument("--pid", type=int)
    reg.add_argument("--cwd")
    reg.add_argument("--charter", help="one paragraph: what this session is for")
    reg.add_argument("--claim", action="append", help="a path or glob this session will edit")
    reg.add_argument("--transcript")
    reg.add_argument("--wait", type=float, default=5.0,
                     help="seconds to wait for the presence lock (default 5). Deliberately "
                          "short: this runs in a SessionStart hook, and a long stall there is "
                          "the first thing a session does.")
    reg.set_defaults(func=cmd_register)

    b = sub.add_parser("board", help="who else is working in this repo")
    repo_arg(b)
    b.add_argument("--quiet", action="store_true", help="say nothing unless there are peers")
    b.add_argument("--json", action="store_true")
    b.set_defaults(func=cmd_board)

    c = sub.add_parser("check", help="findings for this repo; exit 2 blocks, 1 warns")
    repo_arg(c)
    c.add_argument("--workdir", help="report only findings that involve this working tree")
    c.add_argument("--repo-of", dest="repo_of",
                   help="resolve the repo from this path rather than from cwd")
    c.add_argument("--quiet", action="store_true", help="print only blocking findings")
    c.add_argument("--json", action="store_true")
    c.set_defaults(func=cmd_check)

    s = sub.add_parser("sweep", help="drop records whose session is gone")
    repo_arg(s)
    s.add_argument("--wait", type=float, default=5.0)
    s.set_defaults(func=cmd_sweep)

    dr = sub.add_parser("deregister", help="remove one session's record by id")
    repo_arg(dr)
    dr.add_argument("--session", required=True)
    dr.add_argument("--wait", type=float, default=5.0)
    dr.set_defaults(func=cmd_deregister)

    cond = sub.add_parser("conductor", help="who sequences the sessions here")
    cond.add_argument("action", choices=["claim", "show", "release"])
    repo_arg(cond)
    cond.add_argument("--session")
    cond.add_argument("--wait", type=float, default=30.0,
                      help="seconds to wait for the presence lock (default 30)")
    cond.set_defaults(func=cmd_conductor)

    a = sub.add_parser("ask", help="the one queue Daniel reads")
    a.add_argument("action", choices=["add", "list", "answer", "dissolve"])
    repo_arg(a)
    a.add_argument("--session")
    a.add_argument("--window")
    a.add_argument("--tag")
    a.add_argument("--kind", choices=["A", "Q"])
    a.add_argument("--text")
    a.add_argument("--depends-on", action="append", dest="depends_on")
    a.add_argument("--wait", type=float, default=30.0,
                   help="seconds to wait for the queue lock (default 30)")
    a.set_defaults(func=cmd_ask)

    m = sub.add_parser("msg", help="type a message into another session's prompt")
    repo_arg(m)
    m.add_argument("target", help="pane id (%%67), window label (infra:11) or session id")
    m.add_argument("text")
    m.add_argument("--subject", help="a short line shown above the body in the inbox")
    m.add_argument("--sender", help="how to label this message (default: this session's window)")
    m.add_argument("--force", action="store_true",
                   help="send even if the target's prompt has unsubmitted text in it")
    m.add_argument("--pid", type=int,
                   help="the claude pid this message is addressed to; refuses if the pane "
                        "holds a different one. Looked up from the board when omitted.")
    m.add_argument("--session", default="",
                   help="the sending session id, for the doorbell budget")
    m.add_argument("--reply", default="none", choices=["none", "required"],
                   help="whether an answer is wanted. Default none, and the body carries a marker saying so -- acknowledgement is not collegial, it is a turn.")
    m.add_argument("--override", default="",
                   help="one line saying what breaks if this waits; rings past the budget and records the reason.")
    m.set_defaults(func=cmd_msg)

    lk = sub.add_parser("lock", help="run a command under a named per-repo lock")
    repo_arg(lk)
    lk.add_argument("name", help="lock name, e.g. `merge`")
    lk.add_argument("command", nargs="*", help="the command to run under it (after `--`)")
    lk.add_argument("--wait", type=float, default=300.0, help="seconds to wait (default 300)")
    lk.add_argument("--owner", help="how to name this holder to whoever is waiting")
    lk.add_argument("--status", action="store_true", help="report the holder and exit")
    lk.set_defaults(func=cmd_lock)

    ib = sub.add_parser("inbox", help="read and clear messages peers left for this session")
    repo_arg(ib)
    ib.add_argument("--session")
    ib.add_argument("--peek", action="store_true", help="read without marking them read")
    ib.add_argument("--ids", action="store_true",
                    help="print message ids instead of bodies, for a caller that will --ack")
    ib.add_argument("--ack", action="append", default=[],
                    help="mark exactly these ids read (repeatable, or comma-separated); for a "
                         "caller that has already delivered that snapshot")
    ib.add_argument("--quiet", action="store_true", help="say nothing when there is no mail")
    ib.add_argument("--wait", type=float, default=15.0)
    # The UserPromptSubmit hook runs with the prompt's cwd, which can be any subdirectory of a
    # worktree, so it needs the same path-to-repo resolution `check` has rather than relying on
    # the process cwd.
    ib.add_argument("--repo-of", dest="repo_of",
                    help="resolve the repo from this path rather than from cwd")
    ib.set_defaults(func=cmd_inbox)

    h = sub.add_parser("harvest", help="loose ends this session is about to leave behind")
    repo_arg(h)
    h.add_argument("--session")
    h.add_argument("--transcript")
    h.add_argument("--quiet", action="store_true")
    h.add_argument("--json", action="store_true")
    h.set_defaults(func=cmd_harvest)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)
