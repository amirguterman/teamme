---
description: The single entry point for this project's agent team. Grounds a request in the project's spec, decides if and when it should happen, produces an approved brief, then dispatches the specialists in dependency order.
argument-hint: <what you want, in plain words>
---

<!--
TEMPLATE. /teamme:init-team tailors this per project. Replace every {{PLACEHOLDER}}:
  {{PROJECT}}        project name
  {{SPEC_DOCS}}      the authoritative spec/docs to read first
  {{LANE_TABLE}}     layer -> owning agent rows
  {{HARD_RULES}}     the project's "never do X / always do Y" list
  {{ORCHESTRATOR}}   the orchestrator agent, or delete those sentences if there is none
  {{VERIFY}}         the real build/test commands, or an honest statement that none exist locally
-->

# {{PROJECT}} intake

The request: **$ARGUMENTS**

This is the **only sanctioned way to request work** from the agent team. You are not implementing
anything yet. Turn a plain-language request into a grounded, approved brief, then dispatch it. Do
not write code in this command.

Run the preflight first even when the request is empty - a broken install is worth reporting either
way. Then, if it is empty, ask what the user wants and stop until they answer.

## Preflight: is teamme actually working here?

Run this before anything else - before triage, before reading a line of spec:

```bash
python3 .claude/hooks/preflight.py check
```

It prints one `PASS`/`FAIL` line per item, a `fix:` line under each failure, and a final `state:` of
`not-installed`, `installed-outdated`, `installed-not-live` or `live`. Exit 0 iff everything passed.

Exit 0: go to step 0. Non-zero, or the script is missing: **halt.** Show the output, name the fix
from the table, offer the repair, and do not continue into triage. Everything below this line runs
on those hooks - the work log, the phase lock, and the `PreToolUse` guard that makes grounding
read-only. Without them the brief has no write lock behind it and the task never reaches the log, so
a half-working intake is worse than a refusal.

| State | The fix |
|---|---|
| the command itself fails - `python3: command not found` | Nothing teamme ships can run; every hook is a `python3` script. Tell the user to install python3 (system package manager, or python.org). No repair is possible from here. |
| `not-installed` - no registered teamme hooks and no generated intake command, so nothing was ever set up here | Run `/teamme:init-team` in this project to scaffold the team, the hooks and this command. Do not hand-assemble a partial install. This is the only state where the installer is the right advice. |
| `installed-outdated` - teamme **is** installed here, but a hook script or the `hooks` block in `.claude/settings.json` is missing or stale, usually an install from an earlier release | Repairable, and repair is the *only* correct move: the `teamme_install` MCP tool, or `/teamme:team-doctor`. **Never `/teamme:init-team`** - it would re-run the questionnaire and regenerate the roster over a team that already works. See the halt note below: this state blocks this command until it is repaired. |
| `installed-not-live` - everything registered, no `SessionStart` has fired here | The session started before the settings file existed, so no hook is loaded and the grounding guard would deny nothing. Run `/hooks`, or restart the session, then re-run this command. |
| `live` but an item still `FAIL`s - a missing script, an unparseable settings file, an unwritable `.claude/intake/` | Repairable: see below. An invalid `settings.json` is the user's to fix - report it, never rewrite the file around it. |

**An incomplete install halts this command until it is repaired.** The exit code follows the
`PASS`/`FAIL` items, not the `state:` line, and every missing piece is a `FAIL` - so
`installed-outdated` always exits non-zero and always stops the flow here, even though the team
itself is set up and most of it works. That is intended, not a quirk: the guard, the phase lock and
the work log are exactly the parts that go missing, and those are what this command runs on. Say so
plainly to the user rather than leaving them guessing - name the missing files, say that repairing
them is the only thing standing between them and `/intake`, and that re-running the check after the
repair clears the halt. Do not work around it by skipping to step 0.

**Offer the repair, ask before writing.** For a partial install, say exactly which files you would
restore - missing scripts under `.claude/hooks/`, the missing `hooks` block merged into
`.claude/settings.json`, the `.claude/intake/` directory - and get the user's go-ahead first. Use
the `teamme_install` MCP tool if it is available, otherwise `/teamme:team-doctor`, which repairs an
existing install in place. `/teamme:init-team` is the installer and belongs only to
`not-installed` - never point an install that already exists at it. `teamme_status` gives the same
diagnosis and works even when the script is gone.

