# The teamme agent team

This repo has its own agent team installed, scaffolded by `/teamme:init-team` from the very
templates it ships. That recursion is deliberate — teamme is dogfooded on itself — but it has one
sharp edge, so read the warning below before changing a hook.

## Entry point

**All work requests enter through `/intake <what you want>`.** Not a direct message to an agent, not
a plain "please fix X". A `UserPromptSubmit` hook injects a reminder when a prompt looks like a work
request, and a `PreToolUse` guard denies project edits while a brief is still being written, so the
rule is enforced by the harness rather than merely documented.

`/intake` grounds the request in `CLAUDE.md`, decides whether it should happen at all, writes a
brief, gets it approved, and only then dispatches. It also triages mid-flight messages itself — fold
in, queue, or redirect — rather than asking you to.

## Roster

| Agent | Model | Tools | Owns |
|---|---|---|---|
| `teamme-tech-lead` | opus | all (inherits) | Orchestration only. Never implements |
| `teamme-hook-engineer` | opus | Read, Edit, Write, Grep, Glob, Bash | `plugins/teamme/templates/hooks/*.py`, `settings.hooks.json`, `plugins/teamme/server/**/*.py` — the only lane permitted to edit `*.py` |
| `teamme-prompt-author` | opus | Read, Edit, Write, Grep, Glob, `teamme_worklog` | `plugins/teamme/commands/*.md`, `templates/intake.md`, `plugins/teamme/agents/*.md`, and this repo's own `.claude/` prompt copies |
| `teamme-validation-engineer` | sonnet | Read, Edit, Write, Grep, Glob, Bash | `scripts/validate.sh`, `.github/workflows/` |
| `teamme-docs-writer` | sonnet | Read, Edit, Write, Grep, Glob, `teamme_worklog` | `README.md`, `CLAUDE.md`, `CONTRIBUTING.md` |
| `teamme-release-manager` | haiku | Read, Edit, Grep, Glob, Bash | manifests, `CHANGELOG.md` |

### Why these tiers

**Opus** goes where a mistake is expensive and subtle. The hook scripts run inside other people's
editing loops, and every one of their guarantees is a negative — *fails open in every branch*,
*cannot fire twice* — which is exactly the kind of property that reads fine and is wrong. The
prompts get opus because they *are* the product and nothing tests them.

**Sonnet** implements against a clear spec: an assertion to add, a claim to bring back in line with
reality.

**Haiku** does the bookkeeping that `validate.sh` fully covers — if the release manager gets a
manifest wrong, the build says so immediately.

### Why these tool lists

`teamme-prompt-author` and `teamme-docs-writer` have **no `Bash`**: their work is text, and they
hand verification to the lanes that own it. `teamme-release-manager` has **no `Write`** — it only
edits four files that already exist. `teamme-tech-lead` inherits everything so it can dispatch, and
is held to "never implements" by its prompt rather than by its tool list.

The two `Bash`-less lanes carry one MCP tool each —
`mcp__plugin_teamme_teamme__teamme_worklog`, shown as `teamme_worklog` above — because the work log
is the one file every lane must write and its only other writer is a CLI. Without it they had no
sanctioned way in, and the observed result was a lane editing `.claude/intake/worklog.json` with
`Edit` while `worklog.py` held the lock. The tool takes the lock properly and puts no shell in the
path. The half this does **not** fix: neither lane can run `./scripts/validate.sh`, so both hand it
back to `teamme-tech-lead` unrun, and both say so rather than implying otherwise.

## Changing the roster

Never hand-edit `.claude/agents/*.md`, this README's table, `.claude/commands/intake.md`'s lane rows,
or a task's `lane` field to change who owns what — those are four copies of one fact, and drifting one
without the others is a bug. Use `/teamme:modify-team` to add, drop, retool or rename a lane; it
writes all four and migrates the affected open tasks' `lane` fields through `worklog.py lane`. Check
the four still agree at any time with:

```bash
python3 .claude/hooks/preflight.py roster
```

