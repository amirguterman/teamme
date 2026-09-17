#!/usr/bin/env python3
"""The agent team's work log: a durable, prioritized ledger of requested work.

Every request that enters through /intake becomes a task here and stays until it
is explicitly finished or dropped, so nothing is silently forgotten. Unlike the
transient phase lock in intake-state.py, this file is committed to the repo: it
survives a crashed session, a new clone and a context compaction.

Actions:
  add "<title>" [--priority P0|P1|P2] [--lane <agent>]   record new work (status: open)
  list [--all]                 prioritized listing; --all includes done and dropped
  next                         the highest-priority open task
  show <id>                    one task in full
  start <id>                   mark in progress
  block <id> "<reason>"        mark blocked, with what it is waiting on
  unblock <id>                 back to open
  done <id>                    finished
  defer <id> "<reason>"        deliberately not now - spec'd or parked, not forgotten
  decline <id> "<reason>"      judged that it should not be done, with the reason
  drop <id> ["<reason>"]       abandoned - recorded, not deleted
  note <id> "<text>"           append a refinement, input or decision
  priority <id> P0|P1|P2       re-prioritize
  lane <id> <agent>            assign the owning specialist
  stats                        counts by status, and whether anything is unfinished

Statuses: open, active, blocked (unfinished); deferred (intentionally not now);
done, declined, dropped (closed).
Priorities: P0 (now), P1 (normal, default), P2 (someday).
"""

import json
import os
import pathlib
import sys
from datetime import datetime, timezone

PRIORITIES = ("P0", "P1", "P2")
OPEN_STATUSES = ("open", "active", "blocked")
STATUS_MARK = {"open": "[ ]", "active": "[>]", "blocked": "[!]", "deferred": "[~]",
               "done": "[x]", "declined": "[/]", "dropped": "[-]"}


def log_path() -> pathlib.Path:
    root = pathlib.Path(os.environ.get("CLAUDE_PROJECT_DIR") or ".").resolve()
    return root / ".claude" / "intake" / "worklog.json"


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load() -> dict:
    try:
        d = json.loads(log_path().read_text())
        if isinstance(d, dict) and isinstance(d.get("tasks"), list):
            return d
    except Exception:
        pass
    return {"version": 1, "next_id": 1, "tasks": []}


def save(d: dict) -> None:
    p = log_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(d, indent=2) + "\n")


def find(d: dict, task_id: str):
    tid = task_id.upper()
    for t in d["tasks"]:
        if t["id"].upper() == tid:
            return t
    return None


def sort_key(t: dict):
    return (PRIORITIES.index(t.get("priority", "P1")) if t.get("priority") in PRIORITIES else 1,
            OPEN_STATUSES.index(t["status"]) if t["status"] in OPEN_STATUSES else 9,
            t.get("created", ""))


def render(t: dict) -> str:
    lane = f" @{t['lane']}" if t.get("lane") else ""
    extra = f" (blocked on: {t['blocked_on']})" if t.get("blocked_on") else ""
    notes = f" [{len(t['notes'])} note(s)]" if t.get("notes") else ""
    return f"{STATUS_MARK.get(t['status'], '[?]')} {t['id']} {t.get('priority', 'P1')}{lane} {t['title']}{extra}{notes}"


def unfinished(d: dict) -> list:
    return sorted([t for t in d["tasks"] if t["status"] in OPEN_STATUSES], key=sort_key)


def deferred(d: dict) -> list:
    """Deliberately parked - not nagged about, but never silently forgotten."""
    return sorted([t for t in d["tasks"] if t["status"] == "deferred"], key=sort_key)


def flag(name: str, default=None):
    if name in sys.argv:
        i = sys.argv.index(name)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return default


def main() -> int:
    args = sys.argv[1:]
    skip = set()
    for f in ("--priority", "--lane"):
        if f in args:
            i = args.index(f)
            skip.update({i, i + 1})
    args = [a for i, a in enumerate(args) if i not in skip and a != "--all"]
    action = args[0] if args else "list"
    d = load()

    if action == "add":
        title = " ".join(args[1:]).strip()
        if not title:
            print("worklog: add needs a title.", file=sys.stderr)
            return 2
        prio = (flag("--priority") or "P1").upper()
        if prio not in PRIORITIES:
            print(f"worklog: priority must be one of {', '.join(PRIORITIES)}.", file=sys.stderr)
            return 2
        tid = f"T{d['next_id']}"
        d["next_id"] += 1
        d["tasks"].append({"id": tid, "title": title, "status": "open", "priority": prio,
                           "lane": flag("--lane") or "", "notes": [], "blocked_on": "",
                           "created": now(), "updated": now()})
        save(d)
        print(f"worklog: {tid} added ({prio}) - {title}")

    elif action == "list":
        show_all = "--all" in sys.argv
        tasks = sorted(d["tasks"] if show_all else unfinished(d) + deferred(d), key=sort_key)
        if not tasks:
            print("worklog: nothing open" if not show_all else "worklog: empty")
            return 0
        for t in tasks:
            print(render(t))

    elif action == "next":
        tasks = [t for t in unfinished(d) if t["status"] != "blocked"]
        print(render(tasks[0]) if tasks else "worklog: nothing open")

    elif action == "stats":
        counts = {}
        for t in d["tasks"]:
            counts[t["status"]] = counts.get(t["status"], 0) + 1
        print(", ".join(f"{k}: {v}" for k, v in sorted(counts.items())) or "empty")
        print(f"unfinished: {len(unfinished(d))}, deferred: {len(deferred(d))}")

    elif action in ("show", "start", "block", "unblock", "done", "defer", "decline", "drop",
                    "note", "priority", "lane"):
        if len(args) < 2:
            print(f"worklog: {action} needs a task id.", file=sys.stderr)
            return 2
        t = find(d, args[1])
        if t is None:
            print(f"worklog: no task {args[1]}.", file=sys.stderr)
            return 3
        rest = " ".join(args[2:]).strip()

        if action == "show":
            print(json.dumps(t, indent=2))
            return 0
        elif action == "start":
            t["status"] = "active"
            t["blocked_on"] = ""
        elif action == "block":
            if not rest:
                print("worklog: block needs a reason.", file=sys.stderr)
                return 2
            t["status"] = "blocked"
            t["blocked_on"] = rest
        elif action == "unblock":
            t["status"] = "open"
            t["blocked_on"] = ""
        elif action == "done":
            t["status"] = "done"
            t["blocked_on"] = ""
        elif action in ("defer", "decline"):
            if not rest:
                print(f"worklog: {action} needs a reason.", file=sys.stderr)
                return 2
            t["status"] = "deferred" if action == "defer" else "declined"
            t["blocked_on"] = ""
            t["notes"].append(f"{t['status']}: {rest}")
        elif action == "drop":
            t["status"] = "dropped"
            if rest:
                t["notes"].append(f"dropped: {rest}")
        elif action == "note":
            if not rest:
                print("worklog: note needs text.", file=sys.stderr)
                return 2
            t["notes"].append(rest)
        elif action == "priority":
            p = rest.upper()
            if p not in PRIORITIES:
                print(f"worklog: priority must be one of {', '.join(PRIORITIES)}.", file=sys.stderr)
                return 2
            t["priority"] = p
        elif action == "lane":
            t["lane"] = rest

        t["updated"] = now()
        t.pop("nagged_at", None)  # a status change re-arms the Stop reminder
        save(d)
        print(f"worklog: {render(t)}")

    else:
        print(f"unknown action: {action}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
