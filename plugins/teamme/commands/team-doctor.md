---
description: Diagnose this project's teamme install - whether the hooks are present, live, and runnable - and offer to repair whatever is broken.
---

Run teamme's preflight check on demand and report the full diagnosis. This is the same check the
other teamme commands run at their first step; here it is the whole job. Do not re-implement it, and
do not judge the install by reading files — run the check.

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/templates/hooks/preflight.py" check
```

If the MCP tools are available, `teamme_status` reports the same thing and works even when nothing
is installed. Either source is fine; say which one you used.

Print every line the check emitted, passing items included — the point of this command is the full
picture, not just the failures. It reports one `PASS`/`FAIL` line per item (`python3`, `hook
scripts`, `intake command`, `hook registration`, `intake state dir`, `hooks firing`), a `fix:` line
under each failure, and a final `state:`. Then map that state onto the table:

| State | What it means | The fix |
|---|---|---|
| the command itself fails — `python3: command not found` | The check could not run. Every hook, the work log and the phase lock are `python3` scripts. | Nothing teamme ships can run. Install python3 (the system package manager, or python.org), then re-run this command. No repair is possible from here. |
| `not-installed` | The hook scripts, or the `hooks` block in `.claude/settings.json`, are missing — this project has never been scaffolded. | Run `/teamme:init-team` in this project. It analyzes the project and installs the team, the hooks and `/intake`. Do not hand-assemble a partial install instead. |
| `installed-not-live` | Everything is registered but no `SessionStart` has fired here, usually because `.claude/` held no settings file when the session started. Nothing is being enforced right now: `/intake`'s grounding phase would not actually deny a write. | Run `/hooks` to approve them, or restart Claude Code. The next session start writes the heartbeat. Re-run this command to confirm. |
| `live`, individual items still `FAIL`ing | An install was interrupted, or files were deleted, gitignored away, or hand-edited. | Offer to repair (below). An unparseable `settings.json` is the user's to fix — report the syntax error, never rewrite the file around it. |

## Offer the repair

For anything repairable — missing hook scripts, a missing or partial `hooks` block in
`.claude/settings.json`, a missing `.claude/intake/` directory — say exactly which files you would
write, then **ask before writing anything**. Use `teamme_install` if it is available; otherwise copy
the missing scripts verbatim from `${CLAUDE_PLUGIN_ROOT}/templates/hooks/` and merge the `hooks`
block from `${CLAUDE_PLUGIN_ROOT}/templates/settings.hooks.json`, preserving everything already in
the settings file. Never re-author a hook from memory, and never edit a copied hook.

After repairing, re-run the check and report the new state. If it still fails, say so plainly rather
than declaring it fixed.

## Be honest about what this is

This is a diagnosis you ran and a repair you offered — not proof that the harness is enforcing
anything. A green check means the scripts are present and the session has loaded them; it does not
prove a future turn will honour them. Report exactly what the check observed, and nothing more.
