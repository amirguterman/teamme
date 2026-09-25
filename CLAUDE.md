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
  commands/team-doctor.md         diagnoses/repairs an existing install on demand; a prompt, not code -
                                   also runs and reports `preflight.py roster`, a separate verdict
  commands/modify-team.md         changes an EXISTING team - add/drop/retool/rename a lane - without
                                   re-running init-team; migrates the work log's `lane` fields and
                                   never touches a closed task; a prompt, not code (see Decisions
                                   already made: "A roster is four copies")
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
    hooks/*.py                    project-agnostic hook scripts, including preflight.py, whose
                                   `roster` subcommand is a SEPARATE verdict from `check` - does the
                                   team roster still agree with itself in the four places it is
                                   written down? - never folded into `check`'s exit code (see
                                   Decisions already made)
    hooks/librarian-gate.py       PreToolUse on `git push`: ASKS (never denies) when the history
                                   index is behind HEAD, reading a one-line marker rather than the
                                   database - see Design invariants #1 and Decisions already made
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
and `intake_dir` checks each failing independently with a `fix:` line, and both halves of the
install-evidence probe asserted to have no false positive: a stranger's repo carrying its own
unrelated `SessionStart` hook, and — sharpened in T46, after a probe that tested existence but
reported generation misread a project's own unrelated `.claude/commands/intake.md` as a teamme
install and pointed it at a repair that writes teamme scaffolding into a repo that never asked —
a stranger's own `intake.md`, checked in both directions: the bare file (no teamme hook named in its
text) still reads `not-installed`, and the same file with one line added naming `worklog.py` moves
off it. A `[watch-fail]` confirms the probe matches on `any()` of `REQUIRED_HOOKS`, not `all()`: the
first-ever `templates/intake.md` (`6582ed4`) names only `intake-state.py` and `worklog.py`, since
`preflight.py` did not exist yet, so `all()` would regress every pre-`preflight.py` install to
`not-installed` — the same trap `installed` derivation paid a P0 for, reintroduced through a
different probe. Four fail-open cases for that same text probe — an unreadable file, a directory
sitting at that exact path, non-UTF-8 content, and a hook name that only appears past the probe's
1 MiB read cap — are each proven to degrade silently to `not-installed`, never to raise or to wrongly
claim evidence. `heartbeat` is proven silent and always exit-0, even with no `.claude/` and
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

`retitle` and `reopen` get their own sections, each with a watch-fail. `retitle` is proven to append a
note naming the old title, leave `status_changed` unmoved, and leave a freshly-armed `Stop` reminder
silent — a field edit, not a status transition — plus the cheap edges (a same-title call is a silent
no-op, not a junk note; an empty new title exits 2) and the MCP path over the real pipe reporting the
same new title the CLI wrote (watch-failed by folding `retitle` into `STATUS_ACTIONS`, which moved
`status_changed` and re-armed `Stop`). `reopen` and the closed-task refusal are proven across all
fifteen combinations (five status actions × three closed statuses) exiting 4 and naming `reopen`, with
`done → dropped` — a correction between conclusions, not a reopen — still succeeding at exit 0; and the
load-bearing property, that `Stop` stays silent immediately after a reopen because it lands on `open`
and never `active`, is proven directly and over the MCP pipe's refusal path too (watch-failed by
changing `reopen`'s landing status to `active`, which made `Stop` wrongly nag). Two honest limits: the
fifteen refused combinations are driven by one loop rather than fifteen separate watch-fails — one
representative break (the shared refusal condition disabled) proves the mechanism the sweep depends
on, not each combination individually; and neither new action gets its own MCP-path watch-fail,
reasoned rather than assumed — `tool_worklog` forwards every action past `add`/`list`/`next`/`stats`
through the same generic `argv = [action, id, text]` branch with no per-action logic, `retitle`'s MCP
section already drives that exact branch to a full success result, and `reopen`'s MCP section drives
its refusal through the same `WORKLOG_ACTIONS` gate — so a divergence in that shared forwarding code is
not an unwatched risk the way two *independent* implementations (the `hook_freshness()` shape) would
be. Judge that reasoning against `tool_worklog` yourself if you touch it; it holds only as long as
`retitle`/`reopen` stay generic there.

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
never be the thing that conjures state; an unwritable `.gitignore` does not block a refresh OR a
query against the same still-unprotected index — the index is still written (or read), `isError`
stays `False` for the query, and both results say out loud that the index could not be protected
rather than silently succeeding, with refresh's own render checked to appear *exactly once*
(watch-failed by removing `with_protection_report`'s dedup check on a scratch copy, which rendered a
second `UNPROTECTED:` block) and a healthy, writable fixture's query results checked to carry none at
all — the false-positive case, since "exactly one" and "a query reports it" could both pass while the
feature tagged every answer, broken or not; `commit_record=true` followed by a plain refresh with no
second `configure` call
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

The freshness gate that closes the loop on the history librarian — `librarian-gate.py`, a `PreToolUse`
hook on `git push` — gets five sections of its own against real git fixtures, the hook always run as a
subprocess and never imported, plus a sixth under `preflight.py`'s own coverage: a refresh clears a
proven-stale gate, and the pathspec exclusion that makes that possible is watch-failed by re-running a
scratch copy with the exclusion removed, which re-arms on its own refresh commit and is asserted to
count exactly one; the ask-and-never-deny contract is checked structurally, not by grepping for the
word "deny" — the assertion enumerates every `permissionDecision` literal the source can emit and
requires the set to be exactly `{"ask"}`; the push/non-push classification boundary is pinned with
three matching and three non-matching commands against the same stale fixture; and seven fail-open
branches are asserted silent — not a push, no index at all, the `history` librarian disabled, an
unparseable `config.json`, a marker that is not a commit hash, a marker unreachable from `HEAD`
(watch-failed by removing the reachability check on a scratch copy, which then wrongly asks on the
rebase fixture), and `git` missing from `PATH`. Two of those seven — no index, and git missing — are
each guarded by two independent mechanisms, so neither is watch-failable by one surgical break; the
lane verified both by hand and said so rather than claiming a watch-fail it did not perform. The
disabled-librarian and unparseable-config branches are proven by differential — the same stale fixture
with one file changed — not by breaking `history_enabled()` itself. `preflight.py` gets the sixth
section: `librarian-gate.py` missing from an otherwise-complete install drives `installed-outdated`,
names the file in the `hooks` check's detail line, and is never told to run `init-team` — T23 surviving
a seventh entry in `REQUIRED_HOOKS`. The marker's own gitignore status is proven under both
`commit_record` values with `git check-ignore` as ground truth, the same style the rest of the
librarian's privacy guarantee is checked. What none of this proves: the count is `HEAD`-relative, not
push-relative — the hook never contacts a remote, and pinning that boundary would mean building a fake
upstream for a property the code already makes structurally true by never calling one; and shallow
clones remain unfixtured, as they already are for `history.py` generally (see Known gaps).

Two more sections check the *documentation* itself against the code, closing a gap that let three real
0.6.0 mistakes ship in `CHANGELOG.md` — a session query typo'd as `search` instead of `search_turns`, a
query name that was never real, and a phantom tool — none of it compared against the source before it
shipped. `identifiers-exist` resolves every backticked lowercase identifier in `CHANGELOG.md`,
`README.md` and `plugins/teamme/README.md` against the live `TOOLS` registry, the three `QUERY_NAMES`
tuples, every tool schema's parameter and enum values, and a vocabulary of real-but-non-callable names
derived mechanically from dict-key literals and `CREATE TABLE` statements — never a hand-typed
allow-list, since that would be the next copied fact this check exists to stop making. A narrow suffix
rule accepts genuine shorthand (`configure` for `teamme_librarian_configure`, matched only as an exact
trailing word after an underscore) after a strict full-names-only policy was tried first and rejected:
it flagged a real, already-released shorthand sitting in the 0.7.0 `CHANGELOG.md` entry.
`rendered-labels-exist` checks every documented `` `key: value` `` output claim against
`teamme_mcp.py`'s own renderer source — this is what would have caught 0.6.0's `` `has_data: false` ``,
a real internal dict key the renderer never actually prints by that name (it prints
`data: yes`/`data: no`). **Neither check catches a bare value claim** — 0.6.0 also documented "defaults
to disabled" against `DEFAULT_ENABLED = True`, invisible to identifier extraction; see Known gaps and
`CONTRIBUTING.md` for how that class of mistake is handled without a check.

`preflight.py roster` — a third subcommand beside `check` and `heartbeat`, taking the same
`--project-dir`, checking whether the agent files, `.claude/agents/README.md`'s roster table,
`.claude/commands/intake.md`'s lane mentions, and every open task's `lane` in the work log still agree
— gets eight sections of its own, two of them watch-fails, all against throwaway fixtures rather than
this repo's own `.claude/agents/`. A clean fixture PASSes all three checks (`roster_readme`,
`roster_command`, `roster_tasks`) and is used to pin the exact detail-line wording. `roster_readme`
fails in both directions on one line — a missing row and a row naming no agent file — from a single
fixture. `roster_command`'s narrow, whole-word claim is pinned at its edge: `app-api-tests` does not
satisfy `app-api`, and a bare prose mention with no table at all PASSes, which is the check's own
documented limit, not a bug. Closed tasks naming a dead lane are proven exempt — `done`/`declined`/
`dropped` — with a watch-fail (a scratch copy of `preflight.py`, never the file in place, per
`CONTRIBUTING.md`'s rule on watch-failing code another lane owns) that removes the closed-status skip
and confirms `roster_tasks` then fails on the identical fixture. The closed-status list itself is read
out of `worklog.py`'s own source text rather than copied a second time, proven by a differential (the
real, unmodified `preflight.py`) rather than a break. Seven distinct missing/unreadable/corrupt
inputs — no `.claude/agents/`, no roster README, an unreadable roster README, no `intake.md`, no
`worklog.json`, a corrupt one, one with no `tasks` key — are each proven to degrade to `SKIP`
("not verified: ...", naming what could not be read), never to a false `PASS`. An agent's identity is
proven to be its frontmatter `name:`, not the filename stem, and a `<name>.md.disabled` file is proven
genuinely out of the roster (a README row still naming it fails). The load-bearing property gets its
own watch-fail in both directions: a fully scaffolded, heartbeated (`live`) fixture with a deliberately
drifted roster leaves `check`'s exit code, `state:` line and exact six-item check set untouched, and
the string `roster` never appears in `check --json`'s own output — while `roster` itself, run against
the same fixture, exits non-zero; and, in reverse, a project whose roster genuinely agrees but was
never scaffolded at all gets `check` pinned exactly to `not-installed` — tightened by T46 from
accepting either non-installed state, since that fixture's own `intake.md` names no teamme hook and
can no longer read as evidence — and a zero-exit `roster`, so neither verdict can drag the other
down. A scratch copy that folds `roster()`'s checks into `diagnose()` confirms the property
is not vacuous: the same drifted-but-live fixture immediately flips `check`'s exit code, state and id
set once the separation is removed.

The manifests check gained two things alongside `roster`. `check_scalar_shapes()` flags a command or
agent frontmatter scalar that starts with an unquoted `[` or `{` — this repo's lenient frontmatter
reader treats `[what to change...]` as plain text, but a real YAML parser reads an unquoted leading
bracket or brace as a flow sequence or mapping, so a value shaped that way would read green here and
parse differently elsewhere. Found by inspection, in a draft of `modify-team.md`'s own
`argument-hint`, not by this check — which is exactly why it exists now; it was verified by hand, in
both directions, against that near-miss, not by a `scripts/validate.sh` watch-fail. A command-count
lock-in (`plugins/teamme/commands/*.md` must be exactly four, named by file) guards the manifests
check's own file-discovery glob: `modify-team.md` was new and untracked when this landed, so a glob
that silently missed it would have proven nothing.

Four more sections (T50) close holes the register in Known gaps had already named. `commits_touching`,
`commits_between` and `search_subjects` each get their own ground truth read straight from `git log` —
filtered to a path, a since/until boundary deliberately set to land exactly on two commits' own epochs
(proving the bounds are inclusive, not off-by-one), and an independent substring scan of subjects — with
the librarian module out of the loop, the same doctrine `changes_with`'s ground-truth section already
used. None of these carries an **encoded** watch-fail — no break-and-restore lives inside `validate.sh`
for them, unlike the nineteen `[watch-fail]`-tagged sections that do (`grep -c 'echo "== .*\[watch-fail\]'`
against the real file is how to count them). The breaks were **performed and reverted**, not never
attempted: the lane read and broke three exact lines on scratch copies of `store.py` and watched each
assertion above fail before reverting — the `ORDER BY` behind `commits_touching`
(`store.py:1208`, `ORDER BY c.epoch DESC, c.hash LIMIT ?`), the `commits_between` boundary
(`store.py:1231`, `where.append("epoch >= ?")`, narrowed to `>` to confirm the inclusive-bounds fixture
would catch it), and the `ORDER BY` behind `hotspots`' ranking (`store.py:1111`,
`ORDER BY commits DESC, last_epoch DESC, f.path LIMIT ?`), plus `search_subjects`' `LIKE` pattern
(`store.py:1247`, the leading `%` in `"%" + _like(text) + "%"`). Say this plainly rather than either
overclaiming a watch-fail that is not in the suite or underclaiming a break that genuinely happened:
the assertion is not vacuous *today*, proven once, by hand, on the day this landed — but nothing
re-proves that on the next run, and a later refactor could make one of these four checks vacuous with
nothing here to say so (see Known gaps). `hotspots` gets the same ground-truth treatment, counted
straight from `git log --name-only`, covered by the same performed-not-encoded break above. Two renderer
sections
follow: `_render_rows` — behind `recent`, `commits_touching`, `commits_between`, `search_subjects` and
`files_in_commit` — had only ever had its outer `"N row(s)"` wrapper checked; it is now driven over the
real pipe with row content asserted for both its bracketed shape (`commits_touching`, which carries a
`[path ...]`) and its unbracketed one (`commits_between`, `search_subjects`), and `hotspots` is rendered
and checked the same way `changes_with` already was. `_render_cross`'s `around_task` branch, previously
unasserted beyond surviving the call, is driven over the real pipe and checked to name the task, its
padded window and the commit that closed it. `around_path`, `around_commit`, `coupling_between`'s own
rendering, and all four session queries remain unexercised through the real pipe — see Known gaps' T35
and T54 entries.

T48 adds an `ERR` trap to `validate.sh` itself: on an unexpected non-zero exit anywhere in the script it
names the failing line and the command that ran, using `${BASH_LINENO[0]}` rather than `$LINENO` —
verified empirically, since a plain `$LINENO` read inside the trap reports the trap's own line, not the
line of the command that actually failed. The lane found no live unwrapped failure while building this;
it is defence-in-depth for the next one, not a fix for a current gap.

## Design invariants

These are not style preferences. Breaking one ships a trap to someone else's machine.

1. **Hooks fail open — with exactly one carve-out.** Missing, malformed or stale state, an
   unparseable payload, a path outside the project — every one of these must ALLOW the write. A
   broken guard must never block work. The phase lock also expires on a timeout so a crashed session
   cannot leave a repo write-locked. The carve-out: a hook MAY **ask** instead of silently allowing,
   but only on a well-formed state it is genuinely confident about — `librarian-gate.py`'s `git push`
   reminder is the one hook that does this, and only when the history index exists, is enabled, and
   its marker parses and is reachable from `HEAD` with a positive commit count behind it. Every other
   branch, including every failure to read or parse that same state, still ALLOWs. `deny` remains
   reserved for the intake write guard alone; no other hook denies anything, and `ask` is never a
   license to add one.
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
  teamme's own scripts, and/or a `.claude/commands/intake.md` whose own text names one of those same
  scripts — and neither probe changes when a release adds a hook script. The second probe used to test
  only the file's existence while its reason string claimed generation; T46 (see Verify above)
  tightened it to what the reason string always claimed, after that gap let a project's own unrelated
  `intake.md` read as a teamme install and get pointed at a repair that writes teamme scaffolding into
  a repo that never asked for it — the mirror image of the T23 P0: that one told installed users they
  were not installed and offered a destructive reinstall; this one told never-installed users they
  were installed and offered a write. A missing hook *script* became a **repair** condition
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
- **This repo's own `commits.jsonl` stays gitignored, deliberately — the freshness gate existing does
  not by itself change that (T47).** `.gitignore`'s comment above that rule used to read as a
  deferral: track it once `librarian-gate.py` exists, because until then a committed index just goes
  stale on every commit that does not refresh it. `librarian-gate.py` shipped in 0.8.0, its
  precondition was met, and nothing brought anyone back to reopen the decision it was gating — the
  comment kept naming a condition that had already come true, and `CLAUDE.md`'s Known gaps kept saying
  the opposite of what the comment said. Decided now: keep it ignored. teamme ships `commit_record:
  false` as its default (`DEFAULT_COMMIT_RECORD`), and this repo is the main place that default gets
  dogfooded — committing our own record here would mean the project never exercises the default it
  ships. The `.gitignore` comment at that line now states this as a standing reason, not a precondition
  that has since come true; never remove the rule itself, only rewrite the reasoning above it, and only
  teamme's own reasoning — this project's standing discipline is to never remove a `.gitignore` line it
  did not write. This is a different failure shape from a stale claim drifting away from the code
  (T22/T26) or a gap entry outliving its gap (`retitle`/`reopen`, above): here the doc did not rot on
  its own — the world moved past a condition it named, and nothing was watching to reopen the
  deferral. Watch for the shape again: a comment that says "until X happens" is a debt that needs a
  trigger to revisit it, not just a memory of having written it.
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
- **The push reminder reads a text marker, never the database — and `ask` is a deliberately narrow
  second verb, not a weakening of invariant #1.** `librarian-gate.py` is a `PreToolUse` hook on `git
  push`: it prompts, never denies, when the project's history index is confidently behind `HEAD`. It
  is *copied* into a project's `.claude/hooks/`; `server/librarian/` is not, so the hook cannot import
  `history.py` and cannot query the SQLite index directly without carrying a second copy of its schema
  into a file that will drift from the first — the exact shape of T22/T23/T26/T40. Instead
  `history.index()` publishes `.claude/librarians/history/indexed_head`, one line, the indexed commit
  hash, from the same statement that writes `META_LAST_INDEXED` to the database — a derivation with a
  single writer, not a second copy. The residual risk is stated rather than hidden: the marker and the
  database can still diverge if something outside the indexer touches one of them (a hand edit, a
  restore from git, a half-finished write), so `teamme_librarian_status` reports the marker three ways
  — published and agreeing, not yet published, or `MARKER DIVERGED` (naming both hashes and pointing at
  `teamme_librarian_refresh`, which rewrites both together) — rather than trusting it silently.
  `.claude/librarians/*/indexed_head` joins the always-gitignored set for a third, distinct reason: the
  `.db` is excluded because binary cannot merge, `sessions/` because a transcript is a private surface,
  the marker because it is a statement about what *this machine* has indexed, and committing it would
  hand a teammate a marker already behind the commit that carries it — which is also loop fuel (next).
  The gate is opt-in by construction, not by a separate switch: no index on disk means silence, so a
  project that never asked for a librarian is never nagged, and there is no way to keep the reminder
  while turning the rest of `history` off — disabling `history` (`teamme_librarian_configure`) silences
  the gate along with `refresh` and `query`, on purpose, because the gate reads the same enabled flag.
- **The gate's own remedy must not re-arm it, and that was measured, not argued.** With
  `commit_record=true`, refreshing writes `commits.jsonl`, so the commit that carries the refreshed
  record is itself unindexed the instant it lands. `librarian-gate.py`'s `rev-list` excludes
  `.claude/librarians` by pathspec for exactly this reason: measured at the same marker, the count came
  back 1 (the refresh's own commit) without the exclusion and 0 with it, and `validate.sh` turned that
  measurement into a watch-fail — a scratch copy with the exclusion removed re-arms and is asserted to
  count exactly that one commit. Same shape as invariant #2's `status_changed`-not-`updated` stamp: an
  enforcement hook must never be able to react to its own effect.
- **`retitle` does not violate the append-only ledger — a title is current scope, not history.**
  `worklog.py`'s ledger is append-only about what happened, not about what a task is called right now:
  a stale title actively lies at every `SessionStart`. T14's title claimed "Ship 3 librarian agents…
  selectable in `/init-team`" for eight days after every clause of it had gone false. `retitle`
  replaces the `title` field and appends the old text as a note, so the record stays complete — the old
  title is still there, in the notes — while the screen stops lying.
- **A closed task refuses to reopen silently — a CLI-level refusal invariant #1 does not reach, and
  `reopen` lands on `open`, not `active`.** `start`/`dispatch`/`block`/`unblock`/`defer` on a `done`,
  `declined` or `dropped` task now exit 4 (distinct from 2 = usage, 3 = no such task) and name
  `reopen`; `done`/`decline`/`drop` on an already-closed task still succeed, since that is a correction
  between conclusions, not a reopen. This does not weaken invariant #1: `worklog.py` is a CLI a model
  runs deliberately, not a `PreToolUse` hook standing between a session and its own edit, so refusing
  here blocks nobody's editing loop — and a warning printed *after* the status already changed could
  only be superseded in an append-only ledger, never undone, so refusing before the write is the only
  point a mistake is still cheap to catch. `reopen` itself lands on `open`, deliberately not `active`:
  the `Stop` hook nags on `active` alone (invariant #2), so a reopened task re-enters the
  `SessionStart` listing without arming a reminder for work nobody has picked up yet — invariant #2's
  reasoning applied to a new action, not rediscovered later by trapping a session.
- **The privacy chokepoint gained the loose-module import fallback every other cross-module import in
  `server/librarian/` already had, and a query now reports what only a refresh used to.**
  `store.ignore_guard()` imported `config` with a bare relative import; every other cross-module import
  in the package already falls back to a plain `import config` for the case where `server/librarian` is
  loaded off `sys.path` rather than as a package (see `store.py`'s own comment at `ignore_guard()`).
  `ignore_guard()` was the one path that lacked it, so that specific `ImportError` degraded silently
  into the ordinary problem-dict return with nobody told. Not live today — both real callers
  (`teamme_mcp.py`, `store.py` itself) import as a package — but the shape mattered: the one code path
  carrying teamme's privacy guarantee was also the one that could fail invisibly. Fixed at both levels.
  The second half closes the asymmetry that made a failure here genuinely invisible: the refresh paths
  already render their own `ensure_ignored()` result (`_render_ignore_state` in `teamme_mcp.py`), but a
  *query* opening an unprotected index said nothing at all. `store.note_unprotected()`/
  `take_unprotected()` now record a failure at the one chokepoint every index is opened through
  (`connect_file()`), and `teamme_mcp.py`'s `with_protection_report()` drains and appends an
  `UNPROTECTED` block to *any* of the four librarian tools' results, not only refresh's — never
  changing `isError` on its own and never raising, since an index that could not be protected is still
  a usable answer. See Verify above for how `validate.sh` proves this, including the false-positive
  check against a healthy fixture.
- **`teamme_worklog` is the sanctioned ledger path for an agent with no `Bash` tool, and a generated
  roster must resolve the tool name live, not hardcode it.** A tool name that does not resolve in the
  calling session grants nothing and says nothing — the same failure shape invariant #3 exists to
  prevent for a missing `jq`. The instruction carries an explicit fallback: if `teamme_worklog` is not
  in that agent's tool list, or the call fails, it hands the note back to whatever dispatched it,
  marked unrecorded, rather than improvising a write some other way — `.claude/intake/worklog.json` is
  taken under a lockfile by `worklog.py`, and a `Write`/`Edit` against it races that lock and can lose a
  concurrent note, exactly the failure the lock exists to prevent.
- **A documented value — a default, a cap, a count — is only as honest as the constant behind it, and
  `validate.sh` cannot check that mechanically.** `identifiers-exist` and `rendered-labels-exist`
  (T37a/T37b, above) resolve a documented *identifier* or *rendered label* against the live code, but
  neither sees a bare value written in prose — 0.6.0 shipped "defaults to disabled" against
  `DEFAULT_ENABLED = True` and no identifier extraction would have caught it. Rather than leave that
  class of mistake unaddressed until something builds a value-diffing check, the rule is written into
  how these docs are written (see `CONTRIBUTING.md`): a doc stating a default, cap or count names the
  constant that backs it, so a reader can grep the real value instead of trusting the prose. This was
  checked against the numeric and boolean defaults already stated in `README.md` and
  `plugins/teamme/README.md` (`max_files`/`DEFAULT_MAX_COMMIT_FILES`, `pad_minutes`/
  `DEFAULT_PAD_MINUTES`, `enabled`/`DEFAULT_ENABLED`, `commit_record`/`DEFAULT_COMMIT_RECORD`) at the
  time this rule was adopted, and all four matched; the rule's value is in catching the *next* drift,
  not this one.
- **A roster is four copies, and a wizard that writes them without proof would be the fifth instance
  of the same bug class — so `/teamme:modify-team` ships with `preflight.py roster` as a separate
  verdict, deliberately not folded into `check`.** Three shipped places told a user never to re-run
  `/teamme:init-team` over a working team, and `/teamme:team-doctor` repairs scaffolding only, never
  `.claude/agents/` — a documented refusal with no enacted alternative. A grep of both READMEs, all
  three existing commands, `CLAUDE.md`, `CONTRIBUTING.md` and the roster README for any sanctioned way
  to change a roster returned zero hits. Measured, not assumed: a roster is one fact stored in four
  places — the agent files, `.claude/agents/README.md`'s table (plus its tool rationale and dependency
  order, neither of which is checked — see Known gaps), `.claude/commands/intake.md`'s lane mentions,
  and every task's `lane` field in the work log (40 of 45 tasks in this repo's own ledger carry one; 5
  open, 35 closed) — and changing one without the others is exactly T22/T23/T26/T40's shape, a fifth
  time. Two things were rejected: a wizard alone, with no proof it left the four copies agreeing (the
  exact shape that has bitten four times already); and deriving the README table and the lane mentions
  from agent frontmatter at read time, which is the structurally cleaner answer but would change the
  generated-prompt contract for every existing install — the same upgrade pain `installed` derivation
  paid a P0 to avoid. What shipped instead: `/teamme:modify-team` (add, drop, retool or rename a lane)
  writes all four copies and migrates the affected tasks' `lane` fields through `worklog.py lane`,
  never by hand; a dropped or renamed agent's file is moved aside to `<name>.md.disabled`, never
  deleted, the same reversible-disable preference `init-team.md` already states for skills and MCP
  servers; and a closed task is never re-laned — that agent really did do that work, and the ledger is
  append-only about what happened, the same reasoning `retitle` already established for titles.
  `preflight.py roster` is the proof: three checks (`roster_readme`, `roster_command`, `roster_tasks`),
  three marks (`PASS`, `FAIL`, and `SKIP` for "could not be read, so not verified" — never silently
  read as agreement), exit 0 iff all three `PASS`. It is kept out of `check`'s exit code on purpose: a
  stale roster README costs a reader a wrong document, not a broken team, and letting it halt `/intake`
  would repeat the exact over-reach deriving `installed` from a growing hook list already paid a P0 to
  unlearn (see the `installed` entry above). The separation is proven both directions with its own
  watch-fail — see Verify above. T57 (Known gaps, below) later found a fifth copy —
  `teamme-tech-lead.md`'s own `Owns` column, tracking the same map by hand — already drifted, and
  closed it by deleting the column rather than reconciling it: the orchestrator does not route from
  scratch, since the approved brief `/intake` hands it already names the owning agent per part
  (`.claude/commands/intake.md:261`), and `/intake` may only name a lane from its own table
  (`.claude/commands/intake.md:207-210`). So the fifth copy was load-bearing for nothing, and a copy
  with no job left to do is better deleted than kept correct-for-now — the first time this bug class
  was closed by deletion rather than reconciliation.
- **A minimum `ok:` count for `validate.sh` was proposed (T50) and rejected.** The idea: assert the
  script prints at least N `ok:` lines, so a run that silently does less than it should is visible.
  Rejected for two reasons, not one. First, a hardcoded floor needs bumping on every brief that adds a
  section, which is exactly the class of stale, unenforced claim this repo keeps paying to correct —
  see the `REQUIRED_HOOKS` entry and the `commits.jsonl`-gitignore comment entry above, both the same
  shape: a number or a condition written once and never revisited as the world moved past it. Second,
  and more load-bearing, it would not even catch the failure it is aimed at: a loop that should iterate
  N times and silently iterates zero still prints exactly one `ok:` line for the section around it — a
  count checks how many `ok:` lines appeared, not which assertions actually ran inside them. The
  unbuilt counter-proposal, if this is ever revisited, is a per-section marker checked against a static
  list of section names rather than a bare count — recorded here so it is not re-proposed from scratch.

## Known gaps

- No eval suite yet (`claude plugin eval`). The prompts (`init-team.md`, `team-doctor.md`,
  `intake.md`, `queue.md`) are checked only for parsable frontmatter; nothing tests what they
  instruct. The command-level preflight refusal, `/teamme:queue`'s one-line output contract, and
  `/intake`'s step 0a deferral short-circuit are all prompt text, not enforced behaviour.
- `templates/intake.md` has been exercised on one real project (a Minecraft Fabric mod). The
  `{{PLACEHOLDER}}` set may not fit stacks with very different doc conventions.
- **Whether a real, user-edited `intake.md` can drift far enough to stop naming any teamme hook is
  unmeasured.** T46's `_text_names_a_teamme_hook()` (see Verify above) was checked against every
  version of this repo's own `templates/intake.md` across its history — 22 to 27 references, never
  zero — but that is this repo's own template, not a generated file a user has since hand-edited.
  Nobody has a corpus of those to check against; the probe's fail-open behaviour (degrading to
  `not-installed` rather than raising or guessing) is what a genuinely bare file falls back to, and
  that path is proven, but whether real edited files ever reach it is not.
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
- `route-to-intake.py`'s `/queue` passthrough, its blank/missing-prompt silence, and its ordinary
  work-request guidance are asserted; its phase-aware mid-flight routing (pointing a message at the
  triage rules instead of starting a fresh brief) is not. `reground`, out-of-project paths and
  stale-state expiry in `intake-state.py` remain unexercised by the smoke test.
- **T17 and T20 closed, not everything they touched.** `teamme_intake_phase` is now gate-tested
  independently of `teamme_worklog` (its own refusal, naming `teamme_install`, against a fresh
  unscaffolded project, plus a watch-fail with its `gate()` call removed on a scratch copy), and
  `route-to-intake.py`'s `/queue`/blank-prompt paths and `preflight.py`'s heartbeat under a real pty
  are both asserted — the two "checked by hand, not yet asserted" gaps this entry used to record. What
  T20 left: the `isatty()` branch of `drain_stdin` could not be isolated by a differential watch-fail
  — with the master pty fd closed before the child starts, both the tty and non-tty branches converge
  on the same observable outcome (silence, exit 0), so only the pty case itself is asserted; that one
  branch is not independently isolable this way.
- **A lane with no `Bash` tool still cannot run `./scripts/validate.sh` itself.** `teamme_worklog` is
  the sanctioned ledger path for such a lane (see Decisions already made), but that only covers
  recording work — it does not let that lane verify a change it made. That half of T32 is unfixed and
  may be unfixable by design: there is no MCP tool standing in for a shell, and adding one would widen
  what an agent without `Bash` can do well past running a fixed verification script.
- **`retitle`/`reopen` and the query-side of the `UNPROTECTED` fix are proven now** (see Verify
  above) — this entry used to say they were not; a gap entry is a claim too, and it went stale as fast
  as any other undocumented fact once the validation lane closed the work. Two honest limits survive
  from that closure, not fixed and not planned to be: the fifteen refused reopen combinations are
  driven by one loop with one representative watch-fail, not fifteen independent ones; and neither
  `retitle` nor `reopen` has its own MCP-path watch-fail, on the reasoning (checked against
  `tool_worklog` directly, not just asserted) that both forward through the exact same generic,
  per-action-logic-free branch that `retitle`'s own MCP success test already exercises — see Verify
  above for the reasoning in full, since it is load-bearing and worth re-checking if that branch ever
  grows a per-action special case.
- Version bumps are manual: `plugin.json` `version` plus a `CHANGELOG.md` entry.
- **Closed: T24, and it was worse than its own headline.** This entry used to say this repo's own
  `.claude/commands/intake.md` predates the preflight block entirely and so `/intake` here does not
  halt on an incomplete install the way a freshly-generated one would. Fixed — `.claude/commands/
  intake.md` now carries the Preflight section and `state:` handling verbatim — but reconciling the
  two files also turned up a live state-machine bug the headline never named: three of step 2b's six
  dispositions (already-satisfied, defer, decline) ended the flow with `stop` and no
  `intake-state.py release` call, so hitting any of the three left this repo write-locked until the
  one-hour stale timeout — the exact invariant #1 provision that kept it from being worse than a
  timeout. See the "`.claude/` is a filled copy" entry further down for what T24 says about `.claude/`
  more generally, alongside two later tasks that found the same shape.
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
- **T35 is now mostly closed (T50); this entry used to make four claims, and all four were stale or
  wrong by the time this pass reread them.** It used to say `commits_touching`, `commits_between`,
  `search_subjects` and `hotspots` had no `validate.sh` section asserting their own result, that
  `_render_cochange` had no coverage at all, and that `commit_detail`'s renderer had no coverage at
  all. The fourth claim was already false by the time this pass reread it, independent of T50: the
  `commit_detail` section asserts
  `"BODY TRUNCATED at 4000 of 5000 characters"` in the rendered text over the real pipe, which is
  `_render_detail`'s own output — a documented gap this repo's own docs discipline exists to catch, and
  did not, because `identifiers-exist`/`rendered-labels-exist` check identifiers and rendered labels,
  never a sentence claiming an absence. T50 closed the other three: `commits_touching`,
  `commits_between` and `search_subjects` each now have ground truth read straight from `git log`;
  `hotspots` has the same, plus the `changes_with`-style rendering treatment `_render_cochange` already
  had; and `_render_rows` (behind all five list queries) is now rendered and content-checked over the
  real pipe, not just structurally exercised (see Verify above for the T50 section names). What is
  still open: `coupling_between`'s branch of `_render_cochange` has ground truth but is never rendered
  over the pipe; `around_path` and `around_commit` have no `_render_cross` content assertion at all
  (`timeline`'s one pipe call only proves the server survives a corrupt worklog, not that its rendered
  text is right); and the four session queries render nothing through the pipe at all, tracked
  separately as T54 below, since that gap has its own shape and its own history (see the corrected
  session-librarian entry below).
- **T50's four ground-truth query sections were watched failing once, by hand, not encoded as
  `[watch-fail]`s in `validate.sh` — a real, stateable difference from the nineteen sections that are.**
  The lane broke `store.py:1208`'s `ORDER BY` (`commits_touching`), `store.py:1231`'s `epoch >= ?`
  boundary narrowed to `>` (`commits_between`), `store.py:1111`'s `ORDER BY` (`hotspots`), and
  `store.py:1247`'s leading `%` in the `LIKE` pattern (`search_subjects`) on scratch copies, watched
  each ground-truth assertion fail, and reverted — `git status` on `server/librarian/` came back clean
  afterward. That is real proof the assertions are not vacuous *today*. It is not the same guarantee an
  encoded `[watch-fail]` gives: nothing re-runs that break on the next CI run, so a later refactor of any
  of these four queries could make its ground-truth check vacuous again with nothing in `validate.sh`
  itself there to catch it. See Verify above for exactly which lines and what was broken.
- **Four prompt-to-renderer mismatches, found while writing `agents/history-librarian.md` and not yet
  reconciled.** The agent prompt documents named row fields (`shared_commits`, `jaccard`, `weak`, and
  so on); `_render_cochange` emits prose instead, not those field names. `share_of_anchor` is computed
  in `store.py` and never rendered by `teamme_mcp.py`. The overlap tie-break in `changes_with`'s
  `ORDER BY` is invisible in the rendered output, so the prompt can only claim "most shared first", not
  the tie-break rule itself. `weak` means one *shared* commit in `changes_with` but one commit *total*
  in `hotspots` — the same word carrying two different thresholds, each documented correctly in
  isolation but not called out as differing between the two queries.
- Shallow-clone behaviour is untested. `history.probe()` reports `shallow: true` and `index()` notes
  it in the result, but no `validate.sh` fixture is an actual shallow clone. The same gap reaches
  `librarian-gate.py`: its docstring says a shallow clone falls into the same silent branch as a
  rebase or force-push (the marker reads as unreachable from `HEAD`), but no fixture proves that path
  either.
- Merge commits get `commit_parents` rows but no `files_changed` rows (`git log --numstat` reports no
  diff for a merge without `-m`/`-c`). Whether a merge needs file rows is a phase-2 decision, if the
  reasoning layer ends up needing merge diffs.
- `rewrite_records()`'s preservation of non-commit records (`kind != "commit"`, meant for phase 2's
  reasoned entries) through a full reindex has no test coverage, since no phase-2 code writes such a
  record yet.
- **Closed: the session librarian's tools are now instructed, and this entry was already false when
  T50 reread it.** It used to say `teamme_librarian_query`'s `sessions`/`search_turns`/`window`/
  `compaction` queries were live but that nothing instructed any agent to ask the session index for
  lost context. All four are named in `agents/history-librarian.md`'s own LOCATE-then-READ working
  order and query table, and the same wiring reached `commands/init-team.md`'s shared guardrail block
  and `templates/intake.md` step 1 before this entry was next read. A closed gap left open in the
  register is the same failure as an open one left unrecorded — this pass found both at once, in the
  same paragraph of this file: this entry had gone stale in one direction while, separately, the
  cross-index queries (`around_path`/`around_commit`/`around_task`/`timeline`) had zero prompt callers
  and no gap entry naming that fact at all, until the same brief that closed this entry also wired
  those four into the same prompt. Treat a "Known gaps" line as a claim with the same shelf life as any
  other in this file: reread it against the code, not against memory of when it was written. The stale
  claim itself was not unique to this file: the identical sentence — "nothing yet tells an agent to
  consult `sessions`... nothing in the roster calls them yet" — was duplicated verbatim in `README.md`
  and `plugins/teamme/README.md` too, so the same fact went stale in three places at once, at 0.8.0,
  and was corrected in all three the same day it was found. That is exactly the copied-fact shape this
  gap register exists to catch elsewhere (see T22/T23/T26/T40 and the roster-is-four-copies entry
  above), except this time it happened to the gap register's own claim about itself.
- **The session librarian's four queries are never driven through the real MCP pipe (T54).** Every
  `validate.sh` assertion for `sessions`/`search_turns`/`window`/`compaction` calls
  `sessions.query()`/`sessions.index()` directly, module-level, the same way `history`'s ground-truth
  sections correctly keep the librarian module out of the loop for *ground truth* — but unlike
  `history`, no section for `sessions` also drives the same query through `teamme_mcp.py` over a real
  JSON-RPC pipe the way `commit_detail`, `changes_with` and (since T50) `commits_touching`,
  `commits_between`, `search_subjects`, `hotspots` and `around_task` all do. `_render_sessions_status`,
  `_render_sessions_privacy` and `_render_session_rows` — the prose the session librarian actually
  reads back — are therefore unexercised in practice, even though the query logic underneath them is
  well covered. Named rather than covered thinly: a synthetic transcript fixture exists for the session
  librarian's other six sections (see Verify above), so extending one of them through the pipe is the
  concrete next step, not a redesign.
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
  index has ever been built here. `commits.jsonl` is this repo's only librarian file on disk, and — see
  the "stays gitignored, deliberately" entry above (T47) — it is *not* tracked; an earlier version of
  this entry said the opposite, which was itself stale by the time it was corrected. The first
  `teamme_librarian_refresh {"librarian": "sessions"}` run in this repo will write the `sessions/` rule
  itself; nothing needs doing by hand.
- **Two of `librarian-gate.py`'s seven fail-open branches are proven only by hand, not by a
  `validate.sh` watch-fail.** No-index and git-missing are each guarded by two independent mechanisms
  inside the hook, so neither is watch-failable by a single surgical break; the validation lane
  verified both manually and said so rather than claiming a watch-fail it did not perform. The
  disabled-librarian and unparseable-config branches are proven by differential instead — the same
  stale fixture with one file changed — rather than by breaking `history_enabled()` directly.
- **The push reminder's commit count is `HEAD`-relative, not push-relative.** `librarian-gate.py`
  never contacts a remote and contains no remote call, so what it counts is commits since the marker
  on `HEAD`, not commits the push is actually about to transfer. Left unpinned deliberately: pinning
  it against a real remote would mean building a fake upstream in `validate.sh` for a property the
  code already makes structurally true by never calling one.
- **`preflight.py roster` proves the roster table agrees; it cannot tell whether the surrounding prose
  is true.** `roster_command` (agent files vs. `.claude/commands/intake.md`) is a whole-word name
  search over the whole file, not a table parse: an `intake.md` that names every agent only in prose,
  with no lane table at all, PASSes. That is a stated limit of the check, not a bug — hunting for "the
  lane table" inside a per-project, prose-tailored prompt would be a check that passes for the wrong
  reason. `roster_readme` checks the roster table's rows in both directions, but nothing checks the
  README's model/tool rationale or its dependency order, and `roster_command` cannot catch a lane-table
  row left behind for a lane `/teamme:modify-team` just dropped, or a row whose description drifted
  false while the agent's name still appears somewhere in the file. `/teamme:modify-team`'s own Phase 4
  and Phase 5 tell the operator this in the same words, so the limit is stated at the point someone
  would otherwise mistake a `PASS` for more than it is.
- **`.claude/` is a filled copy of what `plugins/teamme/` generates, and `validate.sh` never opens
  it — six tasks have now found drift there.** The suite walks `plugins/` for manifests, hook syntax
  and prompt frontmatter; the one thing under `.claude/` it touches at all is `preflight.py roster`
  (see above), and that check is narrower than it sounds — it proves the agent files,
  `.claude/agents/README.md`'s table, `.claude/commands/intake.md`'s lane mentions and the work log's
  `lane` fields still name the same lanes. It says nothing about whether a `.claude/` copy's *prose*
  still matches the template or instruction it was filled from; that is a different question with no
  check of its own, mechanical or otherwise. Six instances, all closed: T24 (`.claude/commands/
  intake.md` missing `intake-state.py release` on three of six step-2b dispositions — see the entry
  above); T50 (a stale session-librarian claim duplicated in `CLAUDE.md` and both READMEs — not a
  `.claude/` copy, but the identical shape, caught the same pass); T51 (zero of six
  `.claude/agents/*.md` files carried the `## Consulting the librarian` contract `init-team.md` has
  required of every generated roster since 0.6.0, and all six separately stated design invariant #2
  backwards — "stamps itself against the task's `updated` time" where it must read `status_changed` —
  teaching exactly the trap the guardrail exists to prevent); T56 (`teamme-hook-engineer.md` described
  five hooks against a `REQUIRED_HOOKS` of seven, naming neither `preflight.py` nor
  `librarian-gate.py`, and its ownership — frontmatter `description:` and body alike — omitted
  `plugins/teamme/server/**/*.py`, which `.claude/agents/README.md`'s own roster row and
  `.claude/commands/intake.md:201` both already assign to that lane); T57 (`teamme-tech-lead.md`
  carried a fifth, unchecked copy of the roster's ownership map, with two of its cells already
  drifted — see "A roster is four copies" above for how this one was closed, and why that closure
  differs from the other five); and T58 (`teamme-validation-engineer.md` described a 96-section,
  19-watch-fail suite as four checks, and named two gaps as still open that T20 and T17 had already
  closed; `teamme-prompt-author.md` claimed "nothing checks `.claude/agents/*.md` or
  `.claude/commands/intake.md` at all" — false since `preflight.py roster` shipped, and false in the
  *worse* direction: an overstated coverage claim gets caught the next time someone relies on it, but
  an understated one removes the very step that would have caught it — it told the one lane that edits
  `.claude/` not to bother running the one check that reaches it). The T58 correction pass wrote a
  fresh overclaim of its own, caught on review rather than by any check: a draft said the
  settings-template check verifies the four hook events "and wired to the right scripts", when the
  check only requires the four event names as keys and never reads a `command` string — all four
  events present with `Stop` pointed at the wrong script would still pass it. Fixed to say exactly
  that and to name the false-pass case; worth recording because a pass whose entire purpose was
  removing overstated claims produced one of its own, the honest measure of how easily this class of
  mistake happens. The contract itself now lives in `.claude/agents/README.md`'s "Consulting the
  librarian" section and identically in all six agent files; it is not restated here, and should not
  be — a second copy in this file is exactly the class of drift this entry exists to name. Nothing
  here proposes a check for this — a template-vs-copy diff would need to know which parts of a
  `.claude/` file are supposed to be filled-in and project-specific versus carried verbatim, which is
  exactly the judgement `init-team.md` itself makes at generation time and no mechanical diff can
  recover after the fact.

## Conventions

Commits: one imperative, sentence-case line; no scope prefix. Keep `CHANGELOG.md` current for
anything user-visible.
