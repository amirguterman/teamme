# teamme (plugin)

The plugin itself. See the [repository README](../../README.md)
for installation and a full description.

A Claude Code plugin that gives any project a **tailored subagent team**, a single **`/intake`**
front door, and a **work log that will not let work be forgotten**.

## Install

```
/plugin marketplace add amirguterman/teamme
/plugin install teamme@teamme
```

`claude plugin install` defaults to `--scope user`: available in every project on this machine,
which also lets a project's own `/intake` preflight check compare its installed hooks against the
plugin's shipped copy (see Install and verify below), not just teamme's own commands. `--scope
project` confines the install to just this repo instead, if you want that. Either way, a project
still needs `/teamme:init-team` to be set up — installing the plugin and setting up a project are
different things, and the MCP tools refuse to run until the latter has happened.

Then, in any project:

```
/teamme:init-team
```

## What `/teamme:init-team` does

It reads the project first — manifests, docs, CI, the real build and test commands, the "never do X"
rules — and proposes a roster derived from *that project's* actual layer boundaries, not a fixed list
of job titles. You approve the roster, the model tiers, the tool scoping and the environment cleanup
through a questionnaire before anything is written.

It then installs:

- **`.claude/agents/*.md`** — the approved specialists, each with a tight tool list and a shared
  guardrail block built from the project's own working rules.
- **`.claude/commands/intake.md`** — the only sanctioned way to request work.
- **`.claude/hooks/`** plus the matching `settings.json` hooks.

## The `/intake` flow

`/intake <anything you want>` is the front door. It does not just accept the request — it decides
what to do with it.

1. **Triage** — if work is already in flight, intake decides whether this new message folds into it,
   queues behind it, or supersedes it. You are never asked to triage.
2. **Ground** — reads the project's spec first and says whether the request is already specified,
   already implemented, unspecified, or contradicts a documented rule.
3. **Classify** — maps it onto the real layer boundaries and names the owning agent for each part.
4. **Decide if and when** — intake has the authority to answer *do now*, *do next*, *already
   satisfied*, *defer (spec only)*, *decline*, or *needs input*. Declining a request that would
   damage the architecture — with the reason and the nearest legitimate alternative — is a real
   outcome, not a failure.
5. **Brief and confirm** — a written brief with the layer decomposition, dependency-ordered plan,
   spec delta, risks, done criteria and how the work can actually be verified.
6. **Dispatch** — specialists, in dependency order.

`/intake` also recognizes a deferral before it does anything else: if the request itself says
"later", "once you finish X", "after the release", or "queue this", it skips straight to the queue
path below instead of grounding it — regardless of whether anything else is currently in flight.

### The cheap path: `/teamme:queue`

`/teamme:queue <request> [P0|P1|P2]` (priority defaults to `P1`) is for anything you want on record
for later, not decided on now. It records one task in the work log and prints one line back — the
task id and the title — with no grounding, no analysis, no brief and no questions. It never touches
the phase lock, so it behaves identically whether intake is idle, mid-flight or approved. Judgement
about whether the work should happen still waits for `/intake`, on the day it is actually picked up.

## The hooks

| Hook | Event | What it does |
|---|---|---|
| `route-to-intake.py` | `UserPromptSubmit` | Routes work requests into `/intake`. Phase-aware: a mid-flight message is pointed at the triage rules rather than at starting a fresh brief. teamme's own commands, including `/teamme:queue`, pass through untouched |
| `intake-guard.py` | `PreToolUse` | **Denies every project edit while a brief is still being written** |
| `worklog-enforce.py session` | `SessionStart` | Surfaces unfinished and deferred work so nothing is lost across sessions |
| `worklog-enforce.py stop` | `Stop` | Refuses to end a turn while a task is still marked active, so status gets recorded |
| `preflight.py heartbeat` | `SessionStart` | Stamps evidence that hooks are firing here. Never blocks; always exits 0 |

### Why the read-only phase is not plan mode

Plan mode's read-only status is inherited by subagents, so running intake inside it would freeze the
very specialists intake exists to dispatch. `teamme` enforces the same guarantee with its own phase
lock and lifts it exactly at approval.