A separate verdict from `preflight.py check` above — three marks (`PASS`, `FAIL`, `SKIP` for "could not
be read, so not verified"), never folded into `check`'s exit code, so a stale roster README never halts
`/intake`. See `CLAUDE.md`'s "A roster is four copies" entry for why.

## Dependency order

Behaviour → proof → description → version.

1. `teamme-hook-engineer` / `teamme-prompt-author` — the behaviour changes first
2. `teamme-validation-engineer` — a new branch is not covered until the assertion exists
3. `teamme-docs-writer` — documents what is now *proven*, so it needs step 2
4. `teamme-release-manager` — version and changelog last

`teamme-tech-lead` enforces this. Never let a dependent lane start first.

## ⚠ The copies are not the source

`.claude/hooks/*.py` are **copies** of `plugins/teamme/templates/hooks/*.py`, installed exactly the
way a user's project gets them. They were byte-identical at install time and nothing keeps them that
way.

- Editing the **template** does not change this repo's behaviour until it is re-copied.
- Editing the **copy** changes this repo's behaviour and ships nothing.

Always change the template, then re-copy:

```bash
cp plugins/teamme/templates/hooks/*.py .claude/hooks/
```

## Shared guardrails

Every agent carries the same block, drawn from `CLAUDE.md` and `CONTRIBUTING.md`: work enters
through `/intake`; verify with `./scripts/validate.sh` before claiming anything works; `python3`
only; hooks fail open; enforcement hooks cannot loop; `templates/hooks/` stays project-agnostic; the
command copies rather than re-authors; commits are one imperative sentence-case line; stay in your
lane.

### Consulting the librarian

All six agents also carry a second common block, the **librarian consultation contract** —
`init-team.md` has required it of every generated roster since 0.6.0, and this repo's own copies —
which are filled copies, not sources — went without it until T51. It routes three kinds of question
to `history-librarian`, the agent the *plugin* ships rather than one this roster generated:

| Question | Ask for | Carry through |
|---|---|---|
| What changed, when, why — a file's history, when a convention appeared, "X was added in commit Y" | the history index | the commit SHAs it cites |
| What was **said** — decided, proposed, rejected; what a dispatched specialist reported; what fell out of context at a compaction | the session index (`compaction`/`search_turns` to locate, then `window` to read) | the session id and mark point |
| What was happening **around** a file, commit, task id or stretch of time | the cross-index join (`around_path`, `around_commit`, `around_task`, `timeline`) | "active while" / "around" — never "implements", "caused" or "fixes" |

`history-librarian` is **not** in the roster table above and must never be added to it: the table is
checked against the files in `.claude/agents/`, and a row naming an agent no file here owns fails
`preflight.py roster`. The plugin carries that agent; nothing in this repo owns it.

The block ends with an escalation clause, because five of the six lanes cannot actually reach the
librarian — only `teamme-tech-lead` inherits `Task`, and no lane is granted the librarian MCP tools
(`init-team.md` forbids it; the librarian is their intended caller). Those five hand a history or
conversation question **back to whoever dispatched them, marked unanswered**, and never re-derive it
from `git log` or a transcript. That is the third instance of one shape — a lane names the
capability it lacks and hands the work back rather than improvising around it: no `Bash` means the
work-log MCP tool in place of the CLI, and `./scripts/validate.sh` handed back unrun; no `Task`
means a history question handed back unanswered. Like every other convention here it is stated
rather than enforced — nothing blocks a lane that skips it.

## Work log

```bash
python3 .claude/hooks/worklog.py list      # what is outstanding
python3 .claude/hooks/worklog.py next      # highest-priority open task
python3 .claude/hooks/worklog.py stats
```

Written only through `worklog.py` or the `teamme_worklog` MCP tool — **never by editing the JSON**.
Both writers take an `O_EXCL` lock, concurrent writers are normal here, and the ledger is
append-only, so a note lost to a race can only be superseded, never repaired.

`.claude/intake/worklog.json` is durable and meant to be committed. `.claude/intake/state.json` is
the transient phase lock — gitignored, and it expires after an hour so a crashed session can never
leave the repo write-locked.
