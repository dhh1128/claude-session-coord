---
name: session-coord
description: Coordinate with the other Claude sessions working in this repo — see who else is here and what they are doing, claim or hand off the conductor role that sequences them, file an ask into the one queue Daniel reads, message a peer session directly instead of asking him to carry a paste, and harvest loose ends before a window closes. Use when the board reports peers, when you are about to touch a file another session claims, when you would otherwise ask Daniel to relay something between windows, when a merge or a shared branch needs sequencing, or when this session is finishing. Mechanics live in the `session-coord` command; this skill holds the judgement.
---

# Coordinating with the other sessions in this repo

The hooks already register you, sweep dead sessions, and refuse a staging command in a shared working tree. This skill is for the parts that need judgement.

## Why this exists, in one paragraph

On 2026-09-17 four sessions were sited in `acme/infra`. One merged `origin/main` into a branch two others had been committing to, not knowing it was shared. Another's working tree held a third's uncommitted audit, and it commits with `git add -A` — one more commit and it would have landed a stranger's work under its own message. Seventeen commits on one branch split 10 / 4 / 3, and three belonged to nobody. Every session behaved correctly in isolation: each hit a wall, each refused to guess, each escalated to Daniel. The aggregate was an hour of him carrying prompts between tmux windows, and his verdict was *"Three well-behaved sessions producing one badly-behaved system, because nobody was sequencing and you were the only channel between them."* Sequencing is the missing role, not better behaviour. That is what this skill is for.

## The rule that outranks everything here

**His attention is the scarce resource.** Before you ask him anything, ask whether a peer session could answer it instead — and if it could, ask the peer. A question routed to him that a session could have answered is the failure this whole tool exists to remove.

## When the tooling itself misbehaves, tell the conductor

If `session-coord` refuses something you believe is legitimate — a gate denial you think is wrong, a board that shows you in the wrong place, a message that will not send — **that goes to the conductor, not to Daniel.** The conductor owns this tooling's behaviour, can verify your case against the live board in seconds, and can fix it. Routing it to Daniel turns a tool bug into an interruption, which is the exact cost this package exists to remove. Three sessions did that on 2026-09-18 before anyone noticed the pattern.

**Report the observation. Do not attach a cause.** On that same day four sessions — including the one that wrote this tool — produced an accurate symptom report bolted to a confidently wrong diagnosis, and two of those wrong diagnoses were identical. A session can see one layer of a system it did not build; the cause it infers will be plausible and is usually wrong, and a wrong cause sends the fix in the wrong direction. Say what you ran, what you expected, what happened, and what you checked. Let whoever owns the code re-derive why.

If the conductor is unreachable or is itself the problem, then Daniel — but say that you tried.

## Orient first

```
session-coord board          # who else is here, their charter, their claims
session-coord check          # what is currently wrong
session-coord conductor show # who is sequencing
```

If you are alone, all three are nearly silent and you should carry on with your work. Nothing below applies.

## Move with `EnterWorktree`, not with `cd`

**This is the single thing most likely to waste your time, so it comes before everything else.**

There are two different ways to "work in a worktree", and only one of them is visible to the gate:

- **`EnterWorktree`** — a Claude Code tool. It moves *the session*. The session's working directory becomes the worktree, and everything downstream sees that.
- **`cd /path/to/worktree && git commit …`** — a shell command. It moves *one shell invocation*. The session's working directory never changes.

The PreToolUse gate is handed the **session's** directory, not the shell's. So a session that took a worktree and moves into it with `cd` looks, to the gate, exactly like a session still sitting in the shared main checkout — and gets refused, and is shown a remedy it has already followed.

That is not hypothetical. On 2026-09-18 a session did precisely this: its `git rev-parse --show-toplevel` was its own worktree, the main checkout was clean, every commit on its branch held only its own files, and it was blocked anyway. It spent an hour on it, escalated to Daniel, and diagnosed a bug in the gate that did not exist. Two other sessions reached the same wrong conclusion independently.

So: `git worktree add <repo>/.worktrees/<topic> -b <topic> origin/main`, then **`EnterWorktree` with that path**. If you are already using `cd`, switch — `EnterWorktree` relocates a running session with no restart and no lost context.

## Claim your charter and your files

When peers exist, say what you are for and what you will touch:

```
session-coord register --session "$CLAUDE_SESSION_ID" \
  --charter "One paragraph: what this session is for and roughly where it will stop." \
  --claim 'claude/install.sh' --claim 'sessioncoord/**'
```

Claims are how the collision nobody can see gets caught. `AGENTS.md:107` read *identically* on both sides of the 2026-09-17 merge, so git produced no conflict and the file was silently wrong afterwards. A conflict you have to resolve is the safe case; the dangerous one is two edits that merge cleanly and contradict each other. Claim the file that carries a count, a hand-maintained list, a registry — not every file you might read.

