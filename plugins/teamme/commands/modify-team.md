---
description: Change an existing team - add a lane, drop one, retool or rename one - without re-running the installer, and migrate the work log's lane fields to match.
argument-hint: <what to change about the team - add a lane, drop one, retool or rename one>
---

The request: **$ARGUMENTS**

This is how an **existing** team changes. The other two commands deliberately do not do this:
`/teamme:init-team` is the installer, and re-running it over a working team re-runs the
questionnaire and regenerates the roster; `/teamme:team-doctor` repairs teamme's *scaffolding* —
hooks, the settings block, `.claude/intake/`, the librarian gate — and never touches
`.claude/agents/` at all. A lane to add, drop, retool or rename is this command.

Change nothing until the user approves the plan in Phase 4. Work in this order. If `$ARGUMENTS` is
empty, do not ask yet: run Phases 0 through 1 first, so the question in Phase 2 is asked against the
roster that actually exists.

## A roster is four copies, and they drift apart

| Copy | Holds |
|---|---|
| `.claude/agents/*.md` | the agent files themselves — frontmatter and prompt |
| the roster README in that same folder | the roster table, the model/tool rationale, the dependency order |
| `.claude/commands/intake.md` | step 2's lane table, which is what `/intake` classifies against |
| `.claude/intake/worklog.json` | each task's `lane` field, written only through `worklog.py` |

Changing one and not the others is the failure this command exists to prevent. The fourth is the one
a wizard gets wrong: renaming an agent in three files leaves every task pointing at a lane that no
longer exists.

## Phase 0 — Preflight

Run this first, before reading a single project file:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/templates/hooks/preflight.py" check
```

Show the output verbatim, then act on the `state:` line:

| State | What to do |
|---|---|
| the command itself fails — `python3: command not found` | **Halt.** Every hook, the work log and the phase lock are `python3` scripts, and this command re-lanes tasks through one of them. Tell the user to install python3 and re-run. |
| `not-installed` | **Wrong command.** There is no team here to modify — no registered teamme hooks, no generated `/intake`. Say so in one line and point at `/teamme:init-team`, which analyzes the project and creates the roster in the first place. Do not hand-assemble an agent folder instead. |
| `installed-outdated` | **Repair first.** Reshaping a roster on top of missing scaffolding is building on sand: a re-laned task is written by `worklog.py`, and the lane table is read by an `/intake` that currently halts. Point at `/teamme:team-doctor` or the `teamme_install` MCP tool, then come back. Continue only if the user says so anyway, and say plainly that the roster edits will land on a broken install. |
| `installed-not-live` | Continue — the roster is files, and files change either way — but tell the user at hand-over that no hook has fired this session, so nothing is being enforced yet and `/hooks` or a restart is still needed. |
| `live` | Continue. |

## Phase 0b — Read the drift you inherited, before touching anything

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/templates/hooks/preflight.py" roster
```

A third subcommand beside `check` and `heartbeat`, taking the same `--project-dir`. It prints one
mark per item — `PASS`, `FAIL` or `SKIP` — and checks three things:

- **the agent files against the roster README's rows, both directions** — a file with no row, and a
  row naming no file. The agents are whoever the files in `.claude/agents/` declare themselves to be
  in their frontmatter `name:`, never the filename stem; a `.md` there with no `name:` is reported
  as ignored rather than silently dropped.
- **the agent files against `intake.md`** — and only that each agent's name appears *somewhere* in
  `.claude/commands/intake.md`, as a whole word. Describe it that way and no further: a name that
  appears nowhere cannot be being dispatched, a name that appears somewhere is only evidence it has
  not been forgotten. It does not locate a lane table, and it cannot tell whether the row says
  anything true — an `intake.md` that names every agent in prose alone PASSes. Nobody tried to do
  better here on purpose, because a check that hunts for "the lane table" inside a per-project,
  prose-tailored prompt is a check that passes for the wrong reason. Its own `FAIL` line is labelled
  `intake.md lane table:` — that is the item's name, not a claim that a table was parsed. Do not
  repeat it to the user as one.
