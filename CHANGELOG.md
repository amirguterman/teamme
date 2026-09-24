# Changelog

All notable changes to this project are documented here.
This project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