**Registration is a snapshot, not a subscription.** It records where you were when you ran it, and nothing updates it when you move. So re-register after `EnterWorktree`, after a `/clear`, and whenever your charter changes — `register` will warn you when the directory it has recorded is not the one you are in. A session recorded in the wrong place is worse than an unregistered one: it makes the board confidently wrong, and the gate acts on the board.

**A claim means you get told, not that you hold the pen.** If a peer needs one line in a file you claimed, the answer is usually "go ahead, tell me when it lands", not "wait for me". Claim to be consulted, not to block — two sessions froze themselves unnecessarily on 2026-09-18 because a glob claim read as ownership of a whole directory.

## The conductor

One session per repo sequences the others. Claim it if you have the widest view:

```
session-coord conductor claim --session "$CLAUDE_SESSION_ID"
```

**Holding it means:** peers route ordering and sequencing questions to you rather than to Daniel; you decide what lands first when several branches are in flight; you adjudicate contradictions between sessions' outputs; and when you must go to him, you go **once**, with a single ordered list.

**Not holding it means:** when you hit a sequencing question — what merges first, whether to wait, where to write given someone else's unpushed work — send it to the conductor, not to Daniel. A session asking him permission to deviate from a rule he set an hour earlier is the right instinct aimed at the wrong recipient.

Release it before you close, or hand it over explicitly. A conductor that vanishes silently puts him back in the middle.

## Asks: one queue, self-contained, ordered

```
session-coord ask add --tag Q-ZNYN --kind Q \
  --text "Merge the PR that carries three sessions' accumulated work, or split it first?"
session-coord ask list
```

Two rules the queue enforces, both from things that cost him time:

- **An ask must stand on its own.** He was asked to approve `@idymqyp3` and to adjudicate `Q-8RJN` — identifiers meaningful to the sessions and opaque to him, so he had to go and reconstruct the context before he could answer. Restate the subject inside the ask. The command refuses a bare identifier and a verb.
- **Say what blocks what.** Use `--depends-on` so the list orders itself. He said the thing that drives him crazy is not knowing whether to answer one question first or carry something to another window first. If one ask gates the rest, it should be the only thing he sees.

When a peer's work makes your question moot, retire it yourself — `session-coord ask dissolve --tag Q-8RJN`. Do not make him answer a question that no longer exists.

`--kind A` is something he must go and do outside the chat; `--kind Q` is something a typed reply discharges. Fifteen minutes versus one word — never conflate them. The list prints in dependency order with the kind marked per item, rather than grouped by kind: a question that waits on an errand has to appear *below* that errand, or he is being told to answer something he cannot answer yet.

## Talk to peers directly

```
session-coord msg "conf-suite-open" "I am about to change .github/workflows/ci.yml's gate list. You claim it too — are you mid-edit?"
```

**Name sessions by name, never by window number** — peers, and yourself. Window numbers are not stable: Daniel closes windows when sessions finish, tmux renumbers every window after the closed one, and he keeps it that way. On 2026-10-10 a session in infra:6 introduced itself as infra:7, and another asked after a reply "from infra:7" while it was itself running in infra:7. A session's name is the title Claude Code gives its pane — the one in the prompt border — and the board prints it as `conf-suite-open (now infra:6)`, with the window read from tmux at that moment. `msg` accepts the name (quote it if it has spaces), a pane id or a session id. A window label still works, but it reaches whoever holds that number *now*, and the result line names who that was: read it. Your inbox opens with who you are and where you are now. When you tell Daniel about another session, give the name and its current window together, so he can find it.

**The body is filed in the recipient's inbox; only a doorbell is typed.** What lands in their prompt is exactly ` >>> session-coord inbox`, every time, whatever you said. They run it, read everything waiting, and it clears.

That ordering is what makes this reliable. The message is *delivered* the moment it is filed — the doorbell only shortens how long it sits unread. So a peer who is mid-sentence, or whose prompt cannot be read, is a **delay** rather than a failure: the mail is durable and they get it on their next drain. Before this, three consecutive sends to one session were refused inside a minute and the thing that needed saying went nowhere.

Two consequences worth knowing. **Your message can be prose** — newlines, lists, code — because nothing types it. And **a peer that already has unread mail is not rung again straight away**, so ten messages in a row cost one keystroke against their prompt rather than ten. That silence has a lifetime rather than being permanent: once the oldest unread has sat for ten minutes the next message rings again, because two peers accumulated three and six unread messages inside two hours and read none, and "has been told" had quietly stopped meaning "has unread mail".

Read yours with `session-coord inbox`. Do it when a doorbell arrives, and at the start of a turn if you have been away. `--peek` reads without clearing, and says so in its footer.

A filed message is refused only for a control byte that is neither a newline nor a tab — an escape sequence would reconfigure the reader's terminal when they drain it. The **direct** path is stricter, and it is the one you hit when the target is not registered here: no inbox to file into, so the text is typed, and then it must be one line, non-empty, under 4000 characters, and aimed at a pane holding a live session rather than a shell — sending to a shell would *execute* it. For anything longer than a sentence to an unregistered target, write the detail to a file and send the path.