Every guard **fails open** — missing, malformed or stale state, an unparseable payload, a path
outside the project — and the phase expires on a timeout. A crashed session can never leave a
repository write-locked. Edits under `.claude/` are always allowed so the flow can manage itself.

The `Stop` hook reminds at most once per status change — recording a note does not itself count as
one, so narrating progress never re-triggers the reminder — and it enforces without any possibility
of looping.

## The work log

`.claude/hooks/worklog.py` is a durable, prioritized ledger — committed to the project if you want
it shared, and independent of the transient phase lock.

```bash
python3 .claude/hooks/worklog.py list
python3 .claude/hooks/worklog.py add "..." --priority P0 --lane <agent>
python3 .claude/hooks/worklog.py dispatch T4 "teamme-hook-engineer"
python3 .claude/hooks/worklog.py block T3 "waiting on a decision"
python3 .claude/hooks/worklog.py defer T5 "spec'd, build later"
python3 .claude/hooks/worklog.py stats
```

Statuses: `open`, `active`, `dispatched`, `blocked` (unfinished); `deferred` (intentionally not
now); `done`, `declined`, `dropped` (closed). `dispatched` means the work is in flight with
another agent — waiting on that agent's result, not on you. It counts as unfinished, `next` skips
it, and the `Stop` hook never nags about it, since nothing this session does can advance it; a
`SessionStart` report lists it separately from tasks that are `blocked` on your own input.
Priorities: `P0` now, `P1` normal, `P2` someday.

## The librarian substrate

Three MCP tools work against a per-project store under `.claude/librarians/`, shared by two librarians
that index different sources. `history` indexes this project's own git commits —
`.claude/librarians/history/commits.jsonl` (the append-only record; commit it or gitignore it, per
project, your choice) and `.claude/librarians/index.db` (a SQLite index derived from the `.jsonl`,
always gitignored, and rebuildable from it with no git access at all — never commit the `.db`, since a
binary file cannot be merged). `sessions` indexes this project's own conversation transcripts instead —
see The session librarian below, where the storage answer is different and is not a per-project choice.

| Tool | Answers |
|---|---|
| `teamme_librarian_status` | What an index holds: for `history`, row counts, the last indexed commit and how far behind `HEAD` it is; for `sessions`, where this project's transcripts were found and how many bytes are indexed |
| `teamme_librarian_refresh` | Brings an index up to date — incremental by default, `full=true` to reindex from scratch. `{"librarian": "history"}` (the default) or `{"librarian": "sessions"}` |
| `teamme_librarian_query` | A bounded, named question, never arbitrary SQL. Nine names ask `history` (`recent`, `commits_touching`, `files_in_commit`, `commits_between`, `search_subjects`, `commit_detail`, `changes_with`, `coupling_between`, `hotspots` — see The history librarian below); four more (`sessions`, `search_turns`, `window`, `compaction`) ask `sessions` and need no `librarian` argument — the query name says which index it belongs to (see The session librarian below) |

These need only a git repository, not teamme's scaffolding, and work whether or not a project has run
`/teamme:init-team`. Measured on this repository's own history (10 commits, 122 file rows): a first
index takes 0.137s, a warm refresh 0.010s, a query 0.03-0.10ms. Measured on a 10,393-commit repository
(52,947 file rows): a first index is a one-time cost of 15.0s; after that, an incremental refresh of
50 new commits takes 0.40s (~8ms/commit), a refresh that finds nothing new takes 0.012s, and a
`commits_touching` query takes 9-11ms. These figures exclude Python interpreter startup.

## The history librarian

teamme ships one agent of its own: `history-librarian`. It is not part of the roster
`/teamme:init-team` generates for a project — "read the git history and answer from an index" is the
same job in every project, so it ships once, with the plugin, present in every project the plugin is
installed in without being chosen. It answers what changed, when, and why: the history of a file or
directory, how a feature evolved across commits, when a convention was introduced, or whether a claim
of the form "X was added in commit Y" actually holds — citing a commit SHA for every factual claim,
from the index above rather than raw `git log`. The list queries in the table above return only the
commit **subject**; when a subject alone does not explain a change, the librarian reaches for
`commit_detail`, which returns the full message body (capped at 4000 characters).

