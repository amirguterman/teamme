#!/usr/bin/env python3
"""Intake phase lock - the read-only discipline the agent team enforces on itself.

This file holds only the transient phase of the request currently being handled,
plus a pointer to its task in the work log. Tasks, priorities, notes and the
queue live in worklog.py, which is durable and committed; this state is
gitignored and expires, because it is a lock, not a record.

`intake-guard.py` reads the phase on every write and denies project edits while
it is `grounding`, so nothing can start implementing while a brief is still
being written.

Actions:
  begin <task-id>   enter the read-only grounding phase for a work-log task
  approve           brief approved - dispatch may write
  reground          send an approved request back to grounding, so an amendment
                    that changes the plan is re-planned under the write lock
  release           finish the active request (the task's own status lives in the
                    work log - mark it there too)
  status            print the current phase
  show              print the full state as JSON, for the /intake flow to branch on
  clear             reset the lock

The phase expires after STALE_SECONDS so an abandoned session can never leave the
repository write-locked.
"""

import json
import os
import pathlib
import sys
import time

STALE_SECONDS = 3600
ACTIVE = ("grounding", "approved")


def state_path() -> pathlib.Path:
    root = pathlib.Path(os.environ.get("CLAUDE_PROJECT_DIR") or ".").resolve()
    return root / ".claude" / "intake" / "state.json"


def _blank() -> dict:
    return {"phase": "idle", "task": "", "at": time.time()}


def read_state() -> dict:
    """Current phase. A missing, unreadable or expired file reads as idle, so the
    guard fails open and a crashed session cannot lock the repository."""
    try:
        data = json.loads(state_path().read_text())
    except Exception:
        return _blank()
    if not isinstance(data, dict):
        return _blank()
    data.setdefault("phase", "idle")
    data.setdefault("task", "")
    if time.time() - float(data.get("at") or 0) > STALE_SECONDS:
        data["phase"] = "idle"
        data["expired"] = True
    return data


def write_state(data: dict) -> None:
    p = state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    data["at"] = time.time()
    p.write_text(json.dumps(data, indent=2) + "\n")


def main() -> int:
    argv = [a for a in sys.argv[1:] if a != "--force"]
    force = "--force" in sys.argv
    action = argv[0] if argv else "status"
    arg = " ".join(argv[1:]).strip()
    s = read_state()

    if action == "begin":
        if s["phase"] in ACTIVE and not force:
            print(
                f"intake: task {s['task']} is already {s['phase']}. Fold this into it "
                f"(`worklog.py note {s['task']} ...`), queue it (`worklog.py add ...`), or release "
                f"the current one first.",
                file=sys.stderr,
            )
            return 3
        if not arg:
            print("intake: begin needs the work-log task id.", file=sys.stderr)
            return 2
        write_state({"phase": "grounding", "task": arg.upper()})
        print(f"intake: grounding {arg.upper()} - project writes are denied until the brief is approved")

    elif action == "reground":
        if s["phase"] != "approved":
            print(f"intake: cannot reground from phase {s['phase']}.", file=sys.stderr)
            return 3
        s["phase"] = "grounding"
        write_state(s)
        print(f"intake: {s['task']} back to grounding - writes denied again until re-approved")

    elif action == "approve":
        if s["phase"] != "grounding":
            print(f"intake: nothing is grounding (phase {s['phase']}).", file=sys.stderr)
            return 3
        s["phase"] = "approved"
        write_state(s)
        print(f"intake: {s['task']} approved - dispatch may write")

    elif action in ("release", "clear"):
        prev = s.get("task", "")
        write_state(_blank())
        hint = f" (mark {prev} in the work log if you have not)" if prev and action == "release" else ""
        print(f"intake: {action}d{hint}")

    elif action == "status":
        print(s.get("phase", "idle"))

    elif action == "show":
        json.dump(s, sys.stdout, indent=2)
        print()

    else:
        print(f"unknown action: {action}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