Daniel authorised this on 2026-09-17 precisely so he stops being the transport. Use it for: telling a peer you are touching its file; asking what a peer's branch contains; telling a peer its blocker has cleared. Do **not** use it to poll peers for opinions — N sessions broadcasting questions at each other is noise, and it is not what any observed failure needed.

Say who you are in the message, by name. The recipient has no idea which window it came from, and by the time it reads the message the window may be a different one.


### Before you ring: what does the recipient do differently?

The doorbell is **budgeted at three per hour per session**. Filing is not — it costs the
recipient nothing until it looks. What is rationed is *interrupting* a peer, which is a much
narrower claim than telling one something.

Past the budget a message is **still filed and still delivered**; the doorbell is simply
withheld, and the refusal names the three already rung so you can decide which mattered less.
If it genuinely cannot wait, `--override "<what breaks otherwise>"` rings anyway and records
why — the override still counts, because the recipient still spent the turn.

Before ringing, answer this:

> **What does the recipient do differently in the next hour because of this? Name the action.**

If you cannot name one, file it without urgency and let their next turn pick it up. *"They
would know I appreciated the catch"* is not an action. The faster form: **would you still send
this if they could not reply?** Sequencing, claims, corrections and bug reports pass. These
fail, always:

- credit, thanks, or acknowledgement
- agreement that changes nothing
- "adopted your suggestion"
- status updates nobody is blocked on
- relaying something the recipient can already see
- confirming receipt

**Replies are opt-in.** `--reply none` is the default and the body carries a `[no reply needed]`
marker. A peer that sends you one is not being curt; answering it costs two turns and buys
nothing.

### Never narrate peer traffic to Daniel

Do not report what you said to a peer, or what a peer said to you, **unless it changes a
decision he holds**. Coordination is infrastructure: he sees its results, not its transcript.

This is the rule that most directly buys his attention back, and it is the one sessions break
while explaining why they should not. On 2026-09-18 two sessions spent an hour messaging each
other — one relayed something the other had already told him directly, the second messaged back
to stop the relay, and both then narrated the exchange to him. Four turns of his attention, no
decision changed. Each message was defensible alone; that is exactly why there is a budget
rather than a principle.

### You do not have to remember to drain

`claude/hooks/session-coord-mail` runs on every prompt and delivers unread mail straight into
the turn's context, so mail is read without anyone running a command and without a turn spent
on the reading. The doorbell still earns its keep, and more clearly: it **wakes a session that
would not otherwise take a turn for hours**. The hook only removes the case where mail sat
unread for no reason.

## Sequencing a merge

`pr-merge run` waits minutes for CI and a Copilot review, then mutates shared state in a few seconds at the end: it removes a worktree, deletes two branches, prunes, and fast-forwards the main checkout. Under `strict_required_status_checks` the first merge to land also makes every other open PR stale, so a second session can spend a whole review cycle and then be refused.

**The right fix is a lock around that tail, inside `pr-merge`. It does not exist yet.** What exists is a coarse whole-run lock:

```
session-coord lock merge --owner "infra:11 merging #69" -- pr-merge run 69
session-coord lock merge --status
```

Be honest about what that costs, because it is the opposite of the design in `sessioncoord/locks.py`: it holds the lock across the CI wait too, so two merges in one repo run back to back rather than in parallel. On a repo whose CI takes ten minutes that is ten minutes of a second session sitting idle. Use it when two merges would genuinely collide — several PRs landing in one repo in one sitting — and skip it when you are the only session merging, which is most of the time.

`prmerge/` belongs to the `merge-pr` session; taking the lock around only the merge-and-cleanup tail is a change there, not here. Until that lands, do not describe this wrapper as "the tail lock" — it is not, and the gap was caught in review rather than in use.

Exit 75 means another session holds it. That is retryable and is not a merge failure. Report it and wait; never route around it.

## Before this session closes

```
session-coord harvest --quiet
```

**This is a net, not a verdict.** It narrows a thousand messages to a handful of tags nobody answered or cited; it does not decide whether they matter. Read them. Findings become ticks; unanswered asks either go in the queue or get dissolved. Then release the conductor role if you hold it.

The reason this step exists: window 8 closed on 2026-09-17 carrying a tick that was done but still open, a contradiction between two sessions neither had seen, and a Workspace change Daniel had made that was recorded nowhere. His words: *"The major work products get captured, but minor details are left dangling, creating hygiene problems."*

## When you find a tangle

Escalate early and escalate **once**. Do not hand him a coordination job — that is the mistake that produced this tool. Work out the sequence yourself, name the one thing you need from him, and say plainly what he should do first. If several sessions are involved, message them yourself rather than asking him to carry pastes.

The shape that worked, once somebody finally did it: one PR lands everything, two windows close, and his entire part was the word "go".
