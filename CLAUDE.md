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
  commands/queue.md               parks a request in the work log; no grounding, no phase interaction
  server/teamme_mcp.py            stdio JSON-RPC MCP server: status/install/worklog/intake-phase/
                                   librarian tools
  server/librarian/*.py           librarian substrate: append-only JSONL + a disposable SQLite index,
                                   an incremental git indexer. NOT copied into a project, NOT in
                                   REQUIRED_HOOKS - invoked only by teamme_mcp.py
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
succeeds after — and drives `preflight.py` directly through all four states: `not-installed` with a
non-zero exit on an empty project, `installed-outdated` on a synthetic pre-upgrade install with a
`fix:` line that never once names `init-team` (watched failing first, by reverting `installed` to
the old hook-count definition and confirming the fixture flipped back to `not-installed`),
`installed-not-live`, and `live` on a scaffolded-and-heartbeated one — plus the `hooks`, `settings`
and `intake_dir` checks each failing independently with a `fix:` line, and the install-evidence
probe asserted to have no false positive against a stranger's repo carrying its own unrelated
`SessionStart` hook. `heartbeat` is proven silent and always exit-0, even with no `.claude/` and
stdin closed. The MCP repair path is covered too: `teamme_install` repairs an `installed-outdated`
fixture over the real JSON-RPC pipe and leaves its `intake.md` byte-identical.

Hook freshness — whether an installed hook script still matches the plugin's shipped copy, not just
whether it exists — has its own coverage, since `hook_freshness()` is the one shared comparison that
both `preflight.py`'s own check and `teamme_install` call: a present-but-modified hook drives
`installed-outdated` and is named in the `hooks` check's detail line, distinct from a missing one
(watched failing first, by forcing `hook_freshness` to always return `same`); with every resolution
path unset — no hint, no `$CLAUDE_PLUGIN_ROOT`, no install record — the check degrades to
existence-only and still PASSes, with a detail line saying freshness was not verified, proven never to
fail for that reason (the assertion isolates `HOME` and `CLAUDE_CONFIG_DIR` so it degrades on its own
fixture rather than on whatever the contributor's own machine happens to have installed); a synthetic
harness install record resolves the templates and catches a stale hook through it, with the detail
line naming the install record — not env, self or hint — as the source compared against; a missing
record degrades the same way, still naming `/teamme:team-doctor`; a corrupt (unparseable) record
degrades through a different code path than a missing one; and a record whose `installPath` fails the
template-marker check is rejected outright rather than trusted — the false-positive case, where a
confidently wrong verdict would be worse than an honest "not verified". Over the MCP pipe,
`teamme_install` leaves a hook that differs from the plugin's copy untouched without `force=true`, and
only `force=true` replaces it.

The worklog data model gets its own coverage: ten concurrent `note` calls on one task all survive
the lockfile; a stale (crashed-process) lockfile is broken rather than wedging a write and leaves no
lock/tmp debris behind; the nag lifecycle is checked in both directions — a note leaves the `Stop`
reminder armed, a real status transition re-arms it; a pre-migration ledger with no `status_changed`
field loads, lists and does not spuriously re-fire; and a `dispatched` task produces no `Stop`
output, shows `[@]` in `list`, counts as unfinished in `stats`, and gets its own `SessionStart`
wording distinct from `blocked`. If you change a hook or the MCP server, this is the proof — not
inspection.

The librarian substrate (`server/librarian/`) gets the same treatment, plus a fix to the compile step
itself: `plugins/*/server/*.py`, the old glob, only ever expanded to the top-level MCP server file and
silently never reached `server/librarian/*.py` — the same failure shape as a `REQUIRED_HOOKS` that
quietly covers less than its author assumed (see T23 below). Compilation now walks the whole `server/`
tree with `find -path` and fails loudly if the walk turns up nothing. Nine sections then drive the
tools against throwaway git fixtures, never this repo's own `.claude/librarians/`: `store.rebuild()`
is checked row-for-row against ground truth read straight from `git log`/`git rev-list`/`git
rev-parse` — independent of the librarian module, after a first draft compared `rebuild()` against the
module's own `index(full=True)` output and passed with a deliberately broken file-row insert loop
still in the code (watched failing first only once the comparison was made independent, by deleting
that loop); an unreachable last-indexed marker (a simulated rebase and force-push) falls back to a
full reindex and says so in the result, watched failing first by disabling the reachability branch,
which produced exactly the silent, confidently-wrong incremental refresh this test exists to catch; a
corrupt (non-SQLite) `index.db` is discarded and rebuilt rather than raised, through
`teamme_librarian_query` over the real MCP pipe, watched failing first by skipping the `unlink()` call
in `connect_or_reset`; an empty repository, a directory that is not a git repository, a missing
directory, and `git` itself missing from `PATH` are asserted as four distinct, non-overlapping error
payloads; ten concurrent `teamme_librarian_refresh` calls on one repository leave no lock or temp-file
debris and no duplicate rows; `commits_behind_head` reaches zero after a refresh driven over the pipe;
a newline in a path, a non-UTF-8 commit subject, and a merge commit (parent rows with zero file rows)
all parse without raising; and a combined pipe test drives all three librarian tools together — an
unknown query name, a bad commit hash, the row-limit clamp and truncation flag, and a malformed JSONL
line that is skipped rather than fatal — exiting 0 with empty stderr throughout.

## Design invariants

These are not style preferences. Breaking one ships a trap to someone else's machine.

1. **Hooks fail open.** Missing, malformed or stale state, an unparseable payload, a path outside
   the project — every one of these must ALLOW the write. A broken guard must never block work. The
   phase lock also expires on a timeout so a crashed session cannot leave a repo write-locked.
2. **Enforcement hooks cannot loop.** A `Stop` hook that re-fires on an unchanged condition traps
   the session. The reminder stamps itself against the task's `status_changed` time, never against
   `updated`: only a real status transition re-arms it, so recording a note — which moves `updated`
   but not `status_changed` — never triggers a second nag.
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
- **`installed` must never be derived from a hook list that grows, and that took a P0 to learn.**
  `preflight.py` originally scored "is teamme installed here" off `REQUIRED_HOOKS` — the tuple of
  hook scripts this release expects. `REQUIRED_HOOKS` grows every release that adds a hook, so a
  complete, working install from an *older* release started scoring less than 6/6 the moment a new
  hook shipped, and was reported `not-installed` and told to run `/teamme:init-team` — which
  re-runs the questionnaire and regenerates the roster over a team that already works. That would
  have hit every existing user on the release that added `preflight.py` itself to the list. The fix
  is structural: existence is now evidence-based — a `settings.json` hooks block naming one of
  teamme's own scripts, and/or a generated `.claude/commands/intake.md` — and neither probe changes
  when a release adds a hook script. A missing hook *script* became a **repair** condition
  (`installed-outdated`), never an existence condition. States are now four, not three:
  `not-installed` (no evidence teamme was ever set up here — the only state where the installer is
  right), `installed-outdated` (evidence of an install exists, but a hook script or the `hooks`
  block is missing, or a hook script is present but differs from the plugin's copy — repair with
  `teamme_install` / `/teamme:team-doctor`, never `/teamme:init-team`; a differing hook is left in
  place by a plain repair, since the difference could be a deliberate local edit rather than an old
  file, and is only replaced with `force=true`), `installed-not-live` (scaffolding complete, hooks
  registered, but no `SessionStart` has run them yet — needs `/hooks` or a restart), `live` (a
  `SessionStart` heartbeat proves hooks are firing). `installed-outdated` halts
  `templates/intake.md`: its exit code follows the `PASS`/`FAIL` items, not the `state:` line, so an
  outdated install halts `/intake` until repaired even though most of the team already works —
  documented in the README's install-and-verify section, since that is exactly what an upgrading
  user hits first.
- **`/teamme:queue` exists because `/intake`'s own "queue" outcome still cost a full response.**
  The triage table always had a queue disposition, but reaching it still meant grounding,
  classifying and briefing a request the user only wanted parked. `/queue` is that same outcome
  taken directly, with a hard one-line output contract, so parking a request costs the user
  nothing. `/intake` itself gained step 0a for the same reason: it tests for deferral wording
  ("later", "once you finish X", "queue this") *before* it checks the phase, so a parked request
  arriving through `/intake` gets the cheap path too, instead of being fully grounded because the
  phase happened to be idle.
- **Hook freshness has exactly one implementation, shared by `preflight.py` and `teamme_install`.**
  `hook_freshness()` — a byte-exact comparison — lives once, in `preflight.py`; `teamme_mcp.py`
  loads and delegates to it rather than re-implementing the comparison. Two implementations is
  precisely the failure mode this closes: the installer skipping a file it calls "differs" while the
  health check calls the same install fresh. Vocabulary follows the same reasoning: a hook that does
  not match the plugin's copy is reported as "differs from the plugin's copy", never "outdated" or
  "wrong" — it may be a deliberate local edit, and a plain repair leaves it alone on purpose. Only
  `force=true` (or `/teamme:team-doctor`, which can pass it) replaces it.
- **Locating the plugin's own templates to compare against has one more rung: the harness's install
  record.** `template_hooks_source()` tries, in order: an explicit hint (the MCP server, which knows
  its own path), `$CLAUDE_PLUGIN_ROOT` (set only for hooks the plugin registers itself), this script's
  own directory (the case that matters for the MCP server, since the file *is* the template there),
  and last, `$CLAUDE_CONFIG_DIR/plugins/installed_plugins.json` (falling back to `~/.claude/...`) — the
  harness's own record of where each installed plugin lives now. That last rung is what lets an
  in-project `/intake` preflight — `.claude/hooks/preflight.py check`, with no `CLAUDE_PLUGIN_ROOT` in
  its environment — find the plugin's templates and catch a hook that differs, not just one that is
  missing. It is read live, on every check, never captured at install time: the plugin cache is
  version-pinned and old versions persist on disk, so a path recorded once would keep pointing at the
  version installed *from*, and after an upgrade would compare stale hooks against equally stale
  templates and call them all fresh — a confidently wrong verdict, which is worse than an honest "not
  verified". The record is rewritten by every upgrade, so reading it live is self-correcting. This is
  teamme's one assumption about Claude Code's own on-disk layout, and it is deliberately contained:
  every function that touches it sits between one banner comment and the next in `preflight.py`, so it
  is obvious where to fix it if the harness's layout changes. It degrades to existence-only, never to a
  wrong answer, on every failure — the record file missing, unparseable, an unexpected schema, or an
  `installPath` that fails the same `settings.hooks.json` marker check that already guards the other
  resolution paths, so a record pointing at the wrong directory is rejected rather than trusted.
- **A `dispatched` work-log status, distinct from `blocked`.** Async dispatch to another agent is
  this team's normal mode, and it had no representation: `active` nagged every turn even though
  nothing that session could do would advance it, and `blocked` told the user they owed an input
  they did not owe. `dispatched` counts as unfinished, is skipped by `next`, is never nagged about
  by the `Stop` hook, and is reported separately at `SessionStart` from tasks blocked on the user's
  own input.
- **The librarian indexer lives in the plugin, not in `templates/hooks/`.** Nothing under
  `server/librarian/` is copied into a target project, so nothing joins `REQUIRED_HOOKS`. This avoids
  T23's trap *by construction* rather than by remembering — a copied indexer would make every future
  librarian grow that list again, the exact mistake `installed` derivation paid a P0 to fix.
- **The `.db` is derived and disposable; the `.jsonl` is the record.** SQLite is binary and
  unmergeable, so two people indexing different commits would produce files that conflict
  irreconcilably. `.claude/librarians/index.db` is therefore always gitignored, and
  `store.rebuild()` reconstructs it from `.claude/librarians/history/commits.jsonl` alone, with no
  git access at all — verified with `PATH` stripped, so git was genuinely unavailable. This is what
  makes the user's storage fork real: commit the `.jsonl` or gitignore it, per project; the `.db` is
  never committed either way.
- **Queries are a bounded named set, not arbitrary SQL.** Arbitrary SQL from a model is an injection
  surface, and at 9-11ms for `commits_touching` over 53,000 file rows there is no performance
  argument buying it either. A later dependency-tree query (a recursive CTE) should be added as
  another named entry in `QUERY_NAMES`, not as a door into raw SQL.

## Known gaps

- No eval suite yet (`claude plugin eval`). The prompts (`init-team.md`, `team-doctor.md`,
  `intake.md`, `queue.md`) are checked only for parsable frontmatter; nothing tests what they
  instruct. The command-level preflight refusal, `/teamme:queue`'s one-line output contract, and
  `/intake`'s step 0a deferral short-circuit are all prompt text, not enforced behaviour.
- `templates/intake.md` has been exercised on one real project (a Minecraft Fabric mod). The
  `{{PLACEHOLDER}}` set may not fit stacks with very different doc conventions.
- The plugin ships no `agents/` of its own by design — teams are generated per project — so nothing
  validates the *generated* agent files beyond frontmatter parsing.
- `route-to-intake.py`, `reground`, out-of-project paths and stale-state expiry remain unexercised
  by the smoke test.
- `route-to-intake.py`'s new `/queue` passthrough and its blank-prompt return, and `preflight.py`'s
  heartbeat under a real pty, have been checked by hand but are not yet asserted in `validate.sh`
  (tracked as T20).
- `teamme_intake_phase` is not separately gate-tested; only `teamme_worklog` exercises the shared
  gate-on-install path against the MCP server.
- Version bumps are manual: `plugin.json` `version` plus a `CHANGELOG.md` entry.
- This repo's own dogfood install is `installed-outdated` by the definition above: `.claude/commands/
  intake.md` predates the preflight block entirely (no Preflight section, no `state:` handling), so
  `/intake` in this repo does not halt on an incomplete install the way a freshly-generated one would.
  Tracked as T24.
- **The install record's per-project and multi-entry logic is not directly asserted.**
  `preflight.py` now also resolves the plugin's templates from the harness's own install record
  (`$CLAUDE_CONFIG_DIR/plugins/installed_plugins.json`, falling back to `~/.claude/...`), so an
  in-project `/intake` check can catch a hook that differs from the plugin's copy, not just one that
  is missing — provided the record resolves, which depends on install scope (see the README's Install
  section). `_install_record_template_dirs()` ignores a record entry whose `projectPath` names a
  *different* project, and among entries with no `projectPath` (user/global scope) prefers the newest
  by `lastUpdated`. `validate.sh` exercises single-entry records — a resolvable one, a missing one, a
  corrupt one, a bad `installPath` — but not a record carrying more than one `teamme` entry, so the
  "belongs to a different project, ignore it" branch and the `lastUpdated` ordering among competing
  global entries are unverified.
- **Phase 1 of the librarian substrate ships no agents.** `teamme_librarian_status`,
  `teamme_librarian_refresh` and `teamme_librarian_query` exist and are proven by `validate.sh`; the
  tool descriptions say a librarian agent is the *intended* caller of the query tools, but that is
  documented intent, not enforcement — phase 1 has no librarian agents, no gate, and nothing stops
  any other caller with MCP access from calling `teamme_librarian_query` directly today.
- The `commits_touching`, `commits_between` and `search_subjects` query variants are exercised while
  building fixtures and ground truth for other assertions, but none has a `validate.sh` section
  asserting its own result directly — only `recent`, `files_in_commit` and the unknown-query/bad-hash
  error paths are.
- Shallow-clone behaviour is untested. `history.probe()` reports `shallow: true` and `index()` notes
  it in the result, but no `validate.sh` fixture is an actual shallow clone.
- Merge commits get `commit_parents` rows but no `files_changed` rows (`git log --numstat` reports no
  diff for a merge without `-m`/`-c`). Whether a merge needs file rows is a phase-2 decision, if the
  reasoning layer ends up needing merge diffs.
- `rewrite_records()`'s preservation of non-commit records (`kind != "commit"`, meant for phase 2's
  reasoned entries) through a full reindex has no test coverage, since no phase-2 code writes such a
  record yet.

## Conventions

Commits: one imperative, sentence-case line; no scope prefix. Keep `CHANGELOG.md` current for
anything user-visible.
