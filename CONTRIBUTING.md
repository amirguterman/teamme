# Contributing

## Run the checks

```bash
./scripts/validate.sh
```

It validates the manifests, compiles every hook and the MCP server, checks that `.mcp.json` parses,
runs the scaffolding end to end in a throwaway project, drives the MCP server over a real JSON-RPC
pipe, and drives `preflight.py` through each of its three install states. CI runs exactly this.
Please make it pass before opening a pull request.

## Layout

```
.claude-plugin/marketplace.json   the marketplace this repo publishes
plugins/teamme/
  .claude-plugin/plugin.json      the plugin manifest
  .mcp.json                       the MCP server this plugin ships
  commands/init-team.md           the command that installs the team
  commands/team-doctor.md         the command that diagnoses/repairs an existing install
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
  session. The `Stop` reminder stamps itself against the task's `updated` time for this reason.
- **Assume nothing is installed.** `python3` only — no `jq`, no third-party packages. Pipe-test a
  command with a synthesized payload before wiring it into settings.
- **Stay project-agnostic.** No project names, paths or stack assumptions in `templates/hooks/`.
  Anything project-specific belongs in the `{{PLACEHOLDER}}`s of `templates/intake.md`.

## Rules for the MCP server

`plugins/teamme/server/teamme_mcp.py` runs on the same bare-machine assumption as the hooks: stdlib
`python3` only, no third-party `mcp` package or other dependency. It must never crash on a malformed
JSON-RPC line or a client that closes the pipe. It gates `teamme_worklog` and `teamme_intake_phase`
on the scaffolding being installed by refusing — with an error naming `teamme_install` — rather than
by denying a tool call: this server has no hook registration, so it cannot block a write or a prompt
even if it wanted to. See `CLAUDE.md`'s "Decisions already made" for why prerequisite enforcement
lives here and in the commands' prompt text, and never in a hook.

## Changing the commands

`commands/init-team.md` and `commands/team-doctor.md` are prompts, not code. Keep `init-team.md`
explicit about what must be *copied* from `templates/` versus what must be *derived* from the
project being analyzed — re-authoring scaffolding from memory is how installs end up subtly broken.
