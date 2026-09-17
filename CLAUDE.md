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
  commands/build-agent-team.md    the command users invoke; a prompt, not code
  templates/                      scaffolding the command COPIES into a target project
    hooks/*.py                    project-agnostic hook scripts
    intake.md                     skeleton with {{PLACEHOLDER}}s the command fills in
    settings.hooks.json           the hooks block merged into the project's settings.json
scripts/validate.sh               manifests + hook syntax + end-to-end smoke test
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

CI runs exactly this. It compiles every hook and exercises the scaffolding in a throwaway project:
fail-open with no intake active, deny during grounding, lift on approval, `Stop` firing exactly
once. If you change a hook, this is the proof — not inspection.

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
5. **The command copies scaffolding, it does not re-author it.** Re-deriving five hook scripts from
   memory each run is how an install ends up subtly broken. `commands/build-agent-team.md` must keep
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
  install from GitHub in one step. A user-level `~/.claude/commands/build-agent-team.md` would
  shadow the plugin's copy — delete those duplicates.
- **Two state files, deliberately.** `intake-state.py` is a transient phase *lock* (gitignored,
  expires). `worklog.py` is a durable *record* (tasks, priorities, statuses, notes). Merging them
  would either make the lock un-expirable or make the ledger disposable.
- **Intake can say no.** The disposition step lets it decline or defer a request, with the reason
  and the nearest legitimate alternative. That authority is the point; do not weaken it into
  always-accept.

## Known gaps

- No eval suite yet (`claude plugin eval`). The command is a prompt whose quality is untested
  beyond the scaffolding smoke test.
- `templates/intake.md` has been exercised on one real project (a Minecraft Fabric mod). The
  `{{PLACEHOLDER}}` set may not fit stacks with very different doc conventions.
- The plugin ships no `agents/` of its own by design — teams are generated per project — so nothing
  validates the *generated* agent files beyond frontmatter parsing.
- Version bumps are manual: `plugin.json` `version` plus a `CHANGELOG.md` entry.

## Conventions

Commits: one imperative, sentence-case line; no scope prefix. Keep `CHANGELOG.md` current for
anything user-visible.
