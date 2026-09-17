#!/usr/bin/env python3
"""Work-log enforcement hooks.

  worklog-enforce.py session   SessionStart - surface unfinished work so a new
                               session, or one resumed after a compaction, never
                               loses track of what was already asked for.

  worklog-enforce.py stop      Stop - refuse to end the turn while a task is still
                               marked `active`, so status gets recorded rather than
                               drifting. Fires AT MOST ONCE per status change: the
                               reminder is stamped against the task's `updated`
                               time, so answering it (by marking done, blocked, or
                               even just re-touching the task) re-arms it and a
                               turn can always finish. It can never loop.

Both fail silent on any error - a broken ledger must never block work.
"""

import importlib.util
import json
import pathlib
import sys


def worklog():
    path = pathlib.Path(__file__).resolve().parent / "worklog.py"
    spec = importlib.util.spec_from_file_location("worklog_mod", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("worklog.py not loadable")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def session(wl) -> None:
    d = wl.load()
    pending = wl.unfinished(d)
    parked = wl.deferred(d)
    if not pending and not parked:
        return
    blocked = [t for t in pending if t["status"] == "blocked"]
    if pending:
        head = (
            f"Unfinished work in the work log ({len(pending)} task(s)), highest priority first:\n"
            + "\n".join(wl.render(t) for t in pending)
        )
        if blocked:
            head += (
                f"\n{len(blocked)} of these are blocked and need an input or a decision before "
                f"they can move."
            )
    else:
        head = "Nothing is open in the work log."
    if parked:
        head += "\nDeliberately deferred (do not start without being asked): " + ", ".join(
            f"{t['id']} {t['title']}" for t in parked
        )
    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": (
                    f"{head}\nThis carries over from earlier sessions - do not drop it. Use "
                    f"`python3 .claude/hooks/worklog.py` to inspect or update status, and /intake "
                    f"to start or refine any of it."
                ),
            }
        },
        sys.stdout,
    )


def stop(wl) -> None:
    d = wl.load()
    active = [t for t in d["tasks"] if t["status"] == "active"]
    # Only nag about a task once per status change, so the turn can always end.
    fresh = [t for t in active if t.get("nagged_at") != t.get("updated")]
    if not fresh:
        return
    for t in fresh:
        t["nagged_at"] = t.get("updated")
    wl.save(d)
    names = ", ".join(f"{t['id']} ({t['title']})" for t in fresh)
    json.dump(
        {
            "decision": "block",
            "reason": (
                f"Still marked active in the work log: {names}. Before finishing, record where it "
                f"actually stands - `worklog.py done <id>` if it is finished, "
                f"`worklog.py block <id> \"<what it is waiting on>\"` if it is stuck, or "
                f"`worklog.py note <id> \"<progress>\"` and keep going if there is more to do. "
                f"If the work really is complete, mark it done and finish."
            ),
        },
        sys.stdout,
    )


def main() -> None:
    try:
        sys.stdin.read()  # drain the payload; neither mode needs it
    except Exception:
        pass
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        wl = worklog()
        if mode == "session":
            session(wl)
        elif mode == "stop":
            stop(wl)
    except Exception:
        return


if __name__ == "__main__":
    main()
