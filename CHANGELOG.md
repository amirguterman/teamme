# Changelog

All notable changes to this project are documented here.
This project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.10.0] - 2026-09-25

### Added
- `/teamme:modify-team` command: change an **existing** team — add a lane, drop one, retool or rename one — without re-running the installer. It migrates open tasks' `lane` fields in the work log when a lane is dropped or renamed, checks all four copies of the roster (agent files, README table, intake.md lane table, and task lane fields) before and after each change, and moves a dropped agent's file to `<name>.md.disabled` rather than deleting it. Two commands (`init-team.md`'s Phase 0 and `team-doctor.md`'s state table) and two READMEs told users never to re-run the installer over a working team; this is the missing alternative they all pointed at.
- `preflight.py roster` subcommand: a separate diagnosis that does not change `preflight.py check`'s exit code or state, and never halts `/intake`. It compares the roster in four places and marks three items `PASS`, `FAIL`, or `SKIP` (meaning "not verified, naming what it could not read"). Exit 0 iff all three PASS, so a `SKIP` exits non-zero exactly like a `FAIL` — but they mean different things: `FAIL` says the roster has drifted, `SKIP` names what it could not read. Closed tasks naming a dead lane are exempt. The check stays deliberately loose: it only verifies each agent name appears as a whole word somewhere in `intake.md`, not that a lane table was parsed, because hunting for a particular table shape inside project-specific prose would be a check that passes for the wrong reason. **Nothing surfaces roster drift on its own — you see it only when you run `/teamme:team-doctor` or the `preflight.py roster` subcommand.**
- `/teamme:team-doctor` now runs the roster check after the install check and reports both verdicts, under separate headings.

### Known limitations
- The roster check is deliberately coarse: it never catches a leftover `intake.md` row for a just-dropped lane, a row whose description has drifted, or stale prose in the roster README's tool rationale or dependency-order section. It checks whether agents and rows exist in both places (both directions, including extra rows) and whether open tasks name a live lane — the most obvious kinds of drift, not an exhaustive inventory.

## [0.9.0] - 2026-09-25

### Added
- `worklog.py` gained two commands for task lifecycle management: `retitle <id> "<new title>"` replaces the title and appends the old one as a note, for correcting a title whose scope has shifted; and `reopen <id> "<reason>"` deliberately returns a CLOSED task to open with a required explanation, the only sanctioned way to reopen a task.
- Librarian tool results can now carry an `UNPROTECTED` block when an index was opened but `teamme` could not put an ignore rule in place for it in `.gitignore`. It never sets `isError`, never raises, never stops an index opening, and a healthy project never sees it — this is a visibility feature only, naming when the `.gitignore` file itself is unreadable or unwritable (file permissions, encoding errors, missing parent directory, disk full, or similar).
- Agents without `Bash` in their tool list now have a sanctioned path — the `teamme_worklog` MCP tool — to contribute to the work log. The tool works on agents with or without command execution. (A separate gap remains: an agent without `Bash` cannot run its own verification steps, a lane property the brief-writer must name. The `/intake` template now instructs: when naming who verifies a lane, say so plainly rather than leaving it unattributed to an agent who cannot verify.)

### Changed
- **Exit code 4 is a behaviour change for anything scripting `worklog.py`.** A caller that ran `start`, `dispatch`, `block`, `unblock`, or `defer` on a CLOSED task and got exit 0 now gets exit 4, naming `reopen` as the required path. (Correction between conclusions — `done`, `decline`, or `drop` on an already-closed task — still works at exit 0.) This is the only way to prevent a task from silently reopening during a status change, which would re-arm the `Stop` hook reminder and land it back in the next `SessionStart` list with nobody told.
- `ignore_guard()` in the librarian store module gained a loose-module import fallback (`try: from . import config / except ImportError: import config`), so it can import config even when loaded as a loose module rather than through a package import. The fallback was missing before and caused the guard to degrade silently when store was run with server/librarian on sys.path but outside a package context.
- `/intake` template expanded: added a preflight validation section that explains the four install states (`not-installed`, `installed-outdated`, `installed-not-live`, `live`), names common failure modes (missing scripts, unparseable settings, unwritable directories), and explains when repair rather than re-installation is the correct move. This repo's own `/intake` command (`.claude/commands/intake.md`) was brought up to the shipped template.

### Known limitations
- `UNPROTECTED` appears only when the ignore rule cannot be written. A successful refresh prints `protected: wrote <entries> to <file>`; a query that does not try to write reports nothing about protection, leaving it to the last refresh's output.

## [0.8.0] - 2026-09-25