Three more queries read **co-change** off the same commit stream instead of parsing any source:
`changes_with` (the files that most often changed in the same commits as a given path, each row
carrying its own evidence — how many commits it shares, the partner's own commit count, and the most
recent shared commit), `coupling_between` (the commits where two specific paths both changed, so a
claimed edge can be inspected rather than believed), and `hotspots` (the most-changed paths,
optionally under one directory). This is **correlation, not a call graph**: two files that always
change together may share a cause rather than a dependency, so the librarian reports the edge as
"changes with" — never "depends on" or "imports". A commit touching more than `max_files` (default 25)
files is treated as a sweep and left out of every edge count, since one reformat or rename would
otherwise couple everything it touched to everything else; every co-change answer says how many
commits it considered and how many it skipped. Stable code that never changed has no edge here, so a
thin or empty result means no evidence of coupling was found, not that nothing is related. The same
signal covers tests without knowing any test framework: a test file that keeps changing alongside a
source file is the test-to-code edge, reported as "these tests change with this code" — never "these
tests cover this code", since coverage is a claim about what executes and the index has no execution
in it.

Team agents are instructed to consult it instead of reading git themselves —
`/teamme:init-team`'s shared guardrail block and the generated `/intake` command's grounding step
both say so — but that is an instruction, not a gate: nothing blocks a brief that skips it, and
nothing but convention stops another agent with MCP access from calling the librarian's tools
directly instead.

`teamme_librarian_configure` controls it per project:

| Setting | Default | Effect |
|---|---|---|
| `enabled` (per librarian) | `true` | `false` makes `teamme_librarian_refresh` and `teamme_librarian_query` refuse for that librarian and name how to re-enable it; `teamme_librarian_status` keeps reporting the setting either way. The agent itself is always present — a plugin-shipped agent cannot be hidden per project — it is the tools behind it that refuse. |
| `commit_record` | `false` | Whether `.claude/librarians/*/commits.jsonl` (the append-only record) is committed with the code or kept out of it. Flipping it **writes or removes the actual `.gitignore` entry**, inside a block teamme owns alone — it never removes an ignore line it did not write, and if a line outside that block is already ignoring the record, it says so rather than leaving `commit_record: true` silently without effect. `.claude/librarians/index.db`, the derived SQLite index, is always gitignored regardless — it is binary and cannot be merged. |

Both refusals are the same honest posture as the `teamme_install` gate described below, under
Install and verify: a tool declining to act, never a harness-enforced block.

## The session librarian

A second librarian, `sessions`, indexes the session transcripts Claude Code already writes to disk —
one append-only JSONL file per session, including a sidecar file for every subagent it dispatches.
After a compaction, ask what fell out of context and fetch it back by address — a **mark point** —
instead of re-reading a multi-megabyte transcript file. Indexing is lazy: nothing is captured as it
happens, because the harness already captured it; `teamme_librarian_refresh {"librarian": "sessions"}`
reads only the bytes appended since the last refresh, and no hook exists for this librarian at all.

Four queries: `sessions` (this project's sessions, newest first, with turn counts and date spans, and
the subagent thread count for each — see the limit below), `search_turns` (a literal substring,
answered as mark points with a short snippet, never the conversation itself), `window` (a bounded slice
of one session around one mark point — the only query that returns transcript text, capped per turn and
in total so a fetch cannot reproduce the problem it exists to solve), and `compaction` (what fell out of
context at the most recent compaction boundary, read from the transcript's own record — no `PreCompact`
hook is needed or used).

Two limits worth knowing before relying on it:

- **Reasoning is not recoverable.** Assistant thinking blocks are stored with a signature and an empty
  body — every one measured so far, across an 8.7 MB transcript. "Why was Y rejected" is answerable
  only from what was said out loud; nothing here can recover what was only thought through silently.
