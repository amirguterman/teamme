# The teamme agent team

This repo has its own agent team installed, scaffolded by `/teamme:build-agent-team` from the very
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
| `teamme-hook-engineer` | opus | Read, Edit, Write, Grep, Glob, Bash | `plugins/teamme/templates/hooks/*.py`, `settings.hooks.json` |
| `teamme-prompt-author` | opus | Read, Edit, Write, Grep, Glob | `plugins/teamme/commands/*.md`, `templates/intake.md` |
| `teamme-validation-engineer` | sonnet | Read, Edit, Write, Grep, Glob, Bash | `scripts/validate.sh`, `.github/workflows/` |
| `teamme-docs-writer` | sonnet | Read, Edit, Write, Grep, Glob | `README.md`, `CLAUDE.md`, `CONTRIBUTING.md` |
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

## Work log

```bash
python3 .claude/hooks/worklog.py list      # what is outstanding
python3 .claude/hooks/worklog.py next      # highest-priority open task
python3 .claude/hooks/worklog.py stats
```

`.claude/intake/worklog.json` is durable and meant to be committed. `.claude/intake/state.json` is
the transient phase lock — gitignored, and it expires after an hour so a crashed session can never
leave the repo write-locked.
