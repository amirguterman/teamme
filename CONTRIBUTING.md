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
`installed-outdated` rather than a false "not-installed". It also drives `preflight.py roster` — a
third `preflight.py` subcommand, a *separate* verdict from `check` that answers whether the team
roster still agrees with itself across the agent files, the roster README's table, the generated
`intake.md`'s lane mentions and the work log's `lane` fields, marking each of its three checks `PASS`,
`FAIL`, or `SKIP` (could not be read, so unproven) — through eight sections: a clean fixture, both
check-1 failure directions on one line, check-2's narrow whole-word matching at its own edge, the
closed-task exemption (with a watch-fail), the closed-status list read by differential from
`worklog.py`'s own source, all seven missing/unreadable/corrupt inputs degrading to `SKIP` and never a
false `PASS`, frontmatter-name identity (`<name>.md.disabled` genuinely out of the roster), and the
central watch-failed property, in both directions, that a drifted roster never moves `check`'s exit
code, `state:` line or check set. CI runs exactly this. Please make it pass before opening a pull
request.

If a section fails unexpectedly, `validate.sh` traps its own `ERR` and names the failing line and the
command that ran, using `${BASH_LINENO[0]}` rather than `$LINENO` — a plain `$LINENO` read inside the
trap reports the trap's own line, not the command that failed. Read that line before re-running the
whole script.