### Added
- `librarian-gate` hook: fires on `git push` to ask when the project's history index is behind `HEAD`. It names how many commits are unindexed and suggests refreshing with `teamme_librarian_refresh {"librarian": "history"}`. This is a **reminder, never a block** — approving the prompt proceeds with the push exactly as it would if the hook did not exist. Silent when no index exists, the index is current, or the history librarian is disabled (disabling it also makes `teamme_librarian_refresh` and `teamme_librarian_query` refuse; there is no reminder-only switch).
- `.claude/librarians/history/indexed_head` marker file: written by the indexer beside the append-only record, naming the commit hash that was last indexed. Read by `librarian-gate` to compare against `HEAD` without needing to open the SQLite database.

### Changed
- **Upgrade note: every existing install is now `installed-outdated`.** A seventh hook script (`librarian-gate.py`) now ships; a complete install from 0.7.0 has six. This is the designed behaviour — missing hook scripts are repair conditions, never install conditions (see CLAUDE.md, T23 for the reasoning). Repair with `/teamme:team-doctor` or the `teamme_install` MCP tool. **Never `/teamme:init-team`**, which re-runs the questionnaire and regenerates the roster over a team that already works.
- `.gitignore` protection now covers `.claude/librarians/*/indexed_head` in addition to `index.db` and `sessions/`. The marker is always ignored, like the database — it is machine-local derived state (what this machine indexed) and sharing it would hand a teammate a marker already behind the commit carrying it.

## [0.7.0] - 2026-09-25

