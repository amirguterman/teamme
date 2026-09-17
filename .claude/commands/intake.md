---
description: The single entry point for this project's agent team. Grounds a request in the project's spec, decides if and when it should happen, produces an approved brief, then dispatches the specialists in dependency order.
argument-hint: <what you want, in plain words>
---

# teamme intake

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

Read `CLAUDE.md` first — its **Design invariants** and **Decisions already made** sections are
this project's spec, and they exist specifically so a request does not re-litigate a settled call.
Then `CONTRIBUTING.md` (the rules hook scripts must obey), `README.md` (the behavioural promises made
to users, which a change may falsify), and `CHANGELOG.md` (what has already shipped) before designing anything. Quote what already governs the request, then say which
is true: **already specified** (implement as written), **already implemented** (say where; the real
request is probably a change), **unspecified** (the brief must add the spec text), or **contradicts
a documented rule** (stop and say so).

## 2. Classify it against the layer boundaries

| Layer | Files | Owning agent |
|---|---|---|
| Hook scripts — the only real code | `plugins/teamme/templates/hooks/*.py`, `templates/settings.hooks.json` | `teamme-hook-engineer` |
| Prompts — the product itself | `plugins/teamme/commands/*.md`, `plugins/teamme/templates/intake.md` | `teamme-prompt-author` |
| Validation — the only proof | `scripts/validate.sh`, `.github/workflows/validate.yml` | `teamme-validation-engineer` |
| Human-facing docs | `README.md`, `CLAUDE.md`, `CONTRIBUTING.md`, `plugins/teamme/README.md` | `teamme-docs-writer` |
| Packaging and release | `.claude-plugin/marketplace.json`, `plugins/teamme/.claude-plugin/plugin.json`, `CHANGELOG.md` | `teamme-release-manager` |

Note the recursion: this repo has its own `.claude/` scaffolding installed from these same templates.
`.claude/hooks/*.py` are **copies**. The source of truth is `plugins/teamme/templates/hooks/`. A
change made only to the copy will be silently lost; a change made only to the template will not take
effect here until it is re-copied. Say which you mean.

Enforce the project's hard rules here, before any plan exists:

1. **Hooks fail open.** Missing, malformed or stale state, an unparseable payload, a path outside the
   project — every one must ALLOW the write. A broken guard must never block work.
2. **Enforcement hooks cannot loop.** A `Stop` hook that re-fires on an unchanged condition traps the
   session. Anything added here must name what re-arms it.
3. **`python3` only.** No `jq`, no third-party packages, no assumption that a tool is installed.
4. **`templates/hooks/` stays project-agnostic.** No project names, paths or stack assumptions.
5. **The command copies scaffolding, it does not re-author it.**
6. **Edits under `.claude/` are always permitted** by the guard.
7. **Not plan mode.** Its read-only status is inherited by subagents and would freeze the very
   specialists intake dispatches. Do not "simplify" the phase lock back into `EnterPlanMode`.
8. **Intake keeps the authority to decline.** Do not weaken the disposition step into always-accept.
9. **Commits:** one imperative, sentence-case line, no scope prefix. `CHANGELOG.md` current for
   anything user-visible. Never commit unless asked.

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
· **Risks** · **Done criteria** per lane · **Verification**: `./scripts/validate.sh` — manifests, `py_compile` on every hook, the settings-template
event check, and an end-to-end smoke test in a throwaway project (fail-open, deny-during-grounding,
lift-on-approve, `Stop` firing exactly once). CI runs exactly this. There is no other test suite, no
linter and no typechecker.

Be honest about what it does **not** prove: the prompts in `commands/*.md` and `templates/intake.md`
are checked only for parsable frontmatter. There is no eval suite. `route-to-intake.py` is not
exercised by the smoke test at all, nor are `reground`, the `.claude/` exemption, out-of-project
paths or stale-state expiry. A brief that changes any of those must either add the assertion (via
`teamme-validation-engineer`) or state plainly that the change is unverified

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

Hand the approved brief - the spec quotes, file paths, ordered plan - to `teamme-tech-lead` when the
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
