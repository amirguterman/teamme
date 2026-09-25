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
| `not-installed` | No evidence teamme was ever set up here: no registered teamme hooks and no generated `/intake` command. This project has never been scaffolded. | Run `/teamme:init-team` in this project. It analyzes the project and installs the team, the hooks and `/intake`. Do not hand-assemble a partial install instead. This is the **only** state where the installer is the right advice. |
| `installed-outdated` | teamme **is** installed here — the check names the evidence — but part of the scaffolding is missing or stale: a hook script this release expects, or the `hooks` block in `.claude/settings.json`. Usually an install from an earlier release that predates a file the current version ships. | **Repair only.** Offer the repair below — the `teamme_install` MCP tool, or copying the named missing files yourself — then re-run the check. **Do not run `/teamme:init-team`.** The installer re-runs the questionnaire and regenerates the roster over a team that already works; missing scaffolding is a repair, not a reinstall. Say that in one line if the user asks for the installer here. |
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

## "My push started asking me something"

That is `.claude/hooks/librarian-gate.py`, the one hook teamme installs that has anything to say
about a `git push`. On a push it compares this project's history index against `HEAD` and, when the
index is behind, **asks** for confirmation, naming how many commits are unindexed. It never
denies. Approving
proceeds with the push exactly as if the hook were not there — a hotfix is never held up by a stale
index.

It is already silent everywhere it cannot be confident: no index in this project, the `history`
librarian switched off, the marker missing or unreadable or unreachable from `HEAD` (a rebase, a
force-push, a shallow clone), a command it cannot confidently classify as a push, or git missing or
slow. So if it spoke, there is an index here and it is genuinely behind.

| What the user wants | What to do | What it costs |
|---|---|---|
| The reminder was right — clear it | `teamme_librarian_refresh` with `{"librarian": "history"}`, then push again | Nothing; the refresh is incremental, and it is the whole reason the hook spoke. |
| Not to be asked again in this project | `teamme_librarian_configure` with `{"librarian": "history", "enabled": false}` | The whole `history` librarian, not just the reminder: `teamme_librarian_refresh` and `teamme_librarian_query` then refuse for it, so `history-librarian` can no longer answer anything about this project's commits. `teamme_librarian_status` keeps working and keeps reporting the setting. |

**There is no switch for the reminder on its own.** Say that plainly rather than letting the second
row read as a narrow opt-out. Re-enable with `{"librarian": "history", "enabled": true}`; the
setting lives in `.claude/librarians/config.json`, per project. Deleting the hook script or its
`settings.json` entry is not a third option — the next preflight reports the install
`installed-outdated`, and a repair puts it back.

If the count itself looks wrong, run `teamme_librarian_status`. It reports the marker the hook reads,
`.claude/librarians/history/indexed_head`, in one of three ways: **published and agreeing with the
index**; **not published**, in which case the reminder stays silent until the next refresh writes it;
or **`MARKER DIVERGED`**, marker and index naming different commits, which means the count is being
taken from the wrong commit. A refresh rewrites both together and is the fix for the last two.

If this hook is *missing* rather than noisy — an install from a release before it shipped — the check
reports `installed-outdated`, which is the repair row in the table above: `teamme_install` or this
command. Never `/teamme:init-team`.

## Be honest about what this is

This is a diagnosis you ran and a repair you offered — not proof that the harness is enforcing
anything. A green check means the scripts are present and the session has loaded them; it does not
prove a future turn will honour them. Report exactly what the check observed, and nothing more.