Be honest about what this is: a check this command runs and a refusal it chooses, not a gate the
harness enforces. Do not describe it as something that blocks anyone.

## 0. Triage: is this even for now, and is something already in flight?

### 0a. Does the request itself say "later"?

Test this **before** you read the phase and before you read a line of spec. If the wording parks the
work rather than asks for it, the whole flow below is wasted effort - the user is filing a note, not
commissioning a brief.

| The request says | Example |
|---|---|
| later, not now, some day, eventually | "later, add a dark mode toggle" |
| after something else | "once you finish the parser", "after the release is out" |
| file it | "queue this", "park this", "remind me to look at X", "when you get a chance" |

Any of those: **take the queue path** and stop. Whatever the phase is - `idle`, `grounding` or
`approved` - and whoever else is mid-flight.

**The queue path**, identical to `/teamme:queue` and to the Queue row below:

```bash
python3 .claude/hooks/worklog.py add "<the request, minus the deferral wording>" --priority P1
```

Then say **exactly one line** about it - the task id and the title - and nothing else. No grounding,
no classification, no brief, no disposition, no questions, no offer to start it now. That one-line
cap is a hard output constraint, not a style preference: parking a request has to cost the user
nothing, or they stop parking things and start dropping them. Judgement about whether the work should
happen belongs to `/intake` on the day it is picked up, not today.

The queue path moves **no phase transition at all** - it never runs `intake-state.py`. So an idle
project stays idle and you stop after that line; an intake already in flight stays exactly where it
was, and you carry on with the active work after that line.

Only a request about *when the work happens* counts. A deferral word inside the subject matter - "a
'read later' list", "a retry-after header" - is not a deferral. If it is genuinely ambiguous, treat
it as a normal request and continue.

### 0b. Is something already in flight?

```bash
python3 .claude/hooks/intake-state.py show
python3 .claude/hooks/worklog.py list
```

The first is the phase lock (transient); the second is the work log (durable, and the queue of
record). If `phase` is `idle`, this is new work - go to step 0c, but check the log first in case it
is already recorded as an open or deferred task - a request you queued days ago is arriving for real
now, so start it rather than filing a duplicate.

If `phase` is `grounding` or `approved`, **you decide** what this new message is. Do not ask the
user to choose, and do not start a second brief:

| Decision | When | Action |
|---|---|---|
| **Fold in** | It refines, constrains or corrects the active request, supplies an input a lane is blocked on, or would mean editing the same files twice. | `worklog.py note <active-id> "<text>"`. If it changes the plan, `intake-state.py reground` first so it is re-planned under the write lock, then re-approve. |
| **Queue** | A separate goal with its own layers and no dependency on the active work. | Take the queue path from 0a - `worklog.py add "<request>" --priority P1`, **one line** naming the task id and title, no phase transition - then resume the active work. Do not ground, classify or analyse the queued request; that happens the day it is picked up. |
| **Redirect** | It makes the active brief *wrong*, not merely lower priority. | **Confirm first.** Then `worklog.py block <active-id> "superseded by <new-id>"`, add the new task at `P0`, `intake-state.py release`, and begin the new one. |

Bias toward folding inside the active scope, queueing for anything that widens it. A plain question
changes no state: answer it and stop.

## 0c. Enter the read-only grounding phase

```bash
python3 .claude/hooks/worklog.py add "$ARGUMENTS" --priority P1
python3 .claude/hooks/intake-state.py begin <the-new-task-id>
```

While the phase is `grounding`, a `PreToolUse` guard **denies every edit to a project file** - from
you and from any agent you dispatch - so nothing starts implementing before the brief is approved.
Edits under `.claude/` stay allowed, and the phase expires after an hour so it can never leave the
repo write-locked. Abandoning? `intake-state.py release`.

## 1. Ground it in the spec

