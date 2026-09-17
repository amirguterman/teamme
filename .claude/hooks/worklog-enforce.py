#!/usr/bin/env python3
"""Work-log enforcement hooks.

  worklog-enforce.py session   SessionStart - surface unfinished work so a new
                               session, or one resumed after a compaction, never
                               loses track of what was already asked for.

  worklog-enforce.py stop      Stop - refuse to end the turn while a task is still
                               marked `active`, so status gets recorded rather than
                               drifting. Fires AT MOST ONCE per status change: the
                               reminder is stamped against the task's
                               `status_changed` time, so only a real status
                               transition (done, blocked, dispatched, ...) re-arms
                               it and a turn can always finish. It can never loop.
                               Adding a note does NOT re-arm it - noting progress is
                               one of the remedies it suggests, so re-arming on a
                               note would guarantee it interrupted again next turn.
                               `dispatched` tasks are never nagged about: nothing
                               the model does this turn can advance work that is in
                               flight with another agent.

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
    waiting = [t for t in pending if t["status"] == "dispatched"]
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
        if waiting:
            head += (
                f"\n{len(waiting)} of these are dispatched: in flight with another agent, waiting "
                f"on that agent's result rather than on you. Check the outcome before re-dispatching."
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
    # Read-modify-write under the ledger's own lock: a parallel agent writing a
    # note at the same moment must not lose it to the nag stamp, or vice versa.
    with wl.lock():
        d = wl.load()
        # `dispatched` is deliberately not nagged about - it is unfinished, but it
        # is waiting on another agent, so no action here could resolve it.
        active = [t for t in d["tasks"] if t["status"] == "active"]
        # Only nag once per status change, so the turn can always end. Stamped
        # against status_changed, not updated: a note answers the nag without
        # re-arming it, while start/block/done/dispatch re-arm it.
        fresh = [t for t in active if t.get("nagged_at") != t.get("status_changed")]
        if not fresh:
            return
        for t in fresh:
            t["nagged_at"] = t.get("status_changed")
        wl.save(d)
    names = ", ".join(f"{t['id']} ({t['title']})" for t in fresh)
    json.dump(
        {
            "decision": "block",
            "reason": (
                f"Still marked active in the work log: {names}. Before finishing, record where it "
                f"actually stands - `worklog.py done <id>` if it is finished, "
                f"`worklog.py block <id> \"<what it is waiting on>\"` if it is stuck on an input "
                f"or a decision, `worklog.py dispatch <id> \"<agent>\"` if it is now in flight with "
                f"a specialist, or `worklog.py note <id> \"<progress>\"` and keep going if there is "
                f"more to do. If the work really is complete, mark it done and finish."
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
