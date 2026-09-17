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
  dispatch <id> ["<to whom>"]  handed to a specialist - waiting on that agent, not on you
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

Statuses: open, active, dispatched, blocked (unfinished); deferred (intentionally
not now); done, declined, dropped (closed). `dispatched` means the work is in
flight with another agent: it is unfinished, but nothing this session does can
advance it, so the Stop reminder leaves it alone.
Priorities: P0 (now), P1 (normal, default), P2 (someday).

Two timestamps, deliberately: `updated` moves on any change (an audit stamp),
`status_changed` moves only on a real status transition. worklog-enforce.py arms
its Stop reminder against `status_changed`, so recording progress with a note
does not re-arm the nag it was answering.

Concurrent writers take a best-effort lockfile around load -> mutate -> save, so
parallel agents cannot drop each other's notes. It fails open in both
directions: a stale lock from a crashed process expires, and a lock that cannot
be taken within the bound is abandoned and the write proceeds anyway. A ledger
that refuses to record is worse than a rare lost note.
"""

import json
import os
import pathlib
import sys
import time
from datetime import datetime, timezone

PRIORITIES = ("P0", "P1", "P2")
OPEN_STATUSES = ("open", "active", "dispatched", "blocked")
STATUS_MARK = {"open": "[ ]", "active": "[>]", "dispatched": "[@]", "blocked": "[!]",
               "deferred": "[~]", "done": "[x]", "declined": "[/]", "dropped": "[-]"}

# Actions that are a genuine status transition, and so re-arm the Stop reminder.
STATUS_ACTIONS = ("start", "dispatch", "block", "unblock", "done", "defer", "decline", "drop")
# Actions that read-modify-write the ledger, and so run under the lock.
MUTATING = STATUS_ACTIONS + ("add", "note", "priority", "lane")

LOCK_STALE_SECONDS = 30     # a lock older than this is assumed to be a crashed process
LOCK_TRIES = 40             # bounded retries...
LOCK_WAIT = 0.05            # ...roughly two seconds, then proceed unlocked


def log_path() -> pathlib.Path:
    root = pathlib.Path(os.environ.get("CLAUDE_PROJECT_DIR") or ".").resolve()
    return root / ".claude" / "intake" / "worklog.json"


def lock_path() -> pathlib.Path:
    return log_path().with_name("worklog.lock")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class lock:
    """Best-effort mutual exclusion around a read-modify-write of the ledger.

    O_CREAT | O_EXCL rather than fcntl, which is POSIX-only; this has to work
    anywhere python3 does. Never raises and never refuses the caller: if the lock
    is unavailable for LOCK_TRIES * LOCK_WAIT seconds it is abandoned and the
    caller writes anyway, and a lockfile left behind by a crashed process expires
    after LOCK_STALE_SECONDS.
    """

    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self.held = False

    def __enter__(self):
        if not self.enabled:
            return self
        p = lock_path()
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            return self
        for _ in range(LOCK_TRIES):
            try:
                fd = os.open(str(p), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
                try:
                    os.write(fd, f"{os.getpid()} {time.time()}\n".encode())
                finally:
                    os.close(fd)
                self.held = True
                return self
            except FileExistsError:
                pass
            except Exception:
                return self  # unwritable directory and the like - proceed unlocked
            try:  # break a lock left behind by a process that died holding it
                if time.time() - p.stat().st_mtime > LOCK_STALE_SECONDS:
                    p.unlink()
                    continue
            except Exception:
                pass
            time.sleep(LOCK_WAIT)
        return self

    def __exit__(self, *exc):
        if self.held:
            try:
                lock_path().unlink()
            except Exception:
                pass
            self.held = False
        return False


def load() -> dict:
    """The ledger, or an empty one. A corrupt file reads as empty, never crashes."""
    try:
        d = json.loads(log_path().read_text())
        if isinstance(d, dict) and isinstance(d.get("tasks"), list):
            for t in d["tasks"]:
                # Ledgers written before status_changed existed: default it to the
                # task's own `updated`, so an old file behaves exactly as it did.
                if isinstance(t, dict):
                    t.setdefault("status_changed", t.get("updated") or t.get("created") or "")
            return d
    except Exception:
        pass
    return {"version": 1, "next_id": 1, "tasks": []}


def save(d: dict) -> None:
    """Write via a temp file in the same directory + os.replace, so a reader never
    sees a half-written ledger even if this process dies mid-write."""
    p = log_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(d, indent=2) + "\n"
    tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(text)
        os.replace(str(tmp), str(p))
        return
    except Exception:
        try:
            tmp.unlink()
        except Exception:
            pass
    p.write_text(text)  # last resort: never lose the record over an atomicity nicety


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
    if t.get("blocked_on"):
        extra = f" (blocked on: {t['blocked_on']})"
    elif t.get("status") == "dispatched":
        to = t.get("dispatched_to")
        extra = f" (dispatched to: {to})" if to else " (dispatched)"
    else:
        extra = ""
    notes = f" [{len(t['notes'])} note(s)]" if t.get("notes") else ""
    return f"{STATUS_MARK.get(t['status'], '[?]')} {t['id']} {t.get('priority', 'P1')}{lane} {t['title']}{extra}{notes}"


def unfinished(d: dict) -> list:
    return sorted([t for t in d["tasks"] if t["status"] in OPEN_STATUSES], key=sort_key)


def dispatched(d: dict) -> list:
    """In flight with another agent - unfinished, but not this session's move."""
    return sorted([t for t in d["tasks"] if t["status"] == "dispatched"], key=sort_key)


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

    # Everything that mutates loads, changes and saves inside the lock, so two
    # agents noting progress at the same moment cannot overwrite one another.
    with lock(action in MUTATING):
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
            stamp = now()
            d["tasks"].append({"id": tid, "title": title, "status": "open", "priority": prio,
                               "lane": flag("--lane") or "", "notes": [], "blocked_on": "",
                               "dispatched_to": "", "created": stamp, "updated": stamp,
                               "status_changed": stamp})
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
            # Blocked needs someone else's input; dispatched is already in flight.
            tasks = [t for t in unfinished(d) if t["status"] not in ("blocked", "dispatched")]
            print(render(tasks[0]) if tasks else "worklog: nothing open")

        elif action == "stats":
            counts = {}
            for t in d["tasks"]:
                counts[t["status"]] = counts.get(t["status"], 0) + 1
            print(", ".join(f"{k}: {v}" for k, v in sorted(counts.items())) or "empty")
            print(f"unfinished: {len(unfinished(d))}, deferred: {len(deferred(d))}")

        elif action in ("show", "start", "dispatch", "block", "unblock", "done", "defer",
                        "decline", "drop", "note", "priority", "lane"):
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
            elif action == "dispatch":
                t["status"] = "dispatched"
                t["dispatched_to"] = rest or t.get("lane") or ""
            elif action == "block":
                if not rest:
                    print("worklog: block needs a reason.", file=sys.stderr)
                    return 2
                t["status"] = "blocked"
                t["blocked_on"] = rest
            elif action == "unblock":
                t["status"] = "open"
            elif action == "done":
                t["status"] = "done"
            elif action in ("defer", "decline"):
                if not rest:
                    print(f"worklog: {action} needs a reason.", file=sys.stderr)
                    return 2
                t["status"] = "deferred" if action == "defer" else "declined"
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
            if action in STATUS_ACTIONS:
                # Leaving a status clears whatever the old one was waiting on.
                if action != "block":
                    t["blocked_on"] = ""
                if action != "dispatch":
                    t["dispatched_to"] = ""
                t["status_changed"] = t["updated"]
                t.pop("nagged_at", None)  # only a real status change re-arms the Stop reminder
            save(d)
            print(f"worklog: {render(t)}")

        else:
            print(f"unknown action: {action}", file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