### Added
- `teamme_librarian_query` now serves four cross-index queries that answer *"what happened around this file / commit / task / time"* by joining the history index, session index, and work log. Query names: `around_path` (everything all three stores hold about one file or directory), `around_commit` (one commit, the conversation near it in time, and the tasks open when it landed), `around_task` (one task's own record, the commits that landed in its window, and the session region it was worked in), and `timeline` (everything all three stores hold between two instants). Each row carries its own store and address, so the next question can fetch detail with `commit_detail` or `window`.

### Fixed
- **PRIVACY DEFECT shipped in 0.6.0:** librarian indexes were only gitignored if `teamme_librarian_configure` was called. A user who ran only `teamme_librarian_refresh` could end up with conversation transcripts — containing everything typed in the session, including accidentally-pasted secrets — in `.claude/librarians/sessions/` checked into git, while documentation promised that directory was always ignored. The guarantee now lives where an index is created: `.gitignore` entries are written on every index open, not just on configuration. Both history and sessions indexes are protected by this change. Existing 0.6.0 users should check whether `.claude/librarians/` is in their `.gitignore` (it should be, whether or not they called `configure`); a refresh under 0.7.0 will put the rule in place without touching lines you wrote yourself.

### Known limitations
- **Links are time overlap, not causation.** A commit that landed while a task was open is reported as "active while" — it is not evidence that the commit implements the task. Nothing in these three stores records that link today. A future version may stamp commit hashes at task status transitions.
- **A task's active window is inferred, not recorded.** The work log stamps when a task was created and when its status last changed; nothing else. A task's window defaults to padding by 15 minutes either side (adjustable with `pad_minutes`) to account for the delay between a status change and the commit or message it refers to. Each row reports which inference was used.
- **If a store is empty, absent or disabled, the answer says so** rather than looking complete. The one exception is `around_commit`, which refuses outright (it cannot resolve its anchor without the history index).

## [0.6.0] - 2026-09-25

### Added
- MCP tools `teamme_librarian_status`, `teamme_librarian_query`, and `teamme_librarian_refresh` now also cover session transcripts in addition to git history. Four queries on sessions: `sessions` (list sessions with turn counts), `search_turns` (search turn text across sessions and subagent threads), `window` (fetch a bounded slice of turns around a mark point), and `compaction` (list compaction boundaries and report what was dropped to compaction). Incremental indexing of append-only `.jsonl` transcript files costs zero per-turn latency; cold indexing 35.6 MB across 47 transcripts takes 0.51 s.
- Session mark points: turns are tracked by sequence number per session, indexed for later retrieval after compaction. Each mark records the turn number, its content type (prompt / message / tool / file / compaction), and character bounds in the session transcript.
- Subagent transcript indexing: transcripts live in a parallel sidecar tree (`<session>/subagents/agent-*.jsonl`); the indexer treats each as a child session with its parent tracked, so a question about what happened in a specialist's thread can be answered without re-reading the multi-megabyte transcript.

### Changed
- `teamme_librarian_configure` now accepts `{"librarian":"sessions","enabled":true/false}` in addition to history librarian settings. The sessions librarian is **enabled by default** — use `{"librarian":"sessions","enabled":false}` to disable it per project. No data is indexed until the first `teamme_librarian_refresh {"librarian":"sessions"}`; `teamme_librarian_status` reports `data: no` for it until then.
- Session index storage (`.claude/librarians/sessions/`) joins `.claude/librarians/index.db` in being written to the shared `.gitignore` block and is never a choice point — it is always gitignored regardless of `commit_record`, since a transcript holds everything typed in the session including secrets pasted by accident. The append-only record (`.claude/librarians/history/commits.jsonl`) respects `commit_record`, but transcripts never do.

### Known limitations
- **Reasoning is not recoverable.** Assistant thinking is stored with an empty body in the transcript — every one of 257 blocks in one measured session. Only what was said out loud can be searched.
- **Tool output is not indexed.** Command output and file contents are most of the bytes and would drown search; indexing a truncated prefix would give a confidently wrong "not found" verdict, so neither is indexed.
- **The index is never committed**, whatever `commit_record` says. A transcript holds everything anyone typed, including secrets pasted by accident, so the session store is machine-local and disposable by design.

## [0.5.0] - 2026-09-24

### Added
- Three co-change queries on the history index: `changes_with` (files that most often change in the same commits as a path, ranked by frequency), `coupling_between` (inspect a claimed edge commit by commit), and `hotspots` (most-changed paths, optionally within a directory). These queries answer correlation over the commit stream, work on any language or config without parsing, and cost no new indexing — they run on the `files_changed` rows phase 1 already built. **Critical limits:** an empty result means no evidence of coupling, not "nothing uses this" — a real dependency that has simply never changed produces no edges at all, so stable code is invisible to this signal. Test files shown as changing with source files "change with" the code, never "cover" it — the index contains change history, not execution data.
- `history-librarian` now knows all nine queries: the original six (`recent`, `commits_touching`, `files_in_commit`, `commits_between`, `search_subjects`, and `commit_detail`), plus the three new co-change queries. A single agent consults history for both temporal and change-coupling questions.

### Changed
- Co-change queries exclude commits touching more than 25 files by default (treating them as sweeps: reformats, renames, license-header passes, initial imports). Every answer reports how many commits were considered and how many were skipped as too broad, so the evidence behind each ranking is visible. The threshold is overridable per call with `max_files`. Vocabulary throughout is "changes with", never "depends on" or "imports" — the queries find correlation in the commit stream, not dependencies in source.

## [0.4.0] - 2026-09-24

### Added
- `history-librarian` agent: the first agent teamme has ever shipped, installed in every project. Answers *what changed, when, and why* from the librarian index, citing commit SHAs instead of re-deriving `git log`. Consultation by team agents is instructed, not enforced.
- `teamme_librarian_configure` MCP tool: enable or disable the librarian per project (disabling is a tool refusal, not enforced by the harness), and choose whether to commit the index record alongside code. Enacts the storage choice by writing or removing `.gitignore` entries. Never removes a `.gitignore` line it did not write, so if your own matching rule already exists outside teamme's block, `commit_record: true` will not take effect — the tool reports this rather than silently failing.
- `commit_detail` query: retrieve a single commit's full record including body, capped at 4000 characters with an explicit truncation notice.

## [0.3.0] - 2026-09-24

### Added
- MCP tools for librarian querying: `teamme_librarian_status` (index metadata), `teamme_librarian_refresh` (update commits), `teamme_librarian_query` (search). These drive an upcoming librarian layer for project history analysis — the underlying substrate is new, the agents and hooks that use it are not yet shipped.
- Per-project librarian index stored under `.claude/librarians/`: an append-only `commits.jsonl` that records what was indexed, and a SQLite `index.db` (gitignored, rebuildable from text without git access) that accelerates queries. Incremental refreshes read only commits since the last indexed hash, costing ~12 ms per refresh when the index is current; a cold index of a 10k-commit repo costs ~15 s, a one-time expense.

### Fixed
- `/mcp` displayed server version as `0.1.0` in every release, regardless of the installed version. Version is now read live from the manifest and cannot drift.

### Changed
- `validate.sh` now reaches the new librarian subpackage via tree walk instead of shallow glob, ensuring all syntax checks run. The validation line that compiles Python code now explicitly verifies the full `server/` directory tree.

## [0.2.2] - 2026-09-24

### Fixed
- `/intake`'s own in-project preflight now detects a differing hook, removing the limitation documented in 0.2.1. Preflight resolves the plugin's templates from the harness's own install record as a last resort, so the freshness check works everywhere the plugin is reachable.

### Changed
- Hook freshness is looked up live, never recorded at install time. The plugin cache keeps old versions side by side, so a path captured during install would survive an upgrade still pointing at the version the user installed from. Every lookup failure degrades to existence-only checking (reported as "not verified") rather than guessing.
- Install scope determines reach: a user-scope install (the default) resolves the templates everywhere, so freshness is checked in every project; a project-scope install resolves only for that project, and the check degrades elsewhere. The check result names its source so users can tell which happened.

## [0.2.1] - 2026-09-24

### Fixed
- Preflight only checked that hook scripts *existed*, not that they matched the copies the plugin ships. A project could carry scripts from an older release, be repaired, and report `live` while still running the old ones (including a router that told every project to read an `ARCHITECTURE.md` it does not have). Preflight and the installer now share one comparison, so the health check and the installer cannot disagree.
- A combined missing-and-differing fix line could slip past the guard that scrubs `/teamme:init-team` out of an existing install's advice, so an install that was both could still be told to run the installer over its own team.

### Changed
- Missing hook scripts and differing ones are reported separately, with separate fixes. A hook that merely differs is left alone by a plain repair on purpose — it may be a deliberate local edit — and replacing it takes `force=true` (or `/teamme:team-doctor`).

### Known limitation
- Freshness is only checked where the plugin's templates are reachable: `teamme_status`, `teamme_install`, `/teamme:team-doctor`. Inside a project, `/intake`'s own preflight normally cannot locate them, degrades to an existence-only check and passes. So `/intake` halts on a *missing* hook but not on one that merely differs. Users deciding whether to upgrade should learn that from this changelog entry.

## [0.2.0] - 2026-09-24

### Removed
- `/build-agent-team` command — replaced with `/teamme:init-team` for clearer namespace.

### Added
- `/teamme:init-team` — renamed install command that scaffolds the agent team and intake flow.
- `/teamme:team-doctor` — diagnoses an existing install and offers to repair it.
- `/teamme:queue` — parks a request in the work log and stops: one task, one line printed, no
  grounding, no analysis, no brief. Never touches the phase lock.
- MCP server (`teamme_mcp.py`) with tools: `teamme_status` (always available), `teamme_install`
  (scaffolds and repairs, idempotent), and `teamme_worklog` / `teamme_intake_phase` (guard-gated).
  Added `dispatch` action to `teamme_worklog` for managing the new `dispatched` status.
- Preflight check module (`preflight.py`) distinguishing four install states: not-installed
  (no hooks block), installed-outdated (hooks registered but release added new ones), installed-not-live
  (hooks block present but `SessionStart` not yet run them), and live (validated by `SessionStart`
  heartbeat). Evidence-based existence checking prevents prior releases from being falsely marked as
  not-installed on upgrade.
- Preflight validation step at the start of every command, halting with a repair recommendation
  list when the install is broken.
- `dispatched` work-log status — "in flight with another agent", counted as unfinished, skipped by
  `next`, never nagged by the `Stop` hook, reported separately at session start. CLI
  `worklog.py dispatch <id>` and MCP `teamme_worklog` tool.

### Fixed
- `route-to-intake.py` no longer hard-codes project-specific document references
  (was `ARCHITECTURE.md`).
- Passthrough regex now matches the plugin-prefixed form (e.g., `/teamme:init-team`) so it does not
  trigger false intake routing.
- Intake flow terminal dispositions now release the phase lock, avoiding write-locks that survived
  until the one-hour timeout.
- Work log locking: parallel agents doing read-modify-write no longer silently drop each other's
  notes. Writes are now atomic, guarded by a lock that expires on a crash and fails open.
- `Stop` reminder was stamped against `updated`, which every mutation touched, so recording
  progress re-armed it. It now stamps against `status_changed`, so only real status transitions
  re-arm it.
- Prompt router no longer injects full reminder for null, empty or whitespace-only prompts.
- `preflight.py heartbeat` drained stdin with a blocking read and could hang indefinitely on a
  pty; it now exits in well under a second in every case.
- `/intake` now tests for a deferral signal ("later", "once you finish") before the phase check,
  taking the cheap queue path instead of grounding the request.

### Changed
- `validate.sh` now compiles the MCP server, validates `.mcp.json`, drives the server over a real
  JSON-RPC pipe to prove access gates lift after `teamme_install`, and exercises `preflight.py`
  through all three states with each check failing independently.

## [0.1.0] - 2026-09-17

Initial release.

### Added
- `/build-agent-team` — analyzes a project and proposes a subagent roster derived from its real
  layer boundaries, gated behind an approval questionnaire.
- `/intake` scaffolding — the single entry point for requesting work, with mid-flight triage
  (fold in / queue / redirect) and a disposition step that can defer or decline a request.
- Harness-enforced read-only grounding phase (`intake-guard.py` on `PreToolUse`) so nothing can
  start implementing while a brief is still being written. Fails open in every other case.
- Durable, prioritized work log (`worklog.py`) with statuses, priorities, owning lanes and notes.
- `SessionStart` and `Stop` enforcement hooks so unfinished work survives sessions and task status
  gets recorded rather than drifting. The `Stop` reminder fires at most once per status change.
- `scripts/validate.sh` and a GitHub Actions workflow validating manifests, hook syntax and the
  scaffolding end to end.