- **Subagent transcripts are usually the bulk of a session, not the main thread.** On a team that
  dispatches specialists, most of the actual work — and most of what a later question asks about —
  happened in a sidecar file: measured at roughly 3x the main thread's size on one real session here.
  `search_turns` and `window` reach into them directly; `sessions` reports each session's subagent
  count and total size rather than listing them by default.

`sessions` is switched on and off with `teamme_librarian_configure` the same way `history` is (see the
table above), but its storage answer is not a choice: `commit_record` does not reach it.
`.claude/librarians/sessions/` is always gitignored — a transcript can hold anything anyone typed,
including a secret pasted in by accident — and keeps no second text copy of a conversation; the SQLite
index is the only artifact, disposable, and rebuilt by reading the transcripts again.

Unlike `history`, nothing yet tells an agent to consult `sessions`: `history-librarian` and the
generated `/intake` command's grounding step both name `history-librarian` for history questions, but
neither mentions the session index. The tools work when called; nothing in the roster calls them yet.

## Install and verify

Requirements: `python3` on `PATH`, and nothing else. The hooks and the bundled MCP server
deliberately avoid `jq` and any third-party package, so they work on a bare machine.

A project's teamme install is always in one of four states:

| State | Meaning | Remediation |
|---|---|---|
| `not-installed` | no evidence teamme was ever set up here | `/teamme:init-team` — the only state where the installer is the right advice |
| `installed-outdated` | teamme **is** installed here, but part of the scaffolding is missing, or a hook script is present but differs from the plugin's copy — typically an install from an earlier release | repair only — `/teamme:team-doctor` or the `teamme_install` MCP tool. Never the installer: it would re-run the questionnaire and regenerate the roster over a team that already works. A hook that differs is left alone by a plain repair, since the difference could be a deliberate local edit — add `force=true` (or let `/teamme:team-doctor` ask) to replace it with the plugin's copy |
| `installed-not-live` | the hook scripts and `.claude/settings.json` hooks block both exist, but no `SessionStart` has fired them yet — usually because `.claude/` held no settings file when the session started | `/hooks` or restart |
| `live` | a `SessionStart` heartbeat proves the hooks are actually running | none |

`installed-outdated` is real, not cosmetic: the generated `/intake` command halts on any non-zero
`preflight.py check`, and the exit code follows the individual `PASS`/`FAIL` items rather than the
`state:` line, so an outdated install halts `/intake` until it is repaired — even though the team is
already set up and most of it works. This is the state an upgrading user meets first, on the release
that adds the next hook script.

Whether `/intake`'s own check — run from inside the project — can tell a hook that differs from the
plugin's copy apart from one that is current depends on whether it can locate the plugin's shipped
templates. It tries `$CLAUDE_PLUGIN_ROOT`, its own directory, and last, the harness's own record of
where each plugin is installed (read live on every check, never assumed from install time). A
**user-scope** install (the default; see Install above) resolves from any project, so the check
catches a differing hook everywhere; a **project-scope** install only resolves inside that one
project, and everywhere else the check degrades to existence-only, saying explicitly that freshness
was not verified rather than reporting "clean" when it never checked. `/teamme:team-doctor` and the
MCP tools do not depend on any of this — they run inside the plugin itself, where the shipped
templates are always reachable, so they can tell the two apart regardless of install scope.

Check the state at any time with **`/teamme:team-doctor`** — it reports every check with a `fix:`
line for each failure, and offers to repair a partial install (missing hook scripts, a missing
`hooks` block, a missing `.claude/intake/`), asking before it writes anything.

teamme also ships an MCP server, declared in the plugin's `.mcp.json` and launched with plain
`python3`. If `python3` is missing, that process never starts, and Claude Code reports it in `/mcp`
— that is what makes a missing `python3` visible instead of silently indistinguishable from a
working, but inactive, hook. When the server is connected, `teamme_status` reports the same
four-state diagnosis and `teamme_install` performs the same repair as `/teamme:team-doctor`;
`teamme_worklog` and `teamme_intake_phase` refuse to run until the install is complete, naming
`teamme_install` in the refusal. None of this changes the hooks' own fail-open behaviour — with
`python3` missing, the hooks are still inert either way.

## License

MIT — see [LICENSE](LICENSE).
