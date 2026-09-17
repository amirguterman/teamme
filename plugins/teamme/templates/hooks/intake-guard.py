#!/usr/bin/env python3
"""PreToolUse hook: enforce the intake read-only discipline.

While /intake is grounding a request and writing its brief, no project file may
be edited - not by the main session and not by a dispatched agent. This is
enforced by the harness rather than promised in a prompt, which is the whole
point: instructions alone do not stop a well-meaning agent from starting early.

Denies only while the phase is `grounding`. Every other phase - approved, idle,
missing, expired, unparseable - allows the write, so a broken or abandoned state
file can never lock the repository. Edits under .claude/ are always permitted so
the flow can manage its own state and configuration.
"""

import json
import os
import pathlib
import sys

DENY_REASON = (
    "Blocked by the intake read-only discipline: /intake is still grounding this request and "
    "writing its brief, so project files cannot be edited yet. Finish the brief, get it approved, "
    "then run `python3 .claude/hooks/intake-state.py approve` before dispatching. To abandon the "
    "intake instead, run `python3 .claude/hooks/intake-state.py release`."
)


def load_read_state():
    """Import read_state from the sibling module, whose filename uses a dash."""
    import importlib.util

    path = pathlib.Path(__file__).resolve().parent / "intake-state.py"
    spec = importlib.util.spec_from_file_location("intake_state_mod", path)
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.read_state


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return
    if not isinstance(payload, dict):
        return

    target = (payload.get("tool_input") or {}).get("file_path")
    if not isinstance(target, str) or not target:
        return

    root = pathlib.Path(os.environ.get("CLAUDE_PROJECT_DIR") or ".").resolve()
    try:
        resolved = pathlib.Path(target).resolve()
        rel = resolved.relative_to(root)
    except Exception:
        return  # outside the project (scratchpad, /tmp) - not our business
    if rel.parts and rel.parts[0] == ".claude":
        return  # the flow must be able to manage its own state

    try:
        getter = load_read_state()
        phase = getter().get("phase") if getter else None
    except Exception:
        return  # fail open: never let a broken guard block work
    if phase != "grounding":
        return

    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": DENY_REASON,
            }
        },
        sys.stdout,
    )


if __name__ == "__main__":
    main()
