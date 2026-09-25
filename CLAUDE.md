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
  agents/history-librarian.md     the plugin's own shipped agent - query-only, present in every
                                   project the plugin is installed in, not offered in
                                   /teamme:init-team's roster and not roster-selectable (see
                                   Decisions already made)
  commands/init-team.md           installs the team; a prompt, not code
  commands/team-doctor.md         diagnoses/repairs an existing install on demand; a prompt, not code
  commands/queue.md               parks a request in the work log; no grounding, no phase interaction
  server/teamme_mcp.py            stdio JSON-RPC MCP server: status/install/worklog/intake-phase/
                                   librarian tools
  server/librarian/*.py           librarian substrate: append-only JSONL + a disposable SQLite index,
                                   an incremental git indexer, and a co-change query layer
                                   (changes_with/coupling_between/hotspots) built on the same
                                   files_changed rows - no new table, no new index pass. NOT copied
                                   into a project, NOT in REQUIRED_HOOKS - invoked only by teamme_mcp.py
  server/librarian/transcripts.py the second harness-layout assumption teamme makes (after the install
                                   record in preflight.py) - where Claude Code writes session
                                   transcripts and what a line in one looks like, fenced between one
                                   banner comment and the next, read live rather than cached
  server/librarian/sessions.py    the session librarian: a lazy, incremental-by-byte-offset index over
                                   those transcripts. No hook, nothing copied into a project, and no
                                   JSONL record - the transcript itself is the record
  server/librarian/config.py      per-project librarian settings (.claude/librarians/config.json):
                                   which librarians are enabled, and whether the append-only record
                                   is committed - owned by the MCP server, enacted into .gitignore on
                                   every index open (store.connect_file, not only when configure is
                                   called - see Decisions already made), never hand-edited
  server/librarian/cross.py       the cross-index join: around_path/around_commit/around_task/
                                   timeline, reading the history index, the session index and the
                                   work log (read live, never indexed) together - served through the
                                   existing teamme_librarian_query tool, not a new one
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

Three co-change queries — `changes_with`, `coupling_between`, `hotspots` — read edges out of the same
`files_changed` rows rather than parsing source, and four more sections cover them: `changes_with` is
checked row-for-row against ground truth read straight from `git log --name-only`, with the librarian
module out of the loop; the 25-file damping cap is proven in *both* directions on a fixture with one
30-file sweep commit — the default cap excludes it entirely, leaving the real partner (`a_test.py`)
and no junk file in the ranking, and `max_files=40` (past the sweep's size) brings the junk files back
and drops the skipped count to zero — so the cap is shown to be doing the work rather than the fixture
being thin (watched failing first four ways: an off-by-one `shared_commits`, the cap silently ignored
by `_breadth` even after being raised, `too_broad` forced `False`, and the damped-away empty message
collapsed into the unknown-path one — each revert confirmed to hold afterward by grepping the restored
file for the broken text); `coupling_between` is checked to
mark a too-broad shared commit rather than hide it, since that one listing is deliberately undamped;
and `changes_with`'s four empty/annotated payloads — an unknown path, a path whose every commit was
damped away, a path that genuinely changed alone, and too little history to rank from — are asserted
distinguishable from one another rather than collapsing into one generic "no results". `hotspots` has
no section of its own yet, and the renderer a librarian agent actually reads (`_render_cochange` in
`teamme_mcp.py`) is exercised only by hand, not by `validate.sh` — both tracked as T35.

The session librarian (`server/librarian/transcripts.py` and `sessions.py`) has six sections of its
own, run against synthetic transcripts under a fixture project's own fake
`$CLAUDE_CONFIG_DIR/projects/<slug>/` — never this repo's real conversations. Three are watch-fails: an
**oversized-record wedge**, a real bug found while building this coverage — a record longer than
`MAX_LINE_BYTES` came back from `readline()` with no trailing newline and was mistaken for a live,
still-being-written tail, which permanently parked the byte offset in front of it and reported "nothing
new to index" on every later refresh, forever (watched failing first by forcing the size-cap branch to
always read as a partial tail); a **partial no-newline tail**, proving the opposite direction — a
genuinely incomplete final line is left unconsumed rather than parsed, and picked up whole on the next
refresh (watched failing by disabling that branch, which consumed the incomplete line early); and a
**same-size content replacement**, proving a transcript replaced with different content at an identical
byte length is caught by its first record's `uuid` changing, not by size, and forces a full reindex
instead of silently trusting the old offset (watched failing by disabling the fingerprint-mismatch
check, which left the old content searchable and the new content never indexed). Three more are not
watch-fails: privacy is checked with `git check-ignore` as ground truth, not by reading `.gitignore`
text, confirming `.claude/librarians/sessions/` stays ignored under `commit_record` both `true` and
`false`; the `window` query's retrieval bound is proven by asking for a huge `before`/`after` span and
confirming it still stops at `MAX_WINDOW`/`MAX_WINDOW_TOTAL_CHARS` with `truncated: true`, never
dumping the session; and degradation is checked with no `$CLAUDE_CONFIG_DIR` and no `~/.claude` at all,
returning a named error payload rather than raising.

The librarian privacy guarantee — every index gitignored the moment it exists, not only when
`teamme_librarian_configure` happens to be called — gets five sections of its own, closing a real
defect: in 0.6.0 `apply_gitignore()` had exactly one call site, inside `configure()`, so a project
that only ever ran `teamme_librarian_refresh` (the path both the 0.6.0 changelog and the librarian's
own prompt describe) ended up with `.claude/librarians/sessions/` — an index of conversation text —
sitting in a file git would track, while this repo's own docs said it was "ALWAYS gitignored". Five
`git check-ignore`-grounded sections prove the fix: a refresh alone, with `configure` never called,
protects both `index.db` and `sessions/`, while a foreign `.gitignore` line survives byte-intact and
three refreshes leave the file byte-identical after the first; a read-only `teamme_librarian_status`
on an empty project creates neither `.claude/librarians/` nor `.gitignore` — a diagnostic call must
never be the thing that conjures state; an unwritable `.gitignore` does not block a refresh — the
index is still written and the result says out loud that it could not be protected rather than
silently succeeding; `commit_record=true` followed by a plain refresh with no second `configure` call
leaves `sessions/` ignored while `commits.jsonl` becomes committable, proving the always-ignored and
the choice-dependent paths are independent of each other; and a `[watch-fail]` **reduced ordering
sweep** drives all 24 permutations of 4 entry points — `status`, `refresh`, `configure(enable)`,
`configure(commit_record)` — checking, after *every single step* rather than only at the end of each
sequence, that if an index exists on disk it is already protected. That per-step check is the only
assertion that actually tests "no ordering leaves a gap" rather than one path through the tool set;
trimmed from the lane's own 504-permutation, 9-entry-point sweep to keep CI's runtime sane, but the
property kept is the one that matters.

The cross-index join (`server/librarian/cross.py`) — `around_path`, `around_commit`, `around_task`,
`timeline`, served through the existing `teamme_librarian_query` tool rather than a new one — gets
four sections. `around_path`'s history rows are checked directly against `git log`, with the `cross`
module itself out of the loop, so the assertion cannot agree with a bug in the same code it is
checking. `absent`, `disabled`, `empty` and `error` are proven to be four distinct, non-overlapping
per-store states for both the history and the session store — `error` specifically has to be produced
by `chmod 0o000` on a *valid* index, because a plain corrupt file is discarded and rebuilt by
`connect_or_reset()` into `absent`, not `error`; the lane verified that distinction held before
writing the assertion. A corrupt `worklog.json` is proven to make `timeline()` return an empty, named
`error` store state rather than raise, and `around_commit` is proven to refuse outright — distinctly
for an absent history index versus a disabled one — since it cannot resolve its anchor commit without
one. And the `pad_minutes` boundary is checked at the second: a commit that lands 30 seconds after a
task's `status_changed` is missed at `pad_minutes=0` and caught by the default 15-minute pad, with the
widened window reported in the answer rather than applied silently.

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
- **Team agents are generated per project; librarians are shipped by the plugin — two tiers, not a
  reversal of "the plugin ships no agents of its own."** A team is generated because it must mirror
  *that* project's actual layer boundaries — there is no fixed roster that fits every codebase. A
  librarian is shipped because "read the git history and answer from an index" is the same job in
  every project; regenerating a fresh prompt for it on every install would just be re-deriving one
  answer each time. `plugins/teamme/agents/history-librarian.md` is therefore not offered in
  `/teamme:init-team`'s roster questionnaire and is not roster-selectable at all — it is present the
  moment the plugin is installed, in every project, whether or not that project has even run
  `/teamme:init-team`. Consultation is an **instruction**, not a gate: `commands/init-team.md`'s
  shared guardrail block and `templates/intake.md` step 1 tell team agents to consult the librarian
  for history questions and cite its answer, but nothing blocks a brief that skips it — the same
  register as every other convention this project states rather than enforces (see the MCP install
  gate entry above). Disabling a librarian (`teamme_librarian_configure`, per project) cannot make
  the *agent* disappear — a plugin-shipped agent's visibility is not something a project's own
  config controls — so disabled means the librarian's own tools (`teamme_librarian_refresh`,
  `teamme_librarian_query`) refuse and name how to re-enable, while `teamme_librarian_status` keeps
  answering so the refusal is diagnosable rather than a silent dead end. This entry **replaces** the
  earlier one that read "the plugin ships no `agents/` of its own by design — teams are generated per
  project": that sentence is now false in both halves, and its consequence for validation is in Known
  gaps.
- **The librarian storage fork is *enacted*, not merely recorded.** `teamme_librarian_configure`'s
  `commit_record` key does not just sit in `.claude/librarians/config.json` — flipping it writes or
  removes the project's `.gitignore` entry for the append-only record
  (`.claude/librarians/*/commits.jsonl`), so the choice actually takes effect instead of sitting in a
  file nobody reads. Everything teamme writes lives inside one marked block (`# teamme librarians -
  managed by the teamme_librarian_configure tool` … `# end teamme librarians`); only lines inside
  that block are ever removed. An ignore rule the project already had, for its own reasons, outside
  that block is never teamme's to delete — the same posture as never deriving a verdict from
  something that can silently grow or shrink underneath you. If a foreign line elsewhere is already
  ignoring the record while `commit_record` is set `true`, the tool does not let that pass silently:
  it reports that the record is **still ignored** by that line and names it, since `commit_record:
  true` with the record still out of git is exactly the confidently-wrong state this project's docs
  discipline exists to prevent. `.claude/librarians/index.db`, the derived SQLite index, is never a
  choice — it is written to the same block regardless of `commit_record`, because a binary index
  cannot be merged.
