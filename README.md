# teamme

A Claude Code plugin that gives any project a **tailored subagent team**, a single **`/intake`**
front door, and a **work log that will not let work be forgotten**.

## Install

```
/plugin marketplace add amirguterman/teamme
/plugin install teamme@teamme
```

Then, in any project:

```
/build-agent-team
```

## What `/build-agent-team` does

It reads the project first — manifests, docs, CI, the real build and test commands, the "never do X"
rules — and proposes a roster derived from *that project's* actual layer boundaries, not a fixed list
of job titles. You approve the roster, the model tiers, the tool scoping and the environment cleanup
through a questionnaire before anything is written.

It then installs:

- **`.claude/agents/*.md`** — the approved specialists, each with a tight tool list and a shared
  guardrail block built from the project's own working rules.
- **`.claude/commands/intake.md`** — the only sanctioned way to request work.
- **`.claude/hooks/`** plus the matching `settings.json` hooks.

## The `/intake` flow

`/intake <anything you want>` is the front door. It does not just accept the request — it decides
what to do with it.

1. **Triage** — if work is already in flight, intake decides whether this new message folds into it,
   queues behind it, or supersedes it. You are never asked to triage.
2. **Ground** — reads the project's spec first and says whether the request is already specified,
   already implemented, unspecified, or contradicts a documented rule.
3. **Classify** — maps it onto the real layer boundaries and names the owning agent for each part.
4. **Decide if and when** — intake has the authority to answer *do now*, *do next*, *already
   satisfied*, *defer (spec only)*, *decline*, or *needs input*. Declining a request that would
   damage the architecture — with the reason and the nearest legitimate alternative — is a real
   outcome, not a failure.
5. **Brief and confirm** — a written brief with the layer decomposition, dependency-ordered plan,
   spec delta, risks, done criteria and how the work can actually be verified.
6. **Dispatch** — specialists, in dependency order.

## The hooks

| Hook | Event | What it does |
|---|---|---|
| `route-to-intake.py` | `UserPromptSubmit` | Routes work requests into `/intake`. Phase-aware: a mid-flight message is pointed at the triage rules rather than at starting a fresh brief |
| `intake-guard.py` | `PreToolUse` | **Denies every project edit while a brief is still being written** |
| `worklog-enforce.py session` | `SessionStart` | Surfaces unfinished and deferred work so nothing is lost across sessions |
| `worklog-enforce.py stop` | `Stop` | Refuses to end a turn while a task is still marked active, so status gets recorded |

### Why the read-only phase is not plan mode

Plan mode's read-only status is inherited by subagents, so running intake inside it would freeze the
very specialists intake exists to dispatch. `teamme` enforces the same guarantee with its own phase
lock and lifts it exactly at approval.

Every guard **fails open** — missing, malformed or stale state, an unparseable payload, a path
outside the project — and the phase expires on a timeout. A crashed session can never leave a
repository write-locked. Edits under `.claude/` are always allowed so the flow can manage itself.

The `Stop` hook reminds at most once per status change, so it enforces without any possibility of
looping.

## The work log

`.claude/hooks/worklog.py` is a durable, prioritized ledger — committed to the project if you want
it shared, and independent of the transient phase lock.

```bash
python3 .claude/hooks/worklog.py list
python3 .claude/hooks/worklog.py add "..." --priority P0 --lane <agent>
python3 .claude/hooks/worklog.py block T3 "waiting on a decision"
python3 .claude/hooks/worklog.py defer T5 "spec'd, build later"
python3 .claude/hooks/worklog.py stats
```

Statuses: `open`, `active`, `blocked` (unfinished); `deferred` (intentionally not now); `done`,
`declined`, `dropped` (closed). Priorities: `P0` now, `P1` normal, `P2` someday.

## Requirements

`python3` on `PATH`. The hooks deliberately avoid `jq` and other optional tools, so they work on a
bare machine.

## License

MIT — see [LICENSE](LICENSE).