`server/` is compiled by walking `find -path '*/server/*' -name '*.py'`, not a shallow
`plugins/*/server/*.py` glob: the glob only ever expanded to the top-level `teamme_mcp.py` and would
silently never have reached `server/librarian/*.py`, or any later subdirectory under `server/` — the
same failure shape as a `REQUIRED_HOOKS` list that quietly covers less than its author assumed (see
`CLAUDE.md`'s T23 entry). A new subdirectory under `server/` is picked up automatically; the check
fails loudly if the walk ever turns up nothing.

The manifests check also flags a command or agent frontmatter scalar that starts with an unquoted `[`
or `{`: this repo's own frontmatter reader treats it as plain text, but a real YAML parser reads it as
a flow sequence or mapping, so a value shaped that way would pass here and parse differently
elsewhere — found once by inspection, in a draft `argument-hint`, before the check existed. And a
lock-in asserts `plugins/teamme/commands/*.md` is exactly four files, named by file — a guard on the
check's own file-discovery glob, since a new command file added without updating this count would
otherwise be a silent miss the same shape as the two globs above.

Which `*.md` files the manifests check knows how to validate is decided by content, not by directory,
for the same reason the two globs above walk the whole tree instead of a shallow pattern:
`is_skeleton()` tests whether a file actually contains a `{{PLACEHOLDER}}`-shaped marker before
exempting it, rather than exempting everything under `templates/` by path — a directory-wide exemption
was exactly correct only while `templates/` held nothing but `intake.md`, and stopped being correct,
silently, the moment `templates/agents/devils-advocate.md` shipped a real prompt into that same
directory (see `CLAUDE.md`'s T49 entry in Verify). Every `*.md` found by the walk and not a skeleton
must be accounted for by `commands/`, `agents/` or `templates/agents/` — an `.md` sitting anywhere else
fails the check by name, with one deliberate exception: any file literally named `README.md` is
excluded tree-wide, by name rather than content, a narrower assumption than the one this fix just
replaced (see `CLAUDE.md`'s Known gaps for why that is recorded rather than closed).

## Keep the docs checked against the code

Two more `validate.sh` sections compare `CHANGELOG.md`, `README.md` and `plugins/teamme/README.md`
against the code directly, not just against each other:

- **identifiers-exist** resolves every backticked lowercase identifier in those three files against the
  live `TOOLS` registry, the three `QUERY_NAMES` tuples, every tool schema's parameter and enum values,
  and a vocabulary of real-but-non-callable names derived mechanically (dict-key literals, `CREATE
  TABLE` column names) — never a hand-typed allow-list, since that would be the next copied fact this
  check exists to stop making. A narrow suffix rule accepts genuine shorthand (`configure` for
  `teamme_librarian_configure`, matched only as an exact trailing word after an underscore) — a strict
  full-names-only policy was tried first and rejected, since it flagged a real, already-released
  shorthand sitting in the 0.7.0 `CHANGELOG.md` entry.
- **rendered-labels-exist** checks every documented `` `key: value` `` output claim against
  `teamme_mcp.py`'s own renderer source, catching a claim like 0.6.0's `` `has_data: false` `` — a real
  internal dict key that the renderer never actually prints by that name (it prints `data:
  yes`/`data: no`).

**Neither check catches a bare value claim.** 0.6.0 also shipped "defaults to disabled" against
`DEFAULT_ENABLED = True`; no identifier extraction sees a value written in prose. Until something
checks that mechanically, the discipline is manual, and it is a rule, not a suggestion: **when a doc
states a default, a cap or a count, name the constant that backs it** (`max_files` "defaults to 25
(`DEFAULT_MAX_COMMIT_FILES`)", not just "defaults to 25"), so a reader can grep the real value instead
of trusting the prose. Apply it to any new claim you add; you do not need to retrofit every existing
one, but if you are already touching a paragraph that states a value, name the constant while you are
there.

## Layout

```
.claude-plugin/marketplace.json   the marketplace this repo publishes
plugins/teamme/
  .claude-plugin/plugin.json      the plugin manifest
  .mcp.json                       the MCP server this plugin ships
  agents/history-librarian.md     the plugin's own shipped agent - always present, never
                                   roster-selectable - see Rules for plugin-shipped agents below
  commands/init-team.md           the command that installs the team
  commands/team-doctor.md         the command that diagnoses/repairs an existing install; also runs
                                   and reports `preflight.py roster`, a separate verdict from `check`
  commands/modify-team.md         changes an EXISTING team (add/drop/retool/rename a lane) without
                                   re-running init-team - see Changing the commands below and
                                   `CLAUDE.md`'s "A roster is four copies" entry
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
    agents/devils-advocate.md     opt-in, roster-selectable - copied verbatim into a project's
                                   .claude/agents/ only if chosen in init-team.md's Phase 3; see
                                   Rules for plugin-shipped agents below
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

There are now three agent tiers, and confusing one for another is the mistake this section exists to
prevent. `plugins/teamme/agents/` — `history-librarian.md` is the only file in it today — ships with
the plugin and is present, unchanged, in every project the plugin is installed in, whether or not that
project has run `/teamme:init-team`; it is never offered in the Phase 3 roster questionnaire and never
roster-selectable. `plugins/teamme/templates/agents/` — `devils-advocate.md` is the first and only file
in it today — also ships project-agnostic with the plugin, but is **opt-in**: it is offered in Phase 3
like any derived lane, and copied verbatim into a project's `.claude/agents/` only if selected, exactly
the way `templates/hooks/*.py` are copied rather than derived. Generated `.claude/agents/*.md` are the
third tier: per-project, derived from that project's own layout, never copied from anywhere. See
`CLAUDE.md`'s "Decisions already made" — the "two tiers" entry and the "third agent tier" entry that
follows it — for why shipped-and-fixed, shipped-but-optional and generated-per-project are three
different answers to three different constraints, not one mechanism with variations.

A plugin-shipped agent's frontmatter must never set `permissionMode`, `hooks` or `mcpServers` —
whether it lives in `agents/` or `templates/agents/`. The CLI drops all three for a plugin agent —
`.claude/agents/` is the level that gets that control, not the plugin — and while it does print a
runtime warning naming the ignored key, nothing here catches that at review time. `validate.sh`'s
manifests check does surface it for both tiers: `check_agent_file()` is the one implementation both
`agents/*.md` and `templates/agents/*.md` are validated through, so a forbidden key set in either
location fails the same way, at parse time, in CI — not only via a runtime warning someone has to be
watching for live. What it does not do, for either tier, is test what the prompt *instructs* — see
`CLAUDE.md`'s Known gaps.

## Changing the commands

`commands/init-team.md`, `commands/team-doctor.md`, `commands/modify-team.md` and `commands/queue.md`
are prompts, not code. Keep `init-team.md` explicit about what must be *copied* from `templates/`
versus what must be *derived* from the project being analyzed — re-authoring scaffolding from memory
is how installs end up subtly broken. Keep `queue.md`'s one-line output contract intact when editing
it: it exists so parking a request costs the user nothing (see `CLAUDE.md`'s "Decisions already
made"), and anything that adds a second line of preamble, analysis or a follow-up question defeats
that.

`modify-team.md` changes an *existing* team — it is not a second installer, and it must keep saying so
in its own text (`init-team.md` and `team-doctor.md` both point users away from re-running the
installer over a working roster; `modify-team.md` is the alternative those two refusals name). Its
phase order — preflight, read the inherited roster state, report all four copies, ask what changes,
migrate the affected tasks' lanes, confirm and execute, re-run `preflight.py roster` as proof, hand
over — exists so nothing is written before the user approves it and nothing is claimed fixed without
being checked afterwards; do not collapse phases to save turns. Never let it re-derive
`preflight.py roster`'s output shape from memory: the `PASS`/`FAIL`/`SKIP` marks and the `not
verified: <reason>` wording are owned by `preflight.py`, and a prompt that drifts from them is exactly
the copied-fact bug this check exists to catch elsewhere.