Read {{SPEC_DOCS}} before designing anything. Quote what already governs the request, then say which
is true: **already specified** (implement as written), **already implemented** (say where; the real
request is probably a change), **unspecified** (the brief must add the spec text), or **contradicts
a documented rule** (stop and say so).

**History questions go to the librarian, not to `git log`.** When grounding turns on what changed,
when, or why - the history of a file the request touches, how the current shape came about, when a
convention was introduced - or when you are about to write or repeat a claim of the form "X was
added in commit Y", consult the `history-librarian` agent and cite its answer, SHAs included. It
answers from teamme's history index, so it costs this flow a short consultation rather than a wall
of raw git output. Consulting it is an instruction, not a gate: nothing blocks a brief that skips
it, and saying "the history does not record this" is a legitimate answer to carry into the brief.
Skip it when the request has no history question in it, and skip it when the librarian is
unavailable or disabled in this project - note that in the brief instead of re-deriving the answer
by hand. Reading history changes no phase: you are still in `grounding`.

**Questions about what was *said* go to the same librarian.** Despite its name it reads a second
index: this project's own session transcripts, one per session plus one per dispatched subagent. So
when grounding turns on something agreed in conversation rather than written down - a decision from
an earlier session, what a specialist reported back, or above all **something that fell out of
context at a compaction** and can no longer be seen - consult `history-librarian` for that too, and
cite the session id and mark point it returns. Its four session queries are `sessions`,
`search_turns`, `window` and `compaction`, and the order that keeps the answer cheap is **locate,
then read**: `compaction` or `search_turns` to find where it was discussed, then `window` to fetch
that region and nothing else. Carry three limits into the brief whenever the answer leans on them -
reasoning is **not recoverable** (thinking blocks are stored with an empty body, so "why was Y
rejected" is answerable only from what was said out loud), **tool output is not indexed** (so a miss
means not found in what was *said*, never that it was never on screen), and **subagent threads are
usually the bulk of a session** on a team that dispatches, so most of what the question asks about
happened in a child thread. Same rules as above: an instruction, not a gate; if that index is
disabled for this project, say so and name `teamme_librarian_configure` rather than going and
reading a transcript by hand; and it changes no phase either.

**Questions about what was happening *around* something go to the same librarian too.** A third
group of queries joins those two indexes with this project's own work log - `around_path`,
`around_commit`, `around_task` and `timeline` - and answers what else was moving at the time: what
landed while a task was open, what was being discussed when a commit landed, what happened in a
given stretch of time. When grounding turns on one of those rather than on a file's own history,
consult `history-librarian` and carry its answer into the brief. One rule comes with it, because it
is the one an answer loses by accident: every association there is **time overlap**, not a recorded
link, so write it as "active while" or "around" and never as "implements", "caused" or "fixes" - and
if the brief needs the stronger claim, it has to be established some other way and said out loud.
Same rules again: an instruction, not a gate; a disabled or missing index narrows the answer and the
librarian says which store went quiet; and it changes no phase.

**A decision recovered from a transcript is not spec.** It is evidence the decision was *made*, not
that it was written down or implemented. Treat it as **unspecified** above until the brief's spec
delta writes it down, and check the code or the history index before claiming it landed.

## 2. Classify it against the layer boundaries

{{LANE_TABLE}}

If the request needs a layer no lane owns, or names a lane that no longer exists, say so in the
brief and name the nearest real owner - never invent an agent, and never dispatch to one that is not
in this table. Changing the roster is a separate request: `/teamme:modify-team` adds, drops, retools
or renames a lane and migrates the work log's lane fields with it. Do not run it from inside this
flow.

If this project's roster includes `devils-advocate`, it is deliberately not in that table and never
belongs in it: it owns no layer and is never given a part of the brief. Step 5 engages it, and
nothing else does.

Enforce the project's hard rules here, before any plan exists:

{{HARD_RULES}}

Reshape a request that would violate one, and explain why. Do not quietly implement a weakened
version, and do not refuse the whole request over one bad part.

## 2b. Decide: if, and when

You have the authority to decide whether this work should happen at all. State the disposition and
reason in one line, record it, then continue:

