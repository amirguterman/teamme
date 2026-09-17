# Contributing

## Run the checks

```bash
./scripts/validate.sh
```

It validates the manifests, compiles every hook, and runs the scaffolding end to end in a throwaway
project. CI runs exactly this. Please make it pass before opening a pull request.

## Layout

```
.claude-plugin/marketplace.json   the marketplace this repo publishes
plugins/teamme/
  .claude-plugin/plugin.json      the plugin manifest
  commands/build-agent-team.md    the command users invoke
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

## Changing the command

`commands/build-agent-team.md` is a prompt, not code. Keep it explicit about what must be *copied*
from `templates/` versus what must be *derived* from the project being analyzed — re-authoring
scaffolding from memory is how installs end up subtly broken.
