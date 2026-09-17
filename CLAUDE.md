# teamme

A Claude Code **plugin** that gives any project a tailored subagent team, an `/intake` front door,
and an enforced work log. Published from this repo as its own marketplace.

Install (for users): `/plugin marketplace add amirguterman/teamme` then
`/plugin install teamme@teamme`.

## Layout

```
.claude-plugin/marketplace.json   the marketplace this repo publishes
plugins/teamme/
  .claude-plugin/plugin.json      the plugin manifest (name must match the marketplace entry)
  .mcp.json                       the MCP server this plugin ships, launched with plain python3
  commands/init-team.md           installs the team; a prompt, not code
  commands/team-doctor.md         diagnoses/repairs an existing install on demand; a prompt, not code
  server/teamme_mcp.py            stdio JSON-RPC MCP server: status/install/worklog/intake-phase tools
  templates/                      scaffolding the commands COPY into a target project
    hooks/*.py                    project-agnostic hook scripts, including preflight.py
    intake.md                     skeleton with {{PLACEHOLDER}}s the command fills in
    settings.hooks.json           the hooks block merged into the project's settings.json
scripts/validate.sh               manifests + hook/MCP syntax + preflight states + end-to-end smoke test
```

## Working in this repo: all requests go through `/intake`

This repo has teamme installed on itself (`.claude/agents/`, `.claude/commands/intake.md`,
`.claude/hooks/`). Every request for work enters through `/intake <what you want>` — it grounds the
request in the invariants below, decides whether it should happen, writes a brief, and dispatches the
owning specialist. A request made directly to an agent belongs in the intake flow instead. See
`.claude/agents/README.md` for the roster and dependency order.

`.claude/hooks/*.py` are **copies** of `plugins/teamme/templates/hooks/*.py`. Change the template,
then `cp plugins/teamme/templates/hooks/*.py .claude/hooks/`. Editing the copy alone ships nothing.

## Verify before you claim anything works

```bash
./scripts/validate.sh
```

CI runs exactly this. It compiles every hook and the MCP server, checks that `.mcp.json` parses, and
exercises the scaffolding in a throwaway project: fail-open with no intake active, deny during
grounding, lift on approval, `Stop` firing exactly once. It also drives the MCP server over a real
JSON-RPC pipe — `teamme_worklog` refuses with an error naming `teamme_install` before install and
succeeds after — and drives `preflight.py` directly: `not-installed` with a non-zero exit on an
empty project, `live` on a scaffolded-and-heartbeated one, and the `hooks`, `settings` and
`intake_dir` checks each failing independently with a `fix:` line. `heartbeat` is proven silent and
always exit-0, even with no `.claude/` and stdin closed. If you change a hook or the MCP server, this
is the proof — not inspection.

## Design invariants

These are not style preferences. Breaking one ships a trap to someone else's machine.

1. **Hooks fail open.** Missing, malformed or stale state, an unparseable payload, a path outside
   the project — every one of these must ALLOW the write. A broken guard must never block work. The
   phase lock also expires on a timeout so a crashed session cannot leave a repo write-locked.
2. **Enforcement hooks cannot loop.** A `Stop` hook that re-fires on an unchanged condition traps
   the session. The reminder stamps itself against the task's `updated` time; a status change
   re-arms it.
3. **Assume nothing is installed.** `python3` only. No `jq` — an early version used it and silently
   produced nothing on a machine without it, which is indistinguishable from a hook not firing.
   Pipe-test every command with a synthesized payload before wiring it into settings.
4. **`templates/hooks/` stays project-agnostic.** No project names, paths or stack assumptions.
   Project-specific content belongs in the `{{PLACEHOLDER}}`s of `templates/intake.md`.
5. **The command copies scaffolding, it does not re-author it.** Re-deriving the hook scripts from
   memory each run is how an install ends up subtly broken. `commands/init-team.md` must keep
   saying which files are copied verbatim from `${CLAUDE_PLUGIN_ROOT}/templates/` and which are
   derived from analyzing the project.
6. **Edits under `.claude/` are always permitted** by the guard, so the flow can manage its own
   state and configuration.

## Decisions already made, and why

- **Not plan mode.** Plan mode's read-only status is inherited by subagents, so running `/intake`
  inside it would freeze the very specialists intake exists to dispatch. `teamme` enforces the same
  guarantee with its own phase lock and lifts it exactly at approval. Do not "simplify" this back
  into `EnterPlanMode`.
- **A plugin, not user-level command files.** Plugins ship commands, agents and hooks together and
  install from GitHub in one step. A user-level `~/.claude/commands/init-team.md` would shadow the
  plugin's copy — delete those duplicates.
- **Two state files, deliberately.** `intake-state.py` is a transient phase *lock* (gitignored,
  expires). `worklog.py` is a durable *record* (tasks, priorities, statuses, notes). Merging them
  would either make the lock un-expirable or make the ledger disposable.
- **Intake can say no.** The disposition step lets it decline or defer a request, with the reason
  and the nearest legitimate alternative. That authority is the point; do not weaken it into
  always-accept.
- **An MCP server exists for visibility, not enforcement.** A `python3` script cannot report that
  `python3` is missing, and because every hook fails open by design, a teamme install with no
  `python3` is indistinguishable from a working one — the guard never denies, the router never
  routes. `plugins/teamme/server/teamme_mcp.py` is plain stdlib `python3`, launched by the harness
  itself: if `python3` is missing or broken, the process never starts, and the harness reports it in
  `/mcp` with no teamme code having run. This changes nothing about enforcement — with `python3`
  missing the hooks are still dead — it just says that fact out loud instead of leaving it silent.
- **Prerequisite enforcement is command-level refusal and MCP tool-level gating, never a hook-level
  write block.** Extending `intake-guard.py` to deny on missing prerequisites was considered and
  rejected: it would violate invariant #1 (hooks fail open) and could leave an unfinished install
  write-locked. Instead `teamme_worklog` and `teamme_intake_phase` refuse — naming `teamme_install`
  — when the scaffolding is missing, and `init-team.md`/`team-doctor.md` refuse in prompt text.
  Neither is a harness guarantee; a later turn can still skip the refusal. Say so if asked.
- **Three install states, not two.** `not-installed` (no `hooks` block in `settings.json`),
  `installed-not-live` (hooks registered but no `SessionStart` has run them yet — needs `/hooks` or a
  restart), `live` (a `SessionStart` heartbeat proves hooks are firing). Before `preflight.py` these
  were indistinguishable from outside: a freshly registered, never-restarted install looked exactly
  like a broken one.

## Known gaps

- No eval suite yet (`claude plugin eval`). The prompts (`init-team.md`, `team-doctor.md`,
  `intake.md`) are checked only for parsable frontmatter; nothing tests what they instruct, and the
  command-level preflight refusal they describe is prompt text, not a harness guarantee.
- `templates/intake.md` has been exercised on one real project (a Minecraft Fabric mod). The
  `{{PLACEHOLDER}}` set may not fit stacks with very different doc conventions.
- The plugin ships no `agents/` of its own by design — teams are generated per project — so nothing
  validates the *generated* agent files beyond frontmatter parsing.
- `route-to-intake.py`, `reground`, out-of-project paths and stale-state expiry remain unexercised
  by the smoke test.
- `teamme_intake_phase` is not separately gate-tested; only `teamme_worklog` exercises the shared
  gate-on-install path against the MCP server.
- Version bumps are manual: `plugin.json` `version` plus a `CHANGELOG.md` entry.

## Conventions

Commits: one imperative, sentence-case line; no scope prefix. Keep `CHANGELOG.md` current for
anything user-visible.
