# Changelog

All notable changes to this project are documented here.
This project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] - 2026-09-17

### Removed
- `/build-agent-team` command — replaced with `/teamme:init-team` for clearer namespace.

### Added
- `/teamme:init-team` — renamed install command that scaffolds the agent team and intake flow.
- `/teamme:team-doctor` — diagnoses an existing install and offers to repair it.
- MCP server (`teamme_mcp.py`) with tools: `teamme_status` (always available), `teamme_install`
  (scaffolds and repairs, idempotent), and `teamme_worklog` / `teamme_intake_phase` (guard-gated).
- Preflight check module (`preflight.py`) distinguishing three install states: not-installed,
  installed-not-live (hook state not yet proven), and live (validated by `SessionStart` heartbeat).
- Preflight validation step at the start of every command, halting with a repair recommendation
  list when the install is broken.

### Fixed
- `route-to-intake.py` no longer hard-codes project-specific document references
  (was `ARCHITECTURE.md`).
- Passthrough regex now matches the plugin-prefixed form (e.g., `/teamme:init-team`) so it does not
  trigger false intake routing.
- Intake flow terminal dispositions now release the phase lock, avoiding write-locks that survived
  until the one-hour timeout.

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
