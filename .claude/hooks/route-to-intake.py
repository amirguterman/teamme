#!/usr/bin/env python3
"""UserPromptSubmit hook: route work requests into the /intake flow.

This project requires every request for work to enter through `/intake`
(see .claude/commands/intake.md). This hook reads the submitted prompt and, for
anything that is not already an intake run or a harness command, injects a
reminder that the intake flow is the only sanctioned entry point.

The reminder is phase-aware: with an intake already in flight, a message is far
more likely to be a refinement, an answer the team is blocked on, or a separate
request to queue than it is a brand-new brief - so the guidance names those
routes instead of telling the model to start over.

It never blocks a prompt - it only adds context, so a plain question still gets
a plain answer. Any unexpected input is treated as "say nothing" so a broken
hook can never stop the user from working.
"""

import importlib.util
import json
import pathlib
import re
import sys

# Prompts that must pass through untouched: an intake run already in progress,
# teamme's own commands, and harness/built-in commands that do not request project work.
# A plugin command arrives prefixed - `/teamme:init-team`, not `/init-team` - so the prefix is
# matched generically rather than by plugin name, which teamme must never assume.
PASSTHROUGH = re.compile(
    r"^\s*/(?:[A-Za-z0-9_.-]+:)?"
    r"(intake|init-team|team-doctor"
    r"|clear|help|config|agents|hooks|mcp|model|cost|status|resume"
    r"|compact|rewind|context|doctor|login|logout|exit|quit|export|memory"
    r"|code-review|security-review|simplify|init)\b",
    re.IGNORECASE,
)

IDLE_GUIDANCE = (
    "This project routes ALL work through the /intake flow. If this message asks for code "
    "to be written, changed, debugged, or planned - including a /plan invocation, and whether or "
    "not plan mode is active - do NOT start work or draft a plan directly. Run the intake flow "
    "defined in .claude/commands/intake.md for this request: ground it in the project documents "
    "that command names, classify it across the boundaries it lists, write the intake brief, "
    "confirm scope, then dispatch to the agent team. If the message is only a question or a "
    "lookup that changes nothing, answer it normally."
)

ACTIVE_GUIDANCE = (
    "An intake is already in flight: '{request}' (phase: {phase}). Do NOT start a second brief and "
    "do NOT ask the user how to handle this - YOU decide, using the triage table in step 0 of "
    ".claude/commands/intake.md: fold it into the current work (`worklog.py note`, plus "
    "`intake-state.py reground` first if it changes the plan), queue it as its own task "
    "(`worklog.py add`), or - only if it makes the active brief wrong rather than merely lower "
    "priority, and only after confirming - park the active task and redirect. State the decision "
    "and the reason in one line, act, then resume. A question that changes no code leaves the "
    "state untouched."
)


def _load(filename: str, modname: str):
    path = pathlib.Path(__file__).resolve().parent / filename
    spec = importlib.util.spec_from_file_location(modname, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def current_state() -> dict:
    """Best-effort read of the intake phase; any failure reads as idle."""
    try:
        return _load("intake-state.py", "intake_state_mod").read_state()
    except Exception:
        return {}


def task_label(task_id: str) -> str:
    """Resolve a work-log task id to '<id> <title>' when the ledger is readable."""
    if not task_id:
        return "(no task)"
    try:
        wl = _load("worklog.py", "worklog_mod")
        t = wl.find(wl.load(), task_id)
        return f"{task_id} {t['title']}" if t else task_id
    except Exception:
        return task_id


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return
    if not isinstance(payload, dict):
        return

    prompt = payload.get("prompt") or payload.get("user_prompt") or ""
    if not isinstance(prompt, str) or PASSTHROUGH.match(prompt):
        return

    state = current_state()
    phase = state.get("phase", "idle")
    if phase in ("grounding", "approved"):
        guidance = ACTIVE_GUIDANCE.format(
            request=task_label(state.get("task", "")), phase=phase
        )
    else:
        guidance = IDLE_GUIDANCE

    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": guidance,
            }
        },
        sys.stdout,
    )


if __name__ == "__main__":
    main()
