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
from the plugin's copy alone unless called with `force=true`. It also drives nine sections against the
librarian tools over throwaway git fixtures — rebuild-from-text against ground truth read straight
from `git`, the unreachable-marker fallback, a corrupt `index.db` being discarded rather than raised,
concurrent refreshes, and hostile commit content — never against this repo's own
`.claude/librarians/`. It drives a further six sections against the session librarian, using synthetic
transcripts under a fixture's own fake `$CLAUDE_CONFIG_DIR/projects/<slug>/` — never this repo's own
conversations — including a real bug an earlier version of this coverage found: a record longer than
`MAX_LINE_BYTES` came back from `readline()` without a trailing newline and was mistaken for a live
tail, permanently parking the byte offset in front of it. It also drives `librarian-gate.py` — the
`PreToolUse` hook that asks (never denies) on a `git push` behind a stale history index — through a
refresh-clears-the-gate proof, an ask/never-denies contract checked structurally against every
`permissionDecision` literal the source can emit, a pinned push/non-push classification boundary, and
seven fail-open branches, plus a `preflight.py` section proving a missing `librarian-gate.py` drives
`installed-outdated` rather than a false "not-installed". CI runs exactly this. Please make it pass
before opening a pull request.

`server/` is compiled by walking `find -path '*/server/*' -name '*.py'`, not a shallow
`plugins/*/server/*.py` glob: the glob only ever expanded to the top-level `teamme_mcp.py` and would
silently never have reached `server/librarian/*.py`, or any later subdirectory under `server/` — the
same failure shape as a `REQUIRED_HOOKS` list that quietly covers less than its author assumed (see
`CLAUDE.md`'s T23 entry). A new subdirectory under `server/` is picked up automatically; the check
fails loudly if the walk ever turns up nothing.

## Layout

```
.claude-plugin/marketplace.json   the marketplace this repo publishes
plugins/teamme/
  .claude-plugin/plugin.json      the plugin manifest
  .mcp.json                       the MCP server this plugin ships
  agents/history-librarian.md     the plugin's own shipped agent - see Rules for plugin-shipped
                                   agents below
  commands/init-team.md           the command that installs the team
  commands/team-doctor.md         the command that diagnoses/repairs an existing install
  commands/queue.md               parks a request in the work log; no grounding, no phase interaction
  server/teamme_mcp.py            stdio JSON-RPC MCP server
  server/librarian/*.py           librarian substrate: append-only JSONL + a disposable SQLite index,
                                   an incremental git indexer - not copied into a project
  server/librarian/transcripts.py the session librarian's harness-layout assumption (where Claude Code
                                   writes session transcripts, and what a line looks like) - see
                                   Rules for the hook scripts below, which this module follows too
  server/librarian/sessions.py    the session librarian: lazy, incremental-by-byte-offset index over
                                   those transcripts - no hook, no JSONL record, not copied into a project
  server/librarian/config.py      per-project librarian settings (enable/disable, commit_record),
                                   enacted into .gitignore on every index open (store.connect_file),
                                   not only when configure is called - owned by the MCP server, never
                                   hand-edited
  server/librarian/cross.py       the cross-index join (around_path/around_commit/around_task/
                                   timeline): reads the history index, the session index and the work
                                   log (read live, never indexed) together - served through the
                                   existing teamme_librarian_query tool, not a new one
  templates/                      scaffolding copied into a target project
    hooks/*.py                    project-agnostic; do not hard-code a project name
    hooks/librarian-gate.py       PreToolUse on `git push` - asks, never denies, when the history
                                   index is behind HEAD; see Rules for the hook scripts below
    intake.md                     skeleton with {{PLACEHOLDER}}s the command fills in
    settings.hooks.json           the hooks block merged into the project's settings.json
```

## Rules for the hook scripts

These run on other people's machines, inside their editing loop. They must:

- **Fail open.** Missing, malformed or stale state, an unparseable payload, a path outside the
  project — every one of these allows the write. A broken guard must never block someone's work.
  `ask` is a narrower second verb, not a loophole in this: a hook may prompt instead of silently
  allowing, but only on a well-formed state it is genuinely confident about, and only `ask`, never
  `deny`. See `CLAUDE.md`'s invariant #1 for the exact carve-out and `librarian-gate.py` for the one
  hook that uses it.
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
- **Keep every harness-layout assumption in its own fenced place.** teamme makes exactly two
  assumptions about Claude Code's own on-disk layout, and both are contained the same way. `preflight.py`
  locates the plugin's own templates via `$CLAUDE_CONFIG_DIR/plugins/installed_plugins.json` as a last
  resort; `server/librarian/transcripts.py` locates and parses session transcripts under
  `$CLAUDE_CONFIG_DIR/projects/<slug>/`. Each sits between one banner comment and the next in its own
  file, so it is obvious where to fix it if the harness changes. Read it live on every check, never
  capture it at install time (see `CLAUDE.md`'s "Decisions already made" for why) — and if a third one
  turns out to be needed, give it the same fenced, single-file treatment rather than letting it spread.
- **A watch-fail against code outside the lane's own file must name the mechanism.** "Break it, watch
  the assertion fail, revert it" only proves anything if the break happens somewhere safe to mutate. When
  the code under test lives in a file the lane does not own, or that another lane may be editing
  concurrently, breaking it in place is not safe — say instead which of a scratch copy, a monkeypatched
  import path, or an injected fixture the break must happen against. Do not rely on remembering to say
  this per task; it is a standing rule because a brief that says "watch it fail" and "only touch
  `scripts/validate.sh`" in the same breath is self-contradicting whenever the assertion targets another
  lane's file.

## Rules for the MCP server

`plugins/teamme/server/teamme_mcp.py` runs on the same bare-machine assumption as the hooks: stdlib
`python3` only, no third-party `mcp` package or other dependency. It must never crash on a malformed
JSON-RPC line or a client that closes the pipe. It gates `teamme_worklog` and `teamme_intake_phase`
on the scaffolding being installed by refusing — with an error naming `teamme_install` — rather than
by denying a tool call: this server has no hook registration, so it cannot block a write or a prompt
even if it wanted to. See `CLAUDE.md`'s "Decisions already made" for why prerequisite enforcement
lives here and in the commands' prompt text, and never in a hook.

Any new librarian index must be opened through `server/librarian/store.connect_file()` (or a helper
that calls it) rather than opening SQLite directly — that one funnel is what puts teamme's
`.gitignore` protection in place before the first byte of an index exists, and a call site added
anywhere else is exactly the shape of the bug 0.7.0 fixed (see `CLAUDE.md`'s "Decisions already
made"). The same module's `ignore_guard()` derives the owning project from the path being opened,
never from `CLAUDE_PROJECT_DIR` or the working directory, so a misrouted call can never protect the
wrong project's index.

## Rules for plugin-shipped agents

`plugins/teamme/agents/` is a new shipped surface — `history-librarian.md` is the first file in it,
and the librarian tier is meant to grow more. It is not the same thing as the agents a project's own
`/teamme:init-team` generates into `.claude/agents/`: those are per-project and derived from that
project's layout; these ship with the plugin itself and are present, unchanged, in every project the
plugin is installed in. See `CLAUDE.md`'s "Decisions already made" for why the two are separate tiers
rather than one mechanism.

A plugin-shipped agent's frontmatter must never set `permissionMode`, `hooks` or `mcpServers`. The
CLI drops all three for a plugin agent — `.claude/agents/` is the level that gets that control, not
the plugin — and while it does print a runtime warning naming the ignored key, nothing here catches
that at review time, and a `validate.sh` run does not surface it either (see `CLAUDE.md`'s Known gaps
for what is and is not checked about this file today). Setting one of these keys is a bug that ships
invisibly unless someone happens to see that warning live.

## Changing the commands

`commands/init-team.md`, `commands/team-doctor.md` and `commands/queue.md` are prompts, not code.
Keep `init-team.md` explicit about what must be *copied* from `templates/` versus what must be
*derived* from the project being analyzed — re-authoring scaffolding from memory is how installs end
up subtly broken. Keep `queue.md`'s one-line output contract intact when editing it: it exists so
parking a request costs the user nothing (see `CLAUDE.md`'s "Decisions already made"), and anything
that adds a second line of preamble, analysis or a follow-up question defeats that.