- **every task the work log still counts as open** — unfinished or deferred, never closed — naming a
  lane that is a live agent. A task with no lane at all is counted and reported, not failed.

```
roster consistency
  PASS  6 agents, 6 README rows
  FAIL  intake.md lane table: no row for `app-ui` (the name appears nowhere in .claude/commands/intake.md)
        fix: /teamme:modify-team, or add the row by hand
  PASS  open tasks: 5/5 name a live lane, 1 with no lane
```

**`SKIP` is the mark that matters, and it is not a failure.** It reads `not verified: <reason>`, and
the reason names what it could not read. Usually that is a file — no `.claude/agents/` directory, a
missing or unreadable roster README, a README with no table that names any agent, a missing
`intake.md`, a missing or unparseable `worklog.json` — and sometimes it is not a file at all, when
the check itself failed or the project directory could not be resolved. Quote the reason as written
rather than expecting a path out of it. The mark exists because `PASS` would be a lie and `FAIL`
would be a wrong verdict: the one answer this check must never produce from something it could not
read is "consistent".

**Exit is 0 iff all three PASS**, so a `SKIP` exits non-zero exactly like a `FAIL` does. Never read
the exit code alone — read the marks, and keep the two apart everywhere you report them. A `FAIL`
says the roster has drifted. A `SKIP` says one of the four copies could not be seen.

**Run it before changing anything and show the result.** Two reasons, and say both if asked: drift
that was already there is not this run's doing, and a wizard that silently "fixes" a row the user
never asked it to touch is worse than one that reports it. If it reports pre-existing drift, name it
and ask whether to fold the repair into this run — do not assume either way. An inherited `SKIP` is
neither drift nor something this run caused: say what could not be read, in the check's own words,
and say that Phase 5 will not be able to prove that item either way.

**`roster` is deliberately separate from `check`.** It cannot change the install `state:`, and it
never halts `/intake`. A roster README whose table lost a row costs a stale doc; an install missing
its write guard costs the guarantee `/intake` runs on. Treating the first like the second would
repeat exactly the over-reach that deriving `installed` from a growing hook list paid a P0 to
unlearn. Do not report a `roster` failure as an install problem.

## Phase 1 — Report the roster that exists, from all four copies

Read, do not change:

```bash
python3 .claude/hooks/worklog.py list
```

Plus the agent files, the roster README and `intake.md`'s lane table. An agent is whatever its
frontmatter `name:` says it is — that is what `/intake` dispatches to, what a task's `lane` records,
and what the `roster` check matches on, none of which look at the filename. A `.md` in that folder
with no frontmatter `name:` is not an agent to any of them; report it as what it is rather than
listing it as a lane.

Present one table before proposing anything — a row per agent, and a row per orphan found in any
single copy:

| Agent | File | README row | intake lane row | Open tasks |

For each agent give its model, its tool list and its one-line description as they are written now,
not as you would write them. An entry that exists in some copies and not others is the interesting
row; mark it rather than tidying it away.

Two things are **not** in this roster and must not be listed as though they were:

- `history-librarian` and any other agent the teamme plugin ships. It is present in every project
  the plugin is installed in, generated by nobody, and **not roster-selectable** — a project's own
  config does not control a plugin-shipped agent's visibility. A request to "remove it from the
  roster" is a request about something that was never in the roster: say so, and name the real
  switch, `teamme_librarian_configure` with `{"librarian": "history", "enabled": false}`, which
  makes that librarian's own tools refuse rather than making the agent disappear.
- agents installed at user level (`~/.claude/agents/`) rather than in this project. Name them if
  they exist so the user is not surprised, and say that this command edits the project's roster.

## Phase 2 — Ask what to change

Use the interactive question tool for the genuine fork, not for a choice with an obvious default. If
`$ARGUMENTS` already names the change unambiguously, do not re-ask it — carry it into Phase 4's
confirmation instead. The operations:

