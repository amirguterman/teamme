# Changelog

All notable changes to this project are documented here.
This project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