| Disposition | When | Action |
|---|---|---|
| **Do now** | Unblocks something, is a live correctness or safety problem, or the user is waiting. | `worklog.py priority <id> P0`, continue. |
| **Do next** | Ordinary work, nothing blocked behind it. | Leave `P1`, continue. |
| **Already satisfied** | It exists. | Say where, `worklog.py done <id>`, `intake-state.py release`, stop - or re-scope to the real change. |
| **Defer, spec only** | Sound but not now. | Write the spec text, `worklog.py defer <id> "<reason>"`, `intake-state.py release`, stop. |
| **Decline** | It would break a hard rule. | `worklog.py decline <id> "<reason>"`, `intake-state.py release`. Say why, offer the nearest legitimate alternative. |
| **Needs input** | Undecidable without the user. | `worklog.py block <id> "<what you need>"`, ask, stop. Leave the phase at `grounding`: the answer arrives as a mid-flight message and folds in at step 0b, and the lock expires on its own if it never comes. |

Declining and deferring are real outcomes, not failures. Every one of them that stops here ends the
flow, so release the phase on the way out - a `begin` with no matching `approve` or `release` leaves
the repo write-locked until the timeout.

## 3. Write the intake brief

**Problem** (in behaviour terms) · **Observable outcome** · **Layer decomposition** with the owning
agent per part, in dependency order · **Plan** · **Spec delta** (exact doc text, honestly labelled)
· **Risks** · **Done criteria** per lane · **Verification**: {{VERIFY}}

**Write each lane's part as a contract, not an implementation**: what must be true when that lane is
done, the criteria it is judged against, and the invariants within its reach. Leave the *how* to the
lane - step 5 is where it states that and has to defend it.

**Name who runs that verification.** Not every lane can: an agent whose tool list has no `Bash`
cannot run a command at all, so "verification passes" as its done criterion is a check nobody
performed. Say in the brief which lanes cannot run it themselves and who runs it for them - the
orchestrator, a lane that can, or you. A lane that could not run it hands it back unrun and says so;
that is the expected report, not a failure.

## 4. Confirm, then lift the read-only phase

Use the interactive question tool only for genuine forks. Do not ask whether the brief is good. Once
approved - and only then:

```bash
python3 .claude/hooks/intake-state.py approve
python3 .claude/hooks/worklog.py start <task-id>
```

Writes stay denied until you do. If step 2b ended the flow instead - already satisfied, defer or
decline - it has already released the phase, and you never reach this step.

## 5. Design return and challenge

Between an approved brief and any code there is one more round. The brief told each lane **what must
be true** - its part of the outcome, its done criteria, the invariants in reach. It did not say
*how*. This step makes each lane state the how, and puts those statements in front of an adversary -
one lane at a time, and all of them at once - before anything is built.

**Apply this rule, then say in one line which branch you took and why. Never skip it silently.**

| Branch | When |
|---|---|
| **Engage** | The brief adds a mechanism that did not exist, touches a design invariant, spans more than one lane, or reverses a decision already made. Any one of the four is enough. |
| **Skip** | The brief is already-specified work, lands in a single lane, introduces no new mechanism, and puts no invariant in reach. All four must hold. |

Skipping goes straight to step 6. A request that took the queue path at 0a, and anything that
arrived through `/teamme:queue`, never reached step 3 and so never reaches here at all - those stay
cheap without any guard of their own.

**This step moves no phase transition.** The phase stays `approved` from step 4 until step 7
releases it, for the whole cycle below.

### The cycle

You run this cycle. If the work spans lanes and you are handing it to {{ORCHESTRATOR}} at step 6,
you may hand it this cycle to run and report back instead - it is the same cycle either way, and
whoever runs it is the one who decides what to act on.

1. **Dispatch each lane for design only.** Give it its part of the brief as a *contract* - what must
   be true when it is done, its done criteria, the invariants in reach - and ask for the design, not
   the work: the approach it chose, what it rejected and why, what it is assuming, what it is
   uncertain about. Say explicitly that it writes no code this round.
   Dispatch only the lanes whose part of the brief **leaves real implementation freedom**. That is a
   property of the contract, never of which lane it is: a contract that fully determines its own
   output - make this exact edit, record this exact entry - leaves no design to state, so that lane
   says so in one line and waits for step 6. Judge each part against the contract you wrote for it.
