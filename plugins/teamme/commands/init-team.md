---
description: Analyze the current project, propose a tailored team of Claude Code subagents (with skills/MCP), offer to disable unused context-bloating skills/MCP/plugins, then create the approved agents plus a project /intake command that is the team's single entry point.
---

You are setting up a tailored **team of Claude Code subagents** for THIS project, a project
**`/intake` command** that is the team's single entry point, plus trimming environment bloat. Do NOT
create or change anything until I approve via the questionnaire in Phase 3. Work in this order.

## Phase 0 — Preflight (before anything else)

Run this first, before reading a single project file:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/templates/hooks/preflight.py" check
```

It prints one `PASS`/`FAIL` line per item — `python3`, `hook scripts`, `intake command`, `hook
registration`, `intake state dir`, `hooks firing` — a `fix:` line under each failure, and a final
`state:` of `not-installed`, `installed-outdated`, `installed-not-live` or `live`. Exit 0 iff
everything passed. Show me that output verbatim rather than paraphrasing it, then act on the state:

| State | What it means | What to do |
|---|---|---|
| the command itself fails — `python3: command not found` | The check could not run. Every hook, the work log and the phase lock are `python3` scripts. | **Halt.** Nothing teamme ships can run here. Tell me to install python3 (system package manager, or python.org) and re-run this command. Offer no repair — there is nothing to repair with. |
| `not-installed` | No evidence teamme was ever set up here — no registered teamme hooks, no generated `/intake` command. The project has never been scaffolded. | **Expected here — this command is the installer.** Say so in one line and continue to Phase 1; Phase 5 creates all of it. Do not halt. |
| `installed-outdated` | teamme is **already** installed here — the check names the evidence — but part of the scaffolding is missing or stale, usually an install from an earlier release that predates a file this version ships. | **Stop and say this is probably not the command I want**, in one line: the project already has a team, and re-running the installer re-runs the questionnaire and regenerates the roster over agents that already work. The missing pieces are a repair — point me at `/teamme:team-doctor`, or the `teamme_install` MCP tool, which restores them without touching the existing team. Ask before going further. Continue into Phase 1 only if I say I want a full re-install anyway. |
| `installed-not-live` | The scripts and the `hooks` block are both present, but no `SessionStart` has fired here, usually because `.claude/` held no settings file when the session started. Nothing is being enforced right now. | Continue — Phase 5 re-copies the scaffolding — but tell me at hand-over that `/hooks` or a restart is required before the phase lock is real. |
| `live` but some check still `FAIL`s (a missing script, an unparseable `settings.json`, an unwritable `.claude/intake/`) | A previous install was interrupted, or files were deleted or edited by hand. | Name the exact failing items, then **offer to repair and ask before writing**: re-copy the missing scripts from `${CLAUDE_PLUGIN_ROOT}/templates/hooks/`, merge the `hooks` block from `${CLAUDE_PLUGIN_ROOT}/templates/settings.hooks.json` into the existing settings file without disturbing anything else, create `.claude/intake/`. A JSON syntax error in a settings file is mine to fix — report it, do not rewrite the file around it. Re-run the check afterwards. |

If the MCP tools are available, `teamme_status` gives the same diagnosis and `teamme_install`
performs the repair. Prefer them when present; the script is the fallback. `teamme_worklog` and
`teamme_intake_phase` refuse to run until the install is complete, which is a second confirmation of
the same state, not a different opinion.

Be honest about what this is: a check you run and a refusal you choose, not a gate the harness
enforces. Nothing stops a later turn from skipping it. Say that plainly if I ask.

## Phase 1 — Analyze the codebase (read-only)
Discover what this project actually is. Do not assume a stack.
1. Read the root context files if present: `AGENTS.md`, `CLAUDE.md`, `README*`, `CONTRIBUTING*`,
   and any `/docs` or `/documentation` tree (treat an authoritative docs tree as the source of
   truth and read its index first).
2. Detect the stack and tooling from manifests: `package.json`, `pyproject.toml`/`requirements*.txt`,
   `go.mod`, `Cargo.toml`, `Gemfile`, `pom.xml`/`build.gradle`, `composer.json`, etc. Note the
   languages, frameworks, package manager, and the **build / test / lint / typecheck / run /
   deploy** commands that already exist (scripts, Makefile, CI config).
3. Map the architecture: top-level folders, the major subsystems/domains, where the data layer /
   API / UI / background jobs / infra live, and any framework with non-obvious conventions
   (note version-specific breaking changes and where their docs live, e.g. vendored docs).
4. Capture the project's **non-obvious working rules**: docs-update rules, codegen steps, DB or
   migration workflows, i18n/localization, auth/security model, commit/branch conventions, and
   any "never do X / always do Y" instructions in the context files.
5. Note what already exists: `.claude/agents/`, `.claude/hooks/`, `.claude/commands/`,
   `.claude/settings*.json`. Don't duplicate existing agents.

## Phase 2 — Inventory the environment (read-only)
Enumerate what's installed and judge what's actually useful for THIS project.
1. **Skills** — list native/built-in skills and any plugin / 3rd-party skills available this
   session (these are surfaced in the session context and under `.claude` / plugin configs).
2. **MCP servers** — list configured servers (`.mcp.json`, project/user settings, `claude mcp
   list`) and note which expose tools relevant to this project vs. irrelevant ones whose schemas
   just consume context.
3. **Plugins** — list installed plugins and which of their commands/skills/agents are relevant.
4. For each item, classify: **Relevant** (maps to this project's stack/workflow) /
   **Maybe** / **Unused junk** (no plausible use here, costs context). Be specific about WHY.

## Phase 3 — Propose the team + present a high-detail questionnaire
Derive the roster from the codebase — do NOT use a fixed list. Typical roles to consider and
keep only those the project justifies: tech-lead/orchestrator, architect, domain specialists per
major subsystem, frontend, UI/UX, backend/API, data/DB, AI/ML or prompt engineer (if the app has an
AI surface), i18n/localization, infra/DevOps/deploy, QA/test, security, docs/librarian,
designer/asset. Do NOT propose a product/intake *agent* — intake is a command (Phase 5, step 2),
not a lane. For EACH proposed agent specify:
- **name** (kebab-case, project-prefixed), one-line **description** written so the orchestrator
  auto-delegates, recommended **model** (opus for judgment-heavy; sonnet for implementers;
  haiku for mechanical), and a **restricted tool list** that keeps it in its lane.
- The **skills and/or MCP servers** that agent should use (from the Phase 2 inventory).
- The **project-specific guardrails** to bake into its prompt (pulled from Phase 1's working
  rules — e.g. docs-first, codegen steps, migration flow, i18n source-of-truth, security model).

The roster is not the entry point. Alongside the agents you will create a project `/intake` command
(Phase 5, step 2) that is the single doorway to the team, so present the roster as the set of lanes
that intake dispatches to — not as a menu I am expected to pick from by hand.

Then ask me to decide, using the interactive question tool (multi-select where natural), at least:
- **Which agents** to create (show the full proposed roster as selectable items).
- Whether to include an **orchestrator/tech-lead** agent (the `/intake` command is created
  either way — do not make it optional, but do confirm what it should dispatch to).
- **Model tier** strategy (per-agent recommended vs. a cheaper/uniform override).
- **Location**: project `.claude/agents/` (committed, shared) vs. user `~/.claude/agents/`.
- **Skill/MCP wiring** per agent (confirm or trim the proposed mappings).
- **Cleanup consent** (see Phase 4) — which unused skills / MCP servers / plugins to disable.
Make the questions concrete: list the actual detected items and your recommendation for each,
so I'm choosing from real options, not abstractions.

## Phase 4 — Context-bloat cleanup proposal
From the Phase 2 classification, propose disabling everything that's unused junk to reduce
context consumption, and explain the saving (fewer tool schemas / instructions loaded):
- **MCP servers**: which to disable and how (remove from `.mcp.json`, or set
  `disabledMcpjsonServers` / toggle in settings, or `claude mcp remove`).
- **Skills / plugins**: which to disable and how (plugin/marketplace management or the relevant
  settings keys).
Present this as an explicit opt-in list — never disable anything without my approval, and note
anything risky to remove. Prefer reversible disables over deletion.

## Phase 5 — Execute only what I approved

**Do not re-author the scaffolding.** This command ships working, tested copies at
`${CLAUDE_PLUGIN_ROOT}/templates/`. Copy them verbatim rather than writing them from memory:
- `templates/hooks/*.py` → `<project>/.claude/hooks/` (the routing hook, the read-only guard, the
  phase lock, the work log and its enforcement hooks, and the preflight check the installed
  `/intake` runs). These are project-agnostic - do not edit them.
- `templates/settings.hooks.json` → merge its `hooks` block into `<project>/.claude/settings.json`,
  preserving anything already there.
- `templates/intake.md` → `<project>/.claude/commands/intake.md`, replacing every `{{PLACEHOLDER}}`
  with what you learned in Phase 1. That tailoring is the real work; the rest is a copy.
Then pipe-test each hook in the target project before reporting it live - `python3` must exist, and
the paths must resolve.

1. Create each approved agent as `<location>/<name>.md` with valid YAML frontmatter
   (`name`, `description`, optional `tools`, optional `model`) followed by a system prompt that
   includes: the role, the shared project guardrails (a common block in every agent), the skills/
   MCP it should use, its workflow, and clear done-criteria. Reuse existing project conventions;
   don't invent commands that don't exist.
   The shared guardrail block must also carry the **librarian consultation contract**. teamme ships
   its own `history-librarian` agent — it is present in every project the plugin is installed in,
   not generated here and not part of this roster. Write into the block, in the team's own words:
   when a question is about what changed, when, or why — the history of a file, how a feature
   evolved, when a convention was introduced — or when a claim of the form "X was added in commit Y"
   is about to be made or repeated, **consult `history-librarian` and cite its answer** instead of
   re-deriving it from `git log`. Its answers carry commit SHAs; carry them through. State two
   things honestly in the same block: this is an instruction, not a gate — nothing blocks an agent
   that skips it — and the librarian answers from an index of commit history, so a question about
   the code's *current* state is not its to answer. Do not give team agents the librarian MCP tools;
   the librarian is the intended caller of those, and consulting the agent keeps every other tool
   list tight.
2. Create the project intake command at `<project>/.claude/commands/intake.md`. This is NOT
   optional and is created in every project. It is the **single entry point** for the team: all work
   requests go through `/intake <request>`. Tailor it to THIS project, and give it at minimum:
   - YAML frontmatter with a `description:` line and an `argument-hint:`; accept the request as
     `$ARGUMENTS`, and ask for it if empty.
   - **Preflight** — the block the template already carries: run `.claude/hooks/preflight.py check`
     before anything else and halt with a per-failure remediation if the install is not live. Leave
     it at the top; do not move it below grounding.
   - **Ground** — read the project's authoritative spec/docs FIRST and quote what already governs
     the request. Say plainly when it is already specified, already implemented, or contradicts a
     documented rule. Never design before reading.
   - **Classify** — map the request onto the real layer boundaries found in Phase 1 and name the
     owning agent for each part. Enforce the project's own "never do X / always do Y" rules here,
     and reshape or refuse a request that would violate one, explaining why.
   - **Brief** — produce a written intake brief: problem, observable outcome, layer decomposition
     with the owning agent per part, dependency-ordered plan, spec/doc delta, risks, done criteria,
     and how the work can actually be verified (name the project's real build/test commands, or
     state plainly that it cannot be verified locally and what the real gate is).
   - **Confirm** — use the interactive question tool for genuine forks in scope or approach, not
     for choices with an obvious default. Get approval of the brief before any code is written.
   - **Dispatch** — hand the approved brief to the orchestrator if one exists, otherwise to each
     owning specialist in dependency order. Never let a dependent lane start first.
   - **Report** — what changed per layer, what the docs now claim, and the honest verification
     state.
   Then record "all work requests enter through `/intake`" in the shared guardrail block, the team
   README and the project's CLAUDE.md, so that a request made directly to an agent is redirected
   into the intake flow rather than served ad hoc.
   Then make that routing automatic rather than merely documented: add a `UserPromptSubmit` hook in
   `<project>/.claude/settings.json` that injects a reminder to run the intake flow whenever a
   submitted prompt looks like a request for work — a `/plan` invocation included, and regardless of
   whether plan mode is currently active — while staying silent when the prompt is already `/intake`
   or a harness command. Put the logic in a small script under `<project>/.claude/hooks/` rather than
   an escaped one-liner inside JSON. The hook must only ADD context and never block a prompt, and
   must exit 0 on malformed or empty input so a broken hook can never stop me working. Do not assume
   an interpreter or CLI tool is installed — pipe-test the real command with a synthesized payload
   before writing it into settings (`jq` in particular is often absent) — and tell me if the hook
   needs `/hooks` or a restart to go live because `.claude/` held no settings file when the session
   started.
3. Give the team its own **native read-only discipline**, enforced by the harness rather than
   promised in a prompt. Do NOT use plan mode for this: plan mode's one-shot gate and its read-only
   inheritance would freeze the very subagents intake needs to dispatch. Instead:
   - A small state helper under `<project>/.claude/hooks/` records the intake phase in a gitignored
     state file: `begin` (grounding), `approve` (dispatch may write), `release` (clear), `status`.
   - A `PreToolUse` hook on `Edit|Write|NotebookEdit` reads that state and DENIES edits to project
     files while the phase is `grounding`, returning `permissionDecision: "deny"` with a reason that
     names the exact command to lift it. This is the one capability prompt text cannot provide:
     nothing can start implementing while the brief is still being written, including a dispatched
     agent.
   - It must **fail open in every other case** — approved, idle, missing, unreadable or malformed
     state, an unparseable payload, a path outside the project — and the state must expire on a
     timeout, so a crashed or abandoned session can never leave the repository write-locked. Edits
     under `.claude/` stay allowed so the flow can manage its own state.
   - Wire the phase transitions into the `/intake` command itself: `begin` as its first step,
     `approve` only after I approve the brief, `release` on abandonment and after the final report.
   - Pipe-test every phase before wiring it: idle allows, grounding denies, `.claude/` is exempt,
     an out-of-project path is exempt, approved allows, malformed input allows, stale state allows.
4. Make `/intake` the channel for **mid-flight** work too, not just new requests. A message that
   arrives while work is in progress is usually a refinement, an answer the team is blocked on, or a
   separate request - rarely a new brief. So the state must also carry the active request, the notes
   accrued against it, and a queue of requests parked behind it, with actions to `amend` (fold in),
   `reground` (re-plan an amendment that changes the plan, under the write lock), `queue`, `redirect`
   (supersede the active request, parking it at the FRONT of the queue with its notes so nothing is
   lost), `next` (resume a parked request *with* its notes), `drop <n>` and `clear`.
   **The intake flow decides which of these applies - never make me choose.** Give it an explicit
   triage rubric in step 0, tuned to this project:
   - **Fold in** when the message refines, constrains or corrects the active request, supplies a
     blocked input, or would otherwise mean editing the same files twice.
   - **Queue** when it is a separate goal with its own layers and no dependency on the active work.
   - **Redirect** only when it makes the active brief *wrong* rather than merely lower priority -
     and only after confirming with me, since it parks work in flight.
   Bias toward folding for anything inside the active scope and queueing for anything that widens
   it. Require the flow to state its decision and reason in one line before acting, and to leave the
   state untouched for a plain question. Make the `UserPromptSubmit` reminder phase-aware so a
   mid-flight message is pointed at this rubric rather than at starting a fresh brief.
5. Write a team **README** in the agents folder: the roster table, model/tool rationale, the
   `/intake` entry-point flow (and the orchestrator's place in it), and the shared guardrails.
6. Apply the approved environment cleanup, and report exactly what changed and the expected
   context reduction.
7. If a docs tree exists and the project requires it, document the new team there.
8. Verify: list the created files and confirm each frontmatter parses, including `intake.md`; if the
   project is a git repo, stage on a branch and show the diff (commit/push only if I ask). Re-run
   `python3 "${CLAUDE_PLUGIN_ROOT}/templates/hooks/preflight.py" check` — after a successful install
   it must exit 0, apart from the "not live until `/hooks` or a restart" case.

## Phase 6 — Hand over
This command ships as the **teamme** plugin, so it is already available in every project — there is
nothing to install per repo. Close out by telling me:
- Which files you created, and that `.claude/` is where Claude Code reads project agents and
  commands from, so it has to live inside the repo.
- **Whether I want it committed.** Do not assume. Agent-team scaffolding is often personal tooling
  that should stay out of a shared repo; `.git/info/exclude` keeps it local without the exclusion
  itself being committed, whereas `.gitignore` is a tracked file. Ask, and follow the answer — an
  approval to commit one thing never carries forward to later work.
- How to start: `/intake <what you want>`, and that everything else routes through it.
- Anything the hooks need that this machine lacks, and whether `/hooks` or a restart is required for
  them to go live. `/teamme:team-doctor` re-runs the preflight on demand if anything looks wrong
  later.

Do NOT write outside the current project in this phase.

Constraints: read-only until Phase 5; choose the minimum agents the project actually needs (no
filler roles); keep tool lists tight; `/intake` is always created, is always the only sanctioned way
to request work from the team, always carries a harness-enforced read-only grounding phase that
fails open, and always triages mid-flight messages itself rather than asking me to; mask any secrets
in output; follow the repo's existing commit/branch rules.