| Operation | What it means | What it touches |
|---|---|---|
| **Add** a lane | A layer of this project nobody owns | a new agent file, a README row, a lane-table row |
| **Drop** a lane | A layer that no longer exists, or a lane nothing dispatches to | the file moved aside, its README row, its lane-table row, its open tasks re-laned (Phase 3) |
| **Retool** a lane | Its tool list, model, description or guardrails change | the agent file, and the README prose that explains that choice |
| **Rename** a lane | Same job, different name | all four copies, including every open task's `lane` (Phase 3) |

For an **add** or a **retool**, settle these explicitly rather than guessing:

- **Model tier**, with the reason: opus where a mistake is expensive and subtle, sonnet for an
  implementer working against a clear spec, mechanical bookkeeping cheaper still.
- **The tool list, kept tight** — and one trap in particular. An agent with no `Bash` has no
  work-log CLI, so it needs the work-log MCP tool named in its `tools:` line or it has no sanctioned
  way to write the ledger at all, and what it does instead is edit `.claude/intake/worklog.json` by
  hand against a live lock. Resolve that tool's name from the tools actually available in this
  session rather than from memory; if no such tool is visible here, write no tool name and say at
  hand-over that the lane cannot write the ledger directly. The same cut has a second edge: removing
  `Bash` removes that lane's ability to run this project's own verification command, so name who
  runs it for that lane instead.
- **The shared guardrail block.** A new or retooled agent carries the *same* block as the rest of
  the roster. Copy it from an existing agent file rather than re-deriving it from this project's
  docs — a block that has drifted from its siblings is the same class of bug as a lane table that
  has drifted from the agent files.

## Phase 3 — Migrate the lanes honestly (drop and rename)

A drop or a rename orphans every task that names that lane. Before proposing any file change, list
the affected tasks the work log still counts as open — unfinished or deferred — **by id and title**,
and have the user pick a destination lane for each:

```bash
python3 .claude/hooks/worklog.py lane <id> <agent>
```

A rename usually moves all of them to the new name; a drop rarely does. Leaving a task with no lane
at all is a legitimate answer — `worklog.py lane <id> ""` — and a laneless task is not a defect to
be fixed: `/intake` assigns a lane when it picks the task up, and the `roster` check counts such
tasks in its own PASS line rather than failing them. Do not invent a plausible owner to make a row
look tidy.

If any affected task is `active` or `dispatched`, say so before going further: work is in flight in
a lane that is about to change name or disappear. Finishing or parking it first is usually right,
and it is the user's call.

**Closed tasks are never re-laned and never rewritten.** Not `done`, not `declined`, not `dropped`,
however dead the lane name now is. That agent really did that work, and the ledger is append-only: a
title is current scope and may be corrected, but what happened is not editable. A closed task
naming a lane that no longer exists is an accurate record, not drift — which is why the `roster`
check reads open tasks only. Never edit `.claude/intake/worklog.json` directly to do any of this;
both writers take a lock and concurrent writers are normal, so a hand edit can lose a note that can
then only be superseded, never repaired.

## Phase 4 — Confirm, then execute exactly what was approved

State the full change set — every file, every task id — and **ask before writing anything**. Then,
and only for what was approved:

1. **The agent file.** Write `.claude/agents/<name>.md` with valid YAML frontmatter (`name`,
   `description`, optional `tools`, optional `model`) and a prompt carrying the role, the shared
   guardrail block, the workflow and the done-criteria. Reuse this project's existing conventions;
   do not invent commands that do not exist here.
2. **A dropped or renamed agent's file is moved aside, never deleted.** Rename it to
   `<name>.md.disabled`, in place. Both the harness and the `roster` check read `*.md` from the
   agents folder, so that suffix takes the file out of the roster entirely while the prompt someone
   wrote is still there to restore with a single `mv`. Reversible disables over deletion is the same
   call `/teamme:init-team` makes about skills and MCP servers. Its README row goes in the same
   breath as the rename — an agent that is no longer there with a row that still is, is exactly the
   "a row naming no agent file" half of check 1. Say the new filename out loud at hand-over so it is
   not mistaken for a stray file later.