- **Co-change coupling, not static parsing, is the dependency signal — the rejected option is the
  instructive part.** The obvious way to build "what depends on what" is a parser per language. teamme
  is stdlib-only (invariant #3) and project-agnostic (invariant #4), so that would mean one parser per
  language, shipped with zero dependencies, silently producing nothing on any stack nobody wrote one
  for — the exact "indistinguishable from not working" failure invariant #3 exists to prevent.
  Co-change needs no parser: files that changed in the same commit are related, weighted by how often,
  read entirely off the `files_changed` rows phase 1 already writes — no new table, no new index pass.
  It works on every language plus configs and docs, and it catches edges static analysis cannot see (a
  schema and the migration that follows it, a feature and its docs). Static parsing remains available
  later as a per-language enrichment layer; it is not the foundation. The vocabulary this buys is
  enforced everywhere the result is read: "changes with", never "depends on", "imports" or "requires" —
  a caveat on the tool's own output cannot govern an agent's prose, so the rule is written directly into
  `agents/history-librarian.md`, not left for the tool output to carry alone. Every co-change row
  carries its own evidence (shared-commit count, the partner's own count, the most recent shared commit)
  rather than a bare ranking position, and a row backed by one shared commit is marked `weak` rather than
  presented the same as one backed by forty. A commit touching more than `DEFAULT_MAX_COMMIT_FILES` (25)
  files is excluded from every edge count as a sweep — a reformat, a license pass, a rename — because
  one such commit couples everything it touched to everything else; the cap is overridable per call, and
  every answer reports how many commits were considered and how many were skipped, so the largest input
  to a ranking is never invisible.
- **No separate code librarian; `history-librarian` absorbs co-change instead.** The task that shipped
  this (T34) was titled "a code librarian... plus a tests librarian", but co-change is a **history**
  signal — the same commit stream, the same index, the same SHAs as everything else `history-librarian`
  already answers. A second agent querying the same source of truth under a different name would split
  "ask about history" across two places for no gain. A genuinely separate code librarian would be one
  built on *static* analysis — a different evidence base entirely — and that is not what this phase
  built. Tests get no separate agent either: a test file that keeps changing alongside a source file
  *is* the test-to-code edge on the same signal, reported as "these tests change with this code", never
  "these tests cover this code" — coverage is a claim about execution, and the index has none.
- **The librarian privacy guarantee lives at the chokepoint, not at a call site.**
  `store.connect_file()` is the single funnel every librarian index — history's and sessions' alike —
  is opened through, so `config.ensure_ignored()` runs there, before the first byte of an index
  exists. A librarian that does not exist yet inherits the protection for free, because it has to go
  through that same funnel to be born. The owning project is derived from the *path* being opened
  (`.../.claude/librarians/...`), never from `CLAUDE_PROJECT_DIR` or the current working directory —
  deriving it from the environment would let the guard protect one project while a different one's
  index was actually being written. The shape of the fix matters as much as the fix itself:
  `apply_gitignore()` having exactly one conditional call site, inside `configure()`, was the *bug* in
  0.6.0 (see the next entry), so the fix could not be one more call site to remember at the next
  librarian's write path — it had to be structural. `apply_gitignore()`'s own read-modify-write is now
  taken under a lock, because it used to run only on an explicit `configure` call and now runs on
  every index open: two concurrent refreshes could otherwise interleave and drop a line the user wrote
  themselves.
- **A documented default that nothing enacts is not a default.** `commit_record` has always defaulted
  to `false`, and the docs described that as "the record stays out of git" — but before 0.7.0,
  `commits.jsonl` (and `.claude/librarians/sessions/`, which is never a `commit_record` choice at all)
  was only actually ignored if `teamme_librarian_configure` had been called at least once. A user who
  only ever ran `teamme_librarian_refresh` — the exact path both the 0.6.0 changelog and the
  librarian's own prompt describe — got an un-ignored index of their own conversation transcripts,
  while every doc in this repo told them it was safe. This is a different failure shape from the
  copied-fact drift T22/T26 paid for: those were a stated fact that had drifted away from the code;
  this was a stated fact the code never enacted in the first place.
- **The cross-index join is time-aligned and file-anchored, never id-based — measured, not assumed.**
  The obvious join is by citation: a task id in a commit message, a commit hash in a work-log note.
  Measured on this repository before `server/librarian/cross.py` was written: commit messages cite a
  task id in only 3 of 18 commits, and work-log notes cite a short SHA in only 4 places, both
  incidentally rather than by convention. A join keyed on citation would have returned almost nothing
  while looking exactly like "nothing happened" — the worst outcome available, an empty answer that
  reads as an absence of work rather than an absence of a recorded link. What is reliable is what a
  machine wrote on both sides: TIME (every store has it, retroactively, for everything collected so
  far) and FILE PATHS (`files_changed` comes from git's own `--numstat`; a session `file` mark comes
  from an observed tool call — neither is prose). So every association `cross.py` reports is inference
  from overlap, and every row and caveat says so: "active while" and "around", never "implements" or
  "caused" — the same discipline the co-change queries follow with "changes with", never "depends on".
  The work log is read live from `.claude/intake/worklog.json` on every call and joined in memory,
  never indexed into SQLite: it is the source of truth and it is small — tens of tasks — and a second
  copy of it in a store is precisely the failure this project has already paid for repeatedly (see
  T22/T23 and the `.db`-is-derived-and-disposable entry below). `ATTACH DATABASE` was considered and
  rejected: either the history or the session index may legitimately be absent or empty, and a single
  statement spanning both would turn a missing index into a failed query instead of a partial answer
  that names the empty store — and naming the empty store, not hiding it, is the whole point. The row
  cap is applied *per store*, not per answer: a shared cap against this repository's 18 commits and
  3,136 turns would return turns and no commits at all, which is exactly the thin-but-confident answer
  this module exists to avoid.
- **Lazy indexing over the harness's own transcripts, not a `PreCompact`/per-turn hook — and the
  original premise was wrong.** The session librarian was first specified as continuous, in-flight
  capture, so recovery would never depend on a hook firing at exactly the right moment. That premise
  turned out unnecessary: the harness already writes the whole conversation, append-only, to
  `$CLAUDE_CONFIG_DIR/projects/<slug>/<session>.jsonl`, and the file survives compaction regardless,
  because it is a file rather than context. So `sessions.py` indexes exactly like `history.py` —
  incrementally, by byte offset, on demand — adding zero new hooks, nothing joining `REQUIRED_HOOKS`,
  and no per-turn latency. The rejected design is worth recording here rather than only in the task
  notes: "index it live" is the obvious answer, and the reason it is unnecessary is not obvious until
  the transcript itself is inspected.
- **Compaction needs no `PreCompact` hook, verified rather than assumed.** A transcript records its own
  compaction boundary — a `system` record with `subtype: compact_boundary` carrying pre/post token
  counts, followed by the harness's own summary written into the conversation. The `compaction` query
  reads that record directly; nothing needs to observe the event as it happens.
- **The session index is never committable, regardless of `commit_record`, for a different reason than
  the `.db` is.** `.claude/librarians/sessions/` sits in the same always-ignored `.gitignore` block as
  `index.db`, so `commit_record` cannot reach it — but the reasoning differs. The `.db` is excluded
  because a binary index cannot be merged; the session store is excluded because a transcript holds
  everything anyone typed, including a secret pasted in by accident, and indexing it makes a second copy
  of a private surface. `history` keeps a committable text record (`commits.jsonl`) precisely because it
  is mergeable and worth sharing; the session librarian keeps no equivalent record at all (next entry) —
  there is nothing there to offer the same choice about.
- **No JSONL record for the session librarian.** `history` keeps an append-only text record because it
  is mergeable and worth committing. Here the transcript already *is* that record — the harness's own
  append-only file — so a second text copy would double a private surface for nothing.
  `.claude/librarians/sessions/index.db` is disposable and rebuilt by reading the transcripts again,
  the same relationship `history`'s `.db` has to `commits.jsonl`, except here the rebuild reads the
  harness's own files directly rather than a teamme-owned record.
- **`transcripts.py` is teamme's second assumption about Claude Code's own on-disk layout.**
  `preflight.py`'s install-record lookup was the first; this is the second, contained the same way —
  every function that touches the harness's transcript-file convention (where the files live, what a
  line in one looks like) sits between one banner comment and the next in `transcripts.py`, and is read
  live on every call rather than cached, so an upgrade that moves the layout is self-correcting instead
  of silently wrong. If the slug-encoded directory name does not resolve, `locate()` falls back to
  scanning `projects/` for a directory whose transcripts declare this project as their own `cwd` —
  ground truth from the files themselves, not a second encoding guess — and only after that returns a
  named "nothing found" rather than ever guessing.

## Known gaps

- No eval suite yet (`claude plugin eval`). The prompts (`init-team.md`, `team-doctor.md`,
  `intake.md`, `queue.md`) are checked only for parsable frontmatter; nothing tests what they
  instruct. The command-level preflight refusal, `/teamme:queue`'s one-line output contract, and
  `/intake`'s step 0a deferral short-circuit are all prompt text, not enforced behaviour.
- `templates/intake.md` has been exercised on one real project (a Minecraft Fabric mod). The
  `{{PLACEHOLDER}}` set may not fit stacks with very different doc conventions.
- **`plugins/teamme/agents/history-librarian.md`'s frontmatter is validated; what it instructs is
  not.** `validate.sh`'s manifest check no longer globs `commands/*.md` only — that glob repeated the
  exact `REQUIRED_HOOKS`-style mistake (see T23 below) and would have let `agents/` ship unchecked. It
  now walks the whole plugin tree for `*.md` and requires every prompt file to be accounted for by a
  directory it knows how to validate, so a third prompt surface added later fails loudly instead of
  passing silently. For an agent specifically it parses the frontmatter, requires `name` and
  `description`, and asserts none of `FORBIDDEN_AGENT_KEYS` (`permissionMode`, `hooks`, `mcpServers`)
  is set. What it still does not do — for this file or any command — is test what the prompt
  *instructs*: the co-change vocabulary rule, the query-composition walk, the output contract. That
  gap is the no-eval-suite entry above, not a separate one. Generated, per-project team agents remain
  unvalidated beyond frontmatter parsing, for the original reason — a team is derived from that
  project's own layout, so there is nothing fixed to check beyond that.
- **The intended-caller convention for the librarian tools is unenforced.** The agent, the tool
  descriptions, `commands/init-team.md`'s shared guardrail block and `templates/intake.md` step 1 all
  say `history-librarian` is the intended and only sanctioned caller of
  `teamme_librarian_query`/`teamme_librarian_refresh` — but nothing stops any other agent with MCP
  access from calling those tools directly instead of consulting the librarian. The only real
  refusal is `teamme_librarian_configure`'s disabled-librarian gate, and that only fires when the
  *librarian itself* is switched off for the project — it says nothing about who is calling.
- **The bounded list queries carry the commit subject, not the body.** `recent`, `commits_touching`,
  `files_in_commit`, `commits_between` and `search_subjects` all return the subject line only;
  `commit_detail` is the one query that returns the full message body (capped at 4000 characters),
  and it is what the librarian reaches for when a subject alone does not explain a change. Deliberate
  row-shape economy, not an oversight — but a claim that the librarian "explains why a change was
  made" rests on that second, targeted query, not on the list queries by themselves.
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
- **`config.py`'s atomic write is the part `validate.sh` still does not drive.**
  `teamme_librarian_configure` itself is covered — the disabled-librarian gate, `commit_record`
  enacted and checked with `git check-ignore`, a foreign ignore rule left alone, a corrupt config
  degrading to defaults, and the ordering sweep all exercise it. What no section reaches is the
  failure path *inside* the write: a crash or a full disk between the temp file and the
  `os.replace()`. The entry above this one used to claim the whole tool was uncovered; that went
  stale when phase 2's assertions landed and was corrected in 0.7.0, which is the same
  documented-claim-outlives-the-code shape that `commit_record`'s unenacted default had.
- The `commits_touching`, `commits_between`, `search_subjects` and `hotspots` query variants are
  exercised while building fixtures and ground truth for other assertions, but none has a
  `validate.sh` section asserting its own result directly — only `recent`, `files_in_commit`,
  `changes_with`, `coupling_between`, and the unknown-query/bad-hash error paths are. `hotspots` is
  the one phase-3 co-change query with no assertion of its own at all (tracked as T35, alongside the
  renderer gap below).
- **The MCP renderers are untested (tracked as T35).** `validate.sh`'s co-change assertions call
  `store.query()` directly — correctly, since the module must not be allowed to agree with itself —
  but that means `_render_cochange` in `teamme_mcp.py`, the prose text a librarian agent actually
  reads back over the MCP pipe, has no `validate.sh` coverage, and neither does `commit_detail`'s
  renderer. The co-change renderer was checked by hand over a real pipe (the header reads "correlation,
  not a call graph", the evidence base and overlap are present, damping is explained) — unasserted, not
  unknown.
- **Four prompt-to-renderer mismatches, found while writing `agents/history-librarian.md` and not yet
  reconciled.** The agent prompt documents named row fields (`shared_commits`, `jaccard`, `weak`, and
  so on); `_render_cochange` emits prose instead, not those field names. `share_of_anchor` is computed
  in `store.py` and never rendered by `teamme_mcp.py`. The overlap tie-break in `changes_with`'s
  `ORDER BY` is invisible in the rendered output, so the prompt can only claim "most shared first", not
  the tie-break rule itself. `weak` means one *shared* commit in `changes_with` but one commit *total*
  in `hotspots` — the same word carrying two different thresholds, each documented correctly in
  isolation but not called out as differing between the two queries.
- Shallow-clone behaviour is untested. `history.probe()` reports `shallow: true` and `index()` notes
  it in the result, but no `validate.sh` fixture is an actual shallow clone.
- Merge commits get `commit_parents` rows but no `files_changed` rows (`git log --numstat` reports no
  diff for a merge without `-m`/`-c`). Whether a merge needs file rows is a phase-2 decision, if the
  reasoning layer ends up needing merge diffs.
- `rewrite_records()`'s preservation of non-commit records (`kind != "commit"`, meant for phase 2's
  reasoned entries) through a full reindex has no test coverage, since no phase-2 code writes such a
  record yet.
- **The session librarian's tools exist; nothing consults them.** `teamme_librarian_query`'s
  `sessions`/`search_turns`/`window`/`compaction` queries are live on the MCP server, but
  `agents/history-librarian.md`, `commands/init-team.md`'s shared guardrail block and
  `templates/intake.md` step 1 mention only `history-librarian` and git history — none of them
  instructs any agent to ask the session index about lost context. Until an agent (or an instruction
  telling `history-librarian` to reach for it) is added, the session librarian is reachable only by an
  agent that happens to know the tool exists and calls it directly.
- The following are unasserted by `validate.sh` for the session librarian: the subagent-sidecar
  indexing path (a session's `subagents/agent-*.jsonl` children are indexed and reported by
  `sessions`/`search_turns` in code, but no fixture builds one to prove it); `locate()`'s cwd-based
  fallback scan, used when the slug-encoded directory name does not resolve; the `compaction` query and
  its `MAX_LOST_MARKS` cap on the spine it lists; a concurrent-refresh test for `sessions` (the
  ten-concurrent-refresh assertion exists for `history`, not for `sessions`); and the per-turn
  `MAX_TURN_CHARS` index-time cap.
- **`around_commit` and `around_task`'s row *content* is not ground-truthed the way `around_path`'s
  is.** `around_path`'s history rows are checked directly against `git log`; nothing yet checks that
  `around_commit`'s session/task rows or `around_task`'s commit/session rows are the *right* rows —
  only `around_commit`'s refusal path (absent vs. disabled history) is directly asserted, alongside
  the four per-store states and the `pad_minutes` boundary.
- **`timeline`'s row selection is unasserted beyond the corrupt-worklog degrade.** Its three per-task
  event kinds (`task_created`, `task_status`, `task_note`) and its commit/session rows have no
  ground-truth check of their own.
- **The ordering sweep does not exercise the sessions-refresh entry point or `force=true`.** The
  24-permutation sweep covers `status`, `refresh` (history), `configure(enable)` and
  `configure(commit_record)`; a fifth and sixth entry point — refreshing the *session* librarian, and
  a forced repair — were dropped to avoid needing a real transcript fixture, and are unexercised by it.
- **The prose/basename matching in `around_path`'s task rows is unexercised.** The fallback that
  matches a bare filename (e.g. `worklog.py`) when the full repo-relative path misses a task's title
  or notes has no assertion of its own.
- **This repo's own `.gitignore` has no `sessions/` entry.** Nothing has leaked, because no session
  index has ever been built here — `commits.jsonl` is this repo's only librarian file, and it is
  tracked by deliberate choice, unrelated to this fix. The first `teamme_librarian_refresh
  {"librarian": "sessions"}` run in this repo will write the rule itself; nothing needs doing by hand.

## Conventions

Commits: one imperative, sentence-case line; no scope prefix. Keep `CHANGELOG.md` current for
anything user-visible.
