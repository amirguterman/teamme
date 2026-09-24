# Contributing

## Run the checks

```bash
./scripts/validate.sh
```

It validates the manifests, compiles every hook and the MCP server, checks that `.mcp.json` parses,
runs the scaffolding end to end in a throwaway project, drives the MCP server over a real JSON-RPC
pipe, and drives `preflight.py` through each of its four install states — including
`installed-outdated` against a synthetic 0.1.0-shaped install, asserting it is never told to run
`/teamme:init-team`. It also checks hook freshness on its own: a present-but-modified hook drives
`installed-outdated` and is named in the failure; with no way to locate the plugin's templates —
including via the harness's own install record, tried last — the check degrades to existence-only and
still passes; a synthetic install record resolves the templates and catches the same stale hook, a
missing or corrupt record degrades instead, and a record pointing at a directory that fails the
template-marker check is rejected rather than trusted; and `teamme_install` leaves a hook that differs
from the plugin's copy alone unless called with `force=true`. CI runs exactly this. Please make it
pass before opening a pull request.

## Layout

```
.claude-plugin/marketplace.json   the marketplace this repo publishes
plugins/teamme/
  .claude-plugin/plugin.json      the plugin manifest
  .mcp.json                       the MCP server this plugin ships
  commands/init-team.md           the command that installs the team
  commands/team-doctor.md         the command that diagnoses/repairs an existing install
  commands/queue.md               parks a request in the work log; no grounding, no phase interaction
  server/teamme_mcp.py            stdio JSON-RPC MCP server
  templates/                      scaffolding copied into a target project
    hooks/*.py                    project-agnostic; do not hard-code a project name
    intake.md                     skeleton with {{PLACEHOLDER}}s the command fills in
    settings.hooks.json           the hooks block merged into the project's settings.json
```

## Rules for the hook scripts

These run on other people's machines, inside their editing loop. They must:

- **Fail open.** Missing, malformed or stale state, an unparseable payload, a path outside the
  project — every one of these allows the write. A broken guard must never block someone's work.
- **Never loop.** An enforcement hook that can fire repeatedly on an unchanged condition will trap a
  session. The `Stop` reminder stamps itself against the task's `status_changed` time, not `updated`,
  for this reason: only a real status transition re-arms it, so a `worklog.py note` — which moves
  `updated` but not `status_changed` — can never re-trigger it.
- **Assume nothing is installed.** `python3` only — no `jq`, no third-party packages. Pipe-test a
  command with a synthesized payload before wiring it into settings.
- **Stay project-agnostic.** No project names, paths or stack assumptions in `templates/hooks/`.
  Anything project-specific belongs in the `{{PLACEHOLDER}}`s of `templates/intake.md`.
- **Never duplicate the freshness comparison.** Whether an installed hook script still matches the
  plugin's shipped copy is decided once, by `hook_freshness()` in `preflight.py`; `teamme_mcp.py`
  loads and calls it rather than reimplementing the byte comparison — two copies is how they end up
  disagreeing. A hook that differs from the plugin's copy is left in place by a plain repair, since
  the difference could be a deliberate local edit; only `force=true` overwrites it. Never call a
  differing hook "outdated" or "wrong" in output or docs — "differs from the plugin's copy" is what
  the check actually knows.
- **Keep the harness-layout assumption in one fenced place.** `preflight.py` locates the plugin's own
  templates via `$CLAUDE_CONFIG_DIR/plugins/installed_plugins.json` as a last resort, and that is
  teamme's one assumption about Claude Code's own on-disk layout — contained between one banner
  comment and the next so it is obvious where to fix it if the harness changes. Read it live on every
  check, never capture it at install time (see `CLAUDE.md`'s "Decisions already made" for why), and do
  not grow a second harness-shaped assumption anywhere else.

## Rules for the MCP server

`plugins/teamme/server/teamme_mcp.py` runs on the same bare-machine assumption as the hooks: stdlib
`python3` only, no third-party `mcp` package or other dependency. It must never crash on a malformed
JSON-RPC line or a client that closes the pipe. It gates `teamme_worklog` and `teamme_intake_phase`
on the scaffolding being installed by refusing — with an error naming `teamme_install` — rather than
by denying a tool call: this server has no hook registration, so it cannot block a write or a prompt
even if it wanted to. See `CLAUDE.md`'s "Decisions already made" for why prerequisite enforcement
lives here and in the commands' prompt text, and never in a hook.

## Changing the commands

`commands/init-team.md`, `commands/team-doctor.md` and `commands/queue.md` are prompts, not code.
Keep `init-team.md` explicit about what must be *copied* from `templates/` versus what must be
*derived* from the project being analyzed — re-authoring scaffolding from memory is how installs end
up subtly broken. Keep `queue.md`'s one-line output contract intact when editing it: it exists so
parking a request costs the user nothing (see `CLAUDE.md`'s "Decisions already made"), and anything
that adds a second line of preamble, analysis or a follow-up question defeats that.