2. **Put the designs to `devils-advocate` in two separate spawns, both over this same set of
   returned designs.**
   - **Mode 1 - one spawn per lane.** That lane's design, and its **role framing** mounted with it:
     who the lane is, what it owns, what expertise is being questioned. Take that framing from the
     lane's own agent definition and from the part of the brief you wrote for it - do not paraphrase
     it from memory. Without it the advocate asks the generic version of every question; with it, it
     asks in that domain's own vocabulary.
   - **Mode 2 - one spawn holding every design at once**, with no single-lane framing at all. Its
     job is what happens *between* designs: contradiction, overlap, the same thing built twice, and
     two lanes picking different means to the same end where either would have done - which no
     single-lane pass can see, because each of those designs is defensible on its own. It asks
     whether the divergence is load-bearing; it never mandates convergence.
   Two spawns, never one context doing both: per-lane detail crowds out the cross-cutting view, and
   cross-lane framing dilutes the domain-specific question. **Do not run them serially** - not mode 1
   first and then mode 2 over the revised designs, however tidy that reads. A revision made in answer
   to mode 1 can introduce a fresh collision, which would demand another cross-lane pass, which
   prompts more revisions: that is the unbounded loop the one-round rule exists to prevent, arriving
   through the back door of a sensible-looking sequence.
   **A single-lane brief runs mode 1 only.** There is nothing for mode 2 to compare, and spawning it
   to say so is waste.
3. **Merge both modes' questions, then relay each to the lane it names.** Merging before relaying is
   what keeps this one round: a lane answers mode 1's questions about its own design and mode 2's
   about its boundary with another lane in a single pass. The advocate never talks to a lane and
   never dispatches one; every hop is yours. The lane either revises its design or justifies why its
   approach was best.
4. **Decide, and decide alone.** Accept a revision, overrule the objection, or hand the fork to the
   user - your call in every case. The advocate decides nothing and blocks nothing. "No objection" is
   a real and common result. One round is the default; a second happens only when an *answer* exposed
   a fork that did not exist before, never to re-press a question already answered. If a lane and the
   advocate still disagree after that, it is a fork for you to settle or put to the user, not a
   debate to continue.
   Putting a fork to the user usually means asking and carrying straight on. If instead you stop
   there and wait, `worklog.py block <id> "<the fork>"` and **leave the phase at `approved`** - the
   answer arrives as a mid-flight message and folds in at step 0b, and the lock expires on its own if
   it never comes. Do not `release`: the brief is still live and step 7 is still its one way out.

If this project's roster has no `devils-advocate` - it is opt-in at install time - run the cycle
above with yourself in the advocate's seat: collect the designs, ask both passes' questions of the
lanes yourself - the per-lane ones and the between-lanes ones - then decide. Say in one line that
the advocate is not installed, rather than reporting a round that did not happen.

**Record the exchange in the work log, not in hand-backs.** Each lane's design, the advocate's
questions and each answer go in as notes on the task. Reasoning is not recoverable afterwards: an
approach chosen and an alternative rejected that live only in a hand-back are gone at the next
compaction, and that note is then the only record of why this path was taken over the one the
advocate proposed.

**None of this goes to the user for approval.** The brief was confirmed once, at step 4. This cycle
runs inside dispatch and is reported at step 7; the only thing that reaches the user here is a
genuine fork you chose to hand them.

## 6. Dispatch

Hand the approved brief - the spec quotes, file paths, ordered plan, and whatever step 5 settled -
to {{ORCHESTRATOR}} when the work spans lanes, or straight to the single owning specialist when it
does not. Never start a dependent lane before the lane it depends on has landed.

## 7. Report and close

State what changed per layer, what the docs now claim, and the honest verification status.

```bash
python3 .claude/hooks/worklog.py done <task-id>
python3 .claude/hooks/intake-state.py release
```

Mark the task before releasing: a `Stop` hook refuses to end the turn while anything is still
`active`, so an unmarked task is caught rather than forgotten. Stopped part-way?
`worklog.py block <id> "<what it is waiting on>"`. Then `worklog.py list` and offer the next one.