3. **The roster README — all three parts, not just the table.** The roster row, the model/tool
   rationale prose, and the dependency order. All three go stale on a change and only the first is
   obvious — and only the first is checked, in both directions. A dropped lane still named in the
   dependency order tells the next reader to wait for a lane that no longer exists, and nothing will
   ever flag it.
4. **`intake.md`'s lane table** — step 2's rows, and nothing else in that file. The rest of the
   intake flow belongs to teamme's own template; this command edits the lane table, the one part
   that is per-project by construction. Do not lean on Phase 5 to catch a mistake here: that check
   only asks whether each *live* agent is named somewhere in the file, so a leftover row for a lane
   you just dropped, or a row whose description is now wrong, PASSes. Removing and correcting those
   is this step's job, not the check's.
5. **The lane migration from Phase 3**, exactly the ids the user named, through `worklog.py lane`
   or the work-log MCP tool.

## Phase 5 — Prove it

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/templates/hooks/preflight.py" roster
```

Show the output. **All three `PASS`** is the proof this run worked, and it is the entire reason the
check exists — four copies changed by hand is exactly the job a machine should check afterwards.
Anything else is not that proof, and the two ways of not being it are different things the user
needs told apart:

| Mark | What it means here | What to say |
|---|---|---|
| `FAIL` | The roster is inconsistent right now. | Name the item and the agent it names. Say whether this run caused it or Phase 0b already reported it, and offer to fix it. |
| `SKIP` | Something that item needed could not be read, so it is **unproven, not wrong**. | Quote the `not verified:` reason as written — it names what could not be read, which is usually but not always a file. Do not call the run failed, and do not call it verified either: the change may be perfectly correct and simply unchecked. Say what would make it checkable. |

The exit code cannot tell those apart — a `SKIP` exits non-zero just like a `FAIL` — so report the
marks, not the exit status. **Do not declare success on a `SKIP`**, and do not report one as drift.
A red check reported as green is worse than the drift it was about; an honest "this item was not
verified" is the whole reason the mark exists.

## Phase 6 — Hand over

- What changed, file by file, and which agent file was moved aside and to what name.
- Which tasks were re-laned, by id, and which were deliberately left laneless.
- The `roster` output from Phase 5, and — if any item came back `SKIP` — what could not be read and
  what that leaves unproven. Do not summarise a `SKIP` as "verified".
- Whether the user wants it committed. **Do not assume**, and do not carry an earlier approval
  forward. A roster is often personal tooling that belongs outside a shared repo.
- If the state was `installed-not-live`, that `/hooks` or a restart is still owed.

## What this command does not touch

- **Not hooks, not `.claude/settings.json`, not `.claude/librarians/`.** A roster change is not a
  scaffolding change: `/teamme:team-doctor` owns the first and `teamme_librarian_configure` owns the
  last. Never re-author a hook script from memory here.
- **Not the teamme plugin's own agents.** Nothing under the plugin's `agents/` is a project's to
  edit; those ship with the plugin and are present everywhere it is installed.
- **Not the work-log JSON by hand**, and not a closed task, ever.
- **Not `/intake`'s phase lock.** This command runs no `intake-state.py` transition in any form. It
  writes only under `.claude/`, which the write guard always permits, so it works the same whether
  the phase is `idle`, `grounding` or `approved` — but an intake mid-flight is still a reason to
  pause, which is Phase 3's `active`-or-`dispatched` warning.

## Be honest about what this is

A command the user runs, not a gate. Nothing stops someone editing `.claude/agents/` by hand five
minutes from now and putting the four copies back out of step — which is exactly why
`preflight.py roster` exists as a check that can be run again any time, and why
`/teamme:team-doctor` names it. Report what the check observed, and nothing more.
