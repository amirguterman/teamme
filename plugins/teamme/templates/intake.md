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
  {{ORCHESTRATOR}}   the orchestrator agent, or delete the sentence if there is none
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

## 2. Classify it against the layer boundaries

{{LANE_TABLE}}

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

## 4. Confirm, then lift the read-only phase

Use the interactive question tool only for genuine forks. Do not ask whether the brief is good. Once
approved - and only then:

```bash
python3 .claude/hooks/intake-state.py approve
python3 .claude/hooks/worklog.py start <task-id>
```

Writes stay denied until you do. If step 2b ended the flow instead - already satisfied, defer or
decline - it has already released the phase, and you never reach this step.

## 5. Dispatch

Hand the approved brief - the spec quotes, file paths, ordered plan - to {{ORCHESTRATOR}} when the
work spans lanes, or straight to the single owning specialist when it does not. Never start a
dependent lane before the lane it depends on has landed.

## 6. Report and close

State what changed per layer, what the docs now claim, and the honest verification status.

```bash
python3 .claude/hooks/worklog.py done <task-id>
python3 .claude/hooks/intake-state.py release
```

Mark the task before releasing: a `Stop` hook refuses to end the turn while anything is still
`active`, so an unmarked task is caught rather than forgotten. Stopped part-way?
`worklog.py block <id> "<what it is waiting on>"`. Then `worklog.py list` and offer the next one.
