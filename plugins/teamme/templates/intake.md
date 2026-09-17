---
description: The single entry point for this project's agent team. Grounds a request in the project's spec, decides if and when it should happen, produces an approved brief, then dispatches the specialists in dependency order.
argument-hint: <what you want, in plain words>
---

<!--
TEMPLATE. /build-agent-team tailors this per project. Replace every {{PLACEHOLDER}}:
  {{PROJECT}}        project name
  {{SPEC_DOCS}}      the authoritative spec/docs to read first
  {{LANE_TABLE}}     layer -> owning agent rows
  {{HARD_RULES}}     the project's "never do X / always do Y" list
  {{ORCHESTRATOR}}   the orchestrator agent, or delete the sentence if there is none
  {{VERIFY}}         the real build/test commands, or an honest statement that none exist locally
-->

# {{PROJECT}} intake

The request: **$ARGUMENTS**

If that is empty, ask what the user wants and stop until they answer.

This is the **only sanctioned way to request work** from the agent team. You are not implementing
anything yet. Turn a plain-language request into a grounded, approved brief, then dispatch it. Do
not write code in this command.

## 0. Triage: is something already in flight?

```bash
python3 .claude/hooks/intake-state.py show
python3 .claude/hooks/worklog.py list
```

The first is the phase lock (transient); the second is the work log (durable, and the queue of
record). If `phase` is `idle`, this is new work - go to step 0b, but check the log first in case it
is already recorded as an open or deferred task.

If `phase` is `grounding` or `approved`, **you decide** what this new message is. Do not ask the
user to choose, and do not start a second brief:

| Decision | When | Action |
|---|---|---|
| **Fold in** | It refines, constrains or corrects the active request, supplies an input a lane is blocked on, or would mean editing the same files twice. | `worklog.py note <active-id> "<text>"`. If it changes the plan, `intake-state.py reground` first so it is re-planned under the write lock, then re-approve. |
| **Queue** | A separate goal with its own layers and no dependency on the active work. | `worklog.py add "<request>" --priority P1`, then resume. Tell the user the task id. |
| **Redirect** | It makes the active brief *wrong*, not merely lower priority. | **Confirm first.** Then `worklog.py block <active-id> "superseded by <new-id>"`, add the new task at `P0`, `intake-state.py release`, and begin the new one. |

Bias toward folding inside the active scope, queueing for anything that widens it. A plain question
changes no state: answer it and stop.

## 0b. Enter the read-only grounding phase

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
| **Already satisfied** | It exists. | Say where, `worklog.py done <id>`, stop - or re-scope to the real change. |
| **Defer, spec only** | Sound but not now. | Write the spec text, `worklog.py defer <id> "<reason>"`, stop. |
| **Decline** | It would break a hard rule. | `worklog.py decline <id> "<reason>"`. Say why, offer the nearest legitimate alternative. |
| **Needs input** | Undecidable without the user. | `worklog.py block <id> "<what you need>"`, ask, stop. |

Declining and deferring are real outcomes, not failures.

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

Writes stay denied until you do. If the outcome was defer or decline, record that and
`intake-state.py release` instead.

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
