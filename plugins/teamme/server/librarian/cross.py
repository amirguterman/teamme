#!/usr/bin/env python3
"""The cross-index spine: "what happened around this file / commit / task / time".

teamme already keeps three records of the same work, and each one holds a
different third of any answer:

  history   .claude/librarians/index.db           commits and the files they
                                                  changed, from git
  sessions  .claude/librarians/sessions/index.db  turns and mark points, from
                                                  the transcripts
  work log  .claude/intake/worklog.json           tasks, statuses, lanes, notes

None of them can answer "what happened around X" alone. This module is the
join, and it is deliberately the thinnest possible one: it LOCATES rows in all
three and interleaves them by time, carrying each row's own address so the
caller can go deeper with the existing `commit_detail` and `window` queries. It
is a SPINE, not a dump - it points, it does not paste.

WHY TIME AND FILE PATHS, AND NOT IDS
------------------------------------
The obvious join is by citation: a task id in a commit message, a commit hash
in a work-log note. That join was measured on this repository before this module
was written, and it does not exist in the data - a handful of commit messages
mention a task id, a handful of notes mention a short hash, both incidentally
rather than by convention. A query keyed on citation would return almost nothing
while LOOKING like "nothing happened", which is the worst answer available here:
an empty result that reads as an absence of work rather than as an absence of a
recorded link.

What is reliable is what a machine wrote on both sides:

  TIME        every store has it - the work log's created/updated/
              status_changed, the commit date, the turn timestamp - and it is
              already there, retroactively, for everything collected so far.
  FILE PATHS  `files_changed` comes from git's own --numstat; a session `file`
              mark comes from an observed tool call. Neither is prose.

So every association here is INFERENCE FROM OVERLAP IN TIME, and it is labelled
as such in every row and every caveat. "Active while" and "around", never
"implements" or "caused" - the same discipline the co-change queries follow when
they say "changes with" and never "depends on". One exception is exact rather
than inferred and says so: a work-log row matched because a note NAMES a path is
a literal substring match on prose, which under-reports (nothing forces a note
to name a file) but never invents a link.

WHY THE WORK LOG IS NOT INDEXED
-------------------------------
It is the source of truth, it is small - tens of tasks - and a copy of it in a
second store is precisely the failure this project has paid for repeatedly: a
hook list that grew, a path captured at install time, a version literal. So it
is read live, from disk, on every query, and joined in memory. If that ever
becomes too slow the fix is a cache keyed on the file's mtime, NOT a second
copy; at the scale this file is designed for (tens of tasks, a few hundred
notes) the read is not measurable next to opening two SQLite files.

WHY NOT `ATTACH DATABASE`
-------------------------
ATTACH is stdlib and would let one statement span the two SQLite indexes. It is
not used, for two reasons. The indexes are refreshed independently and either
may legitimately be absent or empty, so a single statement across both turns one
missing index into a failed query instead of a partial answer that names the
empty store - and naming the empty store is the whole point of the `stores`
block below. And the third store is not SQL at all, so an in-memory merge on
epoch has to exist regardless; doing two thirds of it in SQL and one third in
Python would be two mechanisms where one does.

WHICH LIBRARIAN THESE QUERIES BELONG TO
---------------------------------------
Neither, and that is deliberate. Each store is gated on its own: a disabled
history librarian removes the commit rows, a disabled session librarian removes
the session rows, and the result SAYS which store contributed nothing and why.
A partial answer that names its own gaps beats a refusal. The one exception is
an anchor that cannot be resolved without a store - `around_commit` cannot find
its commit with the history index off or absent - which is an error naming the
fix, not a silently thin answer.

Stdlib only. Nothing here raises at the boundary: callers get a result dict.
"""

import json
import pathlib
import re
from datetime import datetime, timezone

try:
    from . import store, sessions, config, transcripts
except ImportError:  # loaded as a loose module rather than a package member
    import store              # type: ignore
    import sessions           # type: ignore
    import config             # type: ignore
    import transcripts        # type: ignore


QUERY_NAMES = ("around_path", "around_commit", "around_task", "timeline")

# The row cap is PER STORE, not per answer. A shared cap would let the busiest
# store crowd the others out of the result - this repository has 18 commits and
# tens of thousands of turns, so a global cap of 12 would be 12 turns and no
# commits, which is exactly the "thin answer that looks complete" this module
# exists to avoid.
DEFAULT_LIMIT = 12
MAX_LIMIT = 50

# How far either side of an instant counts as "around" it, in minutes.
DEFAULT_MINUTES = 120
MAX_MINUTES = 60 * 24 * 30

# How far a task's inferred active window is widened, in minutes. See
# around_task: a status transition is stamped when the agent records it, which
# is not the instant the commit lands, and an exact window misses the boundary
# by seconds. Always reported, never silent, and `pad_minutes: 0` turns it off.
DEFAULT_PAD_MINUTES = 15

MAX_LABEL_CHARS = 160
MAX_NOTE_CHARS = 240
MAX_TASK_NOTES = 12
MAX_TASK_IDS_LISTED = 40

# Which mark kinds a path match looks at. `file` is a tool that WROTE a file and
# `tool` carries the target of everything else (a command, a pattern, a path
# that was read), so both can name a path. The other kinds carry prose.
PATH_MARK_KINDS = ("file", "tool")

# The spine of a conversation, for the window queries: what a person typed, what
# a compaction dropped, and where files were written. Not every turn - that is
# what `window` is for, one region at a time.
SPINE_MARK_KINDS = ("prompt", "recap", "compaction", "file")

OPEN_STATUSES = ("open", "active", "dispatched", "blocked")

CORRELATION_CAVEAT = (
    "every link between stores here is TIME OVERLAP, not a recorded relationship. A commit that "
    "landed while a task was open is reported as 'active while' - it is not evidence that the "
    "commit implements the task. Nothing in these three stores records that link today."
)

SPINE_CAVEAT = (
    "these are POSITIONS, not content. Each row carries its own address - a commit hash, a "
    "session id and seq, a task id - so the next question can fetch the detail with "
    "`commit_detail` or `window`."
)

WORKLOG_PROSE_CAVEAT = (
    "work-log rows matched by NAME are a literal substring match on the task's title and notes - "
    "prose, unlike the commit and session rows, which come from git's own --numstat and from "
    "observed tool calls. Nothing requires a note to name a file, so this match UNDER-reports: "
    "an absent row is not evidence the task did not touch the path."
)


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #

def _clamp(value, default: int = DEFAULT_LIMIT, cap: int = MAX_LIMIT) -> int:
    try:
        n = int(value)
    except Exception:
        return default
    if n <= 0:
        return default
    return min(n, cap)


def _minutes(value) -> int:
    if value is None or isinstance(value, bool):
        return DEFAULT_MINUTES
    try:
        n = int(value)
    except Exception:
        return DEFAULT_MINUTES
    if n <= 0:
        return DEFAULT_MINUTES
    return min(n, MAX_MINUTES)


def _now_epoch() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def _iso(epoch) -> str:
    try:
        return datetime.fromtimestamp(int(epoch), timezone.utc).isoformat(timespec="seconds")
    except Exception:
        return ""


def _one_line(text, limit: int = MAX_LABEL_CHARS) -> str:
    return transcripts._clip(" ".join(str(text or "").split()), limit)


def _sort_rows(rows: list) -> list:
    """Newest first. A row with no readable timestamp sorts last rather than
    being dropped - it is still a real row, it just cannot be placed in time."""
    return sorted(rows, key=lambda r: (r.get("epoch") is not None, r.get("epoch") or 0,
                                       r.get("store") or ""), reverse=True)


def _state(name: str, state: str, detail=None, rows: int = 0, truncated: bool = False) -> dict:
    return {"store": name, "state": state, "detail": detail,
            "rows": rows, "truncated": bool(truncated)}


# --------------------------------------------------------------------------- #
# opening the two indexes - each one's absence is a RESULT, never an exception
# --------------------------------------------------------------------------- #

def _is_enabled(project_dir, name: str, enabled) -> bool:
    """Is librarian `name` switched on here? Unreadable config reads as ON, the
    same fail-open rule config.load() follows."""
    if isinstance(enabled, dict) and name in enabled:
        return bool(enabled[name])
    try:
        return bool(config.enabled(project_dir, name, ("history", "sessions")))
    except Exception:
        return True


def _open_history(project_dir, enabled=None):
    """(conn, state). `conn` is None whenever the index cannot be read."""
    if not _is_enabled(project_dir, "history", enabled):
        return None, _state("history", "disabled", (
            "the history librarian is switched off in this project, so its index was not read. "
            'Re-enable it with teamme_librarian_configure {"librarian": "history", '
            '"enabled": true}.'))
    path = store.db_path(project_dir)
    if not path.exists():
        return None, _state("history", "absent", (
            f"{path} does not exist yet - nothing from git has been indexed here. Run "
            f"teamme_librarian_refresh."))
    try:
        conn, note = store.connect_or_reset(project_dir)
    except Exception as exc:
        return None, _state("history", "error", f"the history index could not be opened: {exc}")
    if note:
        try:
            conn.close()
        except Exception:
            pass
        return None, _state("history", "absent", f"{note}. Run teamme_librarian_refresh.")
    try:
        if not store.counts(conn).get("commits"):
            conn.close()
            return None, _state("history", "empty", (
                "the history index holds no commits. Run teamme_librarian_refresh."))
    except Exception as exc:
        try:
            conn.close()
        except Exception:
            pass
        return None, _state("history", "error", f"the history index could not be read: {exc}")
    return conn, _state("history", "ok")


def _open_sessions(project_dir, enabled=None):
    """(conn, state). `conn` is None whenever the index cannot be read."""
    if not _is_enabled(project_dir, "sessions", enabled):
        return None, _state("sessions", "disabled", (
            "the sessions librarian is switched off in this project, so its index was not read. "
            'Re-enable it with teamme_librarian_configure {"librarian": "sessions", '
            '"enabled": true}.'))
    path = sessions.db_path(project_dir)
    if not path.exists():
        return None, _state("sessions", "absent", (
            f"{path} does not exist yet - no transcript has been indexed here. Run "
            f'teamme_librarian_refresh {{"librarian": "sessions"}}.'))
    try:
        conn, note = sessions.connect_or_reset(project_dir)
    except Exception as exc:
        return None, _state("sessions", "error", f"the session index could not be opened: {exc}")
    if note:
        try:
            conn.close()
        except Exception:
            pass
        return None, _state("sessions", "absent", (
            f"{note}. Run teamme_librarian_refresh {{\"librarian\": \"sessions\"}}."))
    try:
        n = conn.execute("SELECT COUNT(*) AS n FROM turns").fetchone()["n"]
    except Exception as exc:
        try:
            conn.close()
        except Exception:
            pass
        return None, _state("sessions", "error", f"the session index could not be read: {exc}")
    if not n:
        conn.close()
        return None, _state("sessions", "empty", (
            'the session index holds no turns. Run teamme_librarian_refresh '
            '{"librarian": "sessions"}.'))
    return conn, _state("sessions", "ok")


def _close(conn):
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# the work log - read live, never indexed
# --------------------------------------------------------------------------- #
#
# Deliberately a plain read of the JSON file on every call. See the module
# docstring: a second copy of the ledger in SQLite is the drift this project has
# already paid for four times. A corrupt ledger reads as EMPTY and says so,
# exactly as worklog.py's own load() does - a broken file must not be able to
# turn a query into a crash.

def worklog_path(project_dir=None) -> pathlib.Path:
    return store.project_root(project_dir) / ".claude" / "intake" / "worklog.json"


def read_worklog(project_dir=None):
    """(tasks, state). Never raises; a file that cannot be read is an empty log."""
    path = worklog_path(project_dir)
    if not path.exists():
        return [], _state("worklog", "absent", (
            f"{path} does not exist - this project has no work log, so no task could be "
            f"joined. It is created by the /intake flow."))
    try:
        raw = json.loads(path.read_text())
    except Exception as exc:
        return [], _state("worklog", "error", (
            f"{path} could not be read as JSON ({exc}), so it was treated as empty - the same "
            f"rule worklog.py follows. No task rows are in this answer."))
    tasks = raw.get("tasks") if isinstance(raw, dict) else None
    if not isinstance(tasks, list):
        return [], _state("worklog", "error", (
            f"{path} parsed but carries no `tasks` list, so it was treated as empty. No task "
            f"rows are in this answer."))
    tasks = [t for t in tasks if isinstance(t, dict) and t.get("id")]
    if not tasks:
        return [], _state("worklog", "empty", f"{path} holds no tasks.")
    return tasks, _state("worklog", "ok")


def _task_epochs(task: dict) -> dict:
    """The three timestamps a task carries, as epochs. Unreadable ones are None
    rather than guessed - a fabricated instant would place a row in the wrong
    part of a timeline and nothing downstream could tell."""
    out = {}
    for key in ("created", "updated", "status_changed"):
        value = task.get(key)
        out[key] = store._epoch(value) if value else None
    if out["status_changed"] is None:
        # Pre-migration ledgers have no status_changed; worklog.py defaults it
        # to `updated` on load and this does the same, so an old file behaves
        # identically here.
        out["status_changed"] = out["updated"]
    return out


def _task_window(task: dict, now: int):
    """(lo, hi, description) - the span a task was plausibly being worked in.

    INFERRED, and weak on purpose to stay honest: the ledger stamps when a task
    was created and when its status LAST changed, and nothing else. A task that
    was dispatched and left open therefore spans everything from its creation to
    now. Every caller reports the window it used rather than quietly applying it.
    """
    e = _task_epochs(task)
    lo = e["created"]
    status = task.get("status") or ""
    if lo is None:
        return None, None, "the task carries no readable `created` timestamp, so no window could "\
                           "be inferred"
    if status in OPEN_STATUSES:
        return lo, now, (
            f"created {_iso(lo)} .. now ({_iso(now)}) - the task is still `{status}`, so the "
            f"window runs to the present moment. A task left open spans everything since it was "
            f"filed; treat a wide window as weak evidence.")
    hi = e["status_changed"] or e["updated"] or now
    return lo, hi, (
        f"created {_iso(lo)} .. last status change {_iso(hi)} - inferred, because the ledger "
        f"records only the CURRENT status and when it last changed. It does not record when the "
        f"task was worked on.")


def _task_row(task: dict, relation: str, epoch_from: str = "updated", matched=None) -> dict:
    e = _task_epochs(task)
    row = {
        "store": "worklog",
        "kind": "task",
        "id": task.get("id"),
        "title": _one_line(task.get("title")),
        "status": task.get("status"),
        "priority": task.get("priority"),
        "lane": task.get("lane"),
        "dispatched_to": task.get("dispatched_to") or None,
        "blocked_on": task.get("blocked_on") or None,
        "notes": len(task.get("notes") or []),
        "created": task.get("created"),
        "updated": task.get("updated"),
        "status_changed": task.get("status_changed"),
        "ts": task.get(epoch_from),
        "epoch": e.get(epoch_from),
        "epoch_from": epoch_from,
        "relation": relation,
        "address": {"task": task.get("id")},
        "fetch_with": f"python3 .claude/hooks/worklog.py show {task.get('id')}",
    }
    if matched is not None:
        row["matched_in"] = matched
    return row


def _tasks_open_at(tasks: list, instant: int, limit: int):
    """Tasks that were open at `instant`, inferred - with the inference stated.

    A task counts if it was created at or before the instant AND either it is
    still open now, or its last status change came after the instant (so at the
    instant it was in some EARLIER status, which the ledger does not record).
    """
    hits, unreadable = [], 0
    for t in tasks:
        e = _task_epochs(t)
        if e["created"] is None:
            unreadable += 1
            continue
        if e["created"] > instant:
            continue
        changed = e["status_changed"]
        still_open = (t.get("status") or "") in OPEN_STATUSES
        if still_open or (changed is not None and changed >= instant):
            row = _task_row(t, "open at that instant (inferred)", "created")
            row["inference"] = (
                "still open now" if still_open else
                f"its last status change ({t.get('status_changed')}) came after that instant, so "
                f"at that instant it held some earlier status the ledger does not record")
            hits.append(row)
    hits.sort(key=lambda r: r.get("epoch") or 0, reverse=True)
    return hits[:limit], len(hits) > limit, unreadable


# --------------------------------------------------------------------------- #
# per-store fetchers
# --------------------------------------------------------------------------- #

def _commit_row(r: dict, relation: str) -> dict:
    row = {
        "store": "history",
        "kind": "commit",
        "hash": r.get("hash"),
        "short_hash": r.get("short_hash"),
        "author": r.get("author"),
        "ts": r.get("date"),
        "epoch": r.get("epoch"),
        "subject": _one_line(r.get("subject")),
        "relation": relation,
        "address": {"hash": r.get("hash")},
        "fetch_with": {"query": "commit_detail", "hash": r.get("short_hash") or r.get("hash")},
    }
    for key in ("path", "additions", "deletions"):
        if r.get(key) is not None:
            row[key] = r[key]
    return row


def _commits_touching(conn, project_dir, path: str, limit: int, lo=None, hi=None):
    rel = store._rel_path(project_dir, path)
    where = ["(f.path = ? OR f.path LIKE ? ESCAPE '\\')"]
    params = [rel, store._like(rel.rstrip("/")) + "/%"]
    if lo is not None:
        where.append("c.epoch >= ?")
        params.append(lo)
    if hi is not None:
        where.append("c.epoch <= ?")
        params.append(hi)
    rows = [dict(r) for r in conn.execute(
        "SELECT c.hash, c.short_hash, c.author, c.date, c.epoch, c.subject, "
        "f.path, f.additions, f.deletions "
        "FROM files_changed f JOIN commits c ON c.hash = f.hash "
        "WHERE " + " AND ".join(where) +
        " ORDER BY c.epoch DESC, c.hash LIMIT ?", params + [limit + 1]).fetchall()]
    truncated = len(rows) > limit
    return [_commit_row(r, "touched this path") for r in rows[:limit]], truncated, rel


def _commits_between(conn, lo, hi, limit: int, relation: str):
    rows = [dict(r) for r in conn.execute(
        "SELECT hash, short_hash, author, date, epoch, subject FROM commits "
        "WHERE epoch >= ? AND epoch <= ? ORDER BY epoch DESC, hash LIMIT ?",
        (lo, hi, limit + 1)).fetchall()]
    return [_commit_row(r, relation) for r in rows[:limit]], len(rows) > limit


def _mark_row(r: dict, relation: str) -> dict:
    return {
        "store": "sessions",
        "kind": r.get("kind"),
        "session": r.get("session_id"),
        "seq": r.get("seq"),
        "agent": r.get("agent"),
        "session_title": _one_line(r.get("title"), 80),
        "subagent": bool(r.get("parent_session")),
        "ts": r.get("ts"),
        "epoch": r.get("epoch"),
        "label": _one_line(r.get("label")),
        "relation": relation,
        "address": {"session": r.get("session_id"), "seq": r.get("seq")},
        "fetch_with": {"query": "window", "session": r.get("session_id"), "seq": r.get("seq")},
    }


def _marks_naming_path(conn, rel: str, limit: int, kinds=PATH_MARK_KINDS, lo=None, hi=None):
    needle = "%" + store._like(rel) + "%"
    where = ["m.kind IN (" + ",".join("?" * len(kinds)) + ")",
             "(m.detail LIKE ? ESCAPE '\\' OR m.label LIKE ? ESCAPE '\\')"]
    params = list(kinds) + [needle, needle]
    if lo is not None:
        where.append("m.epoch >= ?")
        params.append(lo)
    if hi is not None:
        where.append("m.epoch <= ?")
        params.append(hi)
    rows = [dict(r) for r in conn.execute(
        "SELECT m.session_id, m.seq, m.kind, m.label, m.ts, m.epoch, "
        "s.agent AS agent, s.title AS title, s.parent_session AS parent_session "
        "FROM marks m LEFT JOIN sessions s ON s.session_id = m.session_id "
        "WHERE " + " AND ".join(where) +
        " ORDER BY COALESCE(m.epoch, 0) DESC, m.session_id, m.seq DESC LIMIT ?",
        params + [limit + 1]).fetchall()]
    out = []
    for r in rows[:limit]:
        row = _mark_row(r, "read or wrote this path")
        if r.get("kind") == "file":
            row["relation"] = "WROTE this path"
        out.append(row)
    return out, len(rows) > limit


def _marks_in_window(conn, lo, hi, limit: int, relation: str, kinds=SPINE_MARK_KINDS):
    rows = [dict(r) for r in conn.execute(
        "SELECT m.session_id, m.seq, m.kind, m.label, m.ts, m.epoch, "
        "s.agent AS agent, s.title AS title, s.parent_session AS parent_session "
        "FROM marks m LEFT JOIN sessions s ON s.session_id = m.session_id "
        "WHERE m.epoch >= ? AND m.epoch <= ? AND m.kind IN ("
        + ",".join("?" * len(kinds)) + ") "
        "ORDER BY m.epoch DESC, m.session_id, m.seq DESC LIMIT ?",
        [lo, hi] + list(kinds) + [limit + 1]).fetchall()]
    out = []
    for r in rows[:limit]:
        row = _mark_row(r, relation)
        if r.get("kind") == "file":
            row["relation"] = relation.replace("said ", "a file was written ")
        out.append(row)
    return out, len(rows) > limit


def _marks_spine(conn, lo, hi, limit: int, relation: str, kinds=None):
    """Marks in a window, with what a PERSON typed outranking what a tool did.

    When the cap binds - and on a window of hours it always does - taking marks
    newest-first returns the tail of the window and nothing else, usually a run
    of consecutive file writes. A prompt is the better spine: it is where the
    work was directed, and the file writes around it are reachable from the
    session region this query already reports. Stated rather than silent,
    because it is a ranking choice and not a fact about the data.
    """
    if kinds is not None:
        return _marks_in_window(conn, lo, hi, limit, relation, kinds), None
    first, trunc_a = _marks_in_window(conn, lo, hi, limit, relation,
                                      ("prompt", "compaction", "recap"))
    note = None
    if len(first) >= limit:
        return (first, True), ("capped on what a person typed and what a compaction dropped; "
                               "file writes in the same window were left out. Ask again with "
                               '{"kind": "file"} for those.')
    rest, trunc_b = _marks_in_window(conn, lo, hi, limit - len(first), relation, ("file",))
    if first and rest:
        note = ("what a person typed is listed before file writes when the cap binds - a ranking "
                "choice, not an ordering in the data. The rows themselves are still in time order.")
    return (first + rest, bool(trunc_a or trunc_b)), note


def _turns_nearest(conn, anchor: int, lo: int, hi: int, limit: int):
    """The turns closest in time to `anchor`, inside [lo, hi].

    Nearest rather than newest: a window either side of an instant whose rows
    are taken newest-first would return only the tail of it and quietly drop
    everything before the anchor.
    """
    rows = [dict(r) for r in conn.execute(
        "SELECT t.session_id, t.seq, t.role, t.ts, t.epoch, t.text, t.text_chars, "
        "s.agent AS agent, s.title AS title, s.parent_session AS parent_session "
        "FROM turns t LEFT JOIN sessions s ON s.session_id = t.session_id "
        "WHERE t.epoch >= ? AND t.epoch <= ? "
        "ORDER BY ABS(t.epoch - ?), t.session_id, t.seq LIMIT ?",
        (lo, hi, anchor, limit + 1)).fetchall()]
    truncated = len(rows) > limit
    out = []
    for r in rows[:limit]:
        delta = (r.get("epoch") or anchor) - anchor
        out.append({
            "store": "sessions",
            "kind": "turn",
            "session": r.get("session_id"),
            "seq": r.get("seq"),
            "role": r.get("role"),
            "agent": r.get("agent"),
            "session_title": _one_line(r.get("title"), 80),
            "subagent": bool(r.get("parent_session")),
            "ts": r.get("ts"),
            "epoch": r.get("epoch"),
            "seconds_from_anchor": delta,
            "preview": _one_line(r.get("text"), MAX_LABEL_CHARS),
            "text_chars": r.get("text_chars"),
            "relation": ("before it" if delta < 0 else "after it" if delta > 0 else "at it"),
            "address": {"session": r.get("session_id"), "seq": r.get("seq")},
            "fetch_with": {"query": "window", "session": r.get("session_id"), "seq": r.get("seq")},
        })
    return out, truncated


def _sessions_in_window(conn, lo, hi, limit: int):
    """Which sessions were active in a window, and how much of each was - a
    region, not a transcript."""
    rows = [dict(r) for r in conn.execute(
        "SELECT t.session_id AS session_id, COUNT(*) AS turns, MIN(t.ts) AS first_ts, "
        "MAX(t.ts) AS last_ts, MIN(t.seq) AS first_seq, MAX(t.seq) AS last_seq, "
        "s.agent AS agent, s.title AS title, s.parent_session AS parent_session "
        "FROM turns t LEFT JOIN sessions s ON s.session_id = t.session_id "
        "WHERE t.epoch >= ? AND t.epoch <= ? GROUP BY t.session_id "
        "ORDER BY turns DESC, t.session_id LIMIT ?", (lo, hi, limit + 1)).fetchall()]
    truncated = len(rows) > limit
    out = []
    for r in rows[:limit]:
        out.append({
            "session": r.get("session_id"),
            "agent": r.get("agent"),
            "title": _one_line(r.get("title"), 80),
            "subagent": bool(r.get("parent_session")),
            "turns_in_window": r.get("turns"),
            "first_ts": r.get("first_ts"),
            "last_ts": r.get("last_ts"),
            "seq_range": [r.get("first_seq"), r.get("last_seq")],
            "fetch_with": {"query": "window", "session": r.get("session_id"),
                           "seq": r.get("first_seq")},
        })
    return out, truncated


# --------------------------------------------------------------------------- #
# the four queries
# --------------------------------------------------------------------------- #

def _normalize_time(rows: list) -> list:
    """Give every row one comparable instant, `ts_utc`.

    The three stores print time in three ways: git records the committer's own
    UTC offset, the transcripts record UTC, the work log records UTC with an
    explicit +00:00. Sorting is on `epoch` and was always right, but a list that
    PRINTS +03:00 next to UTC reads as out of order, and a spine nobody can
    check by eye is a spine nobody will trust. Each row keeps the store's own
    string in `ts` and gains the normalized one; the renderer shows `ts_utc`.
    """
    for r in rows:
        r["ts_utc"] = _iso(r["epoch"]) if r.get("epoch") is not None else None
    return rows


def _envelope(name: str, rows: list, states: list, limit: int, extra: dict = None,
              caveats=None) -> dict:
    rows = _normalize_time(rows)
    out = {
        "ok": True,
        "query": name,
        "rows": rows,
        "count": len(rows),
        "limit": limit,
        "limit_applies": "per store - the cap below is applied to each store separately, so one "
                         "busy store cannot crowd the others out of the answer",
        "truncated": any(s.get("truncated") for s in states),
        "stores": {s["store"]: s for s in states},
        "stores_with_nothing": [s["store"] for s in states if not s.get("rows")],
        "caveats": list(caveats if caveats is not None
                        else (CORRELATION_CAVEAT, SPINE_CAVEAT)),
    }
    if extra:
        out.update(extra)
    return out


def around_path(args: dict, project_dir, limit: int, enabled=None) -> dict:
    """Everything the three stores hold about one file or directory."""
    raw = args.get("path")
    if not isinstance(raw, str) or not raw.strip():
        return {"ok": False, "error": "around_path needs `path` - a repo-relative file or "
                                      "directory (an absolute path inside the project is "
                                      "accepted)"}
    rel = store._rel_path(project_dir, raw.strip())
    lo = store._epoch(args["since"]) if args.get("since") else None
    hi = store._epoch(args["until"]) if args.get("until") else None
    if args.get("since") and lo is None:
        return {"ok": False, "error": f"could not read `since` as a date: {args['since']!r}"}
    if args.get("until") and hi is None:
        return {"ok": False, "error": f"could not read `until` as a date: {args['until']!r}"}
    kinds = PATH_MARK_KINDS
    asked = args.get("kind")
    if isinstance(asked, str) and asked.strip():
        if asked.strip() not in PATH_MARK_KINDS:
            return {"ok": False,
                    "error": f"around_path matches a path against the TARGET of a tool call, so "
                             f"`kind` here is one of: {', '.join(PATH_MARK_KINDS)} ('file' is a "
                             f"tool that wrote the path, 'tool' is everything else that named "
                             f"it). '{asked}' carries prose, not a path - search it with "
                             f"`search_turns`."}
        kinds = (asked.strip(),)

    rows, states = [], []

    conn, st = _open_history(project_dir, enabled)
    if conn is not None:
        try:
            got, trunc, rel = _commits_touching(conn, project_dir, rel, limit, lo, hi)
            rows += got
            st.update(rows=len(got), truncated=trunc)
            if not got:
                st["detail"] = (f"no indexed commit changed '{rel}'. It may be untracked, newer "
                                f"than the last refresh, or spelled differently.")
        except Exception as exc:
            st = _state("history", "error", f"the history query failed: {exc}")
        finally:
            _close(conn)
    states.append(st)

    conn, st = _open_sessions(project_dir, enabled)
    if conn is not None:
        try:
            got, trunc = _marks_naming_path(conn, rel, limit, kinds=kinds, lo=lo, hi=hi)
            rows += got
            st.update(rows=len(got), truncated=trunc)
            if trunc and len(kinds) > 1:
                st["detail"] = (f"capped at {limit}, newest first - recent READS can bury older "
                                f"WRITES. Ask again with {{\"kind\": \"file\"}} for the writes "
                                f"alone.")
            if not got:
                st["detail"] = (f"no indexed tool call of kind(s) {', '.join(kinds)} names "
                                f"'{rel}'. Only the tool's own target is matched here, not what "
                                f"was said about the file - ask `search_turns` for that.")
        except Exception as exc:
            st = _state("sessions", "error", f"the session query failed: {exc}")
        finally:
            _close(conn)
    states.append(st)

    tasks, st = read_worklog(project_dir)
    if tasks:
        hits = []
        needle = rel.lower()
        base = rel.rsplit("/", 1)[-1].lower()
        for t in tasks:
            where = []
            if needle in (t.get("title") or "").lower():
                where.append("title")
            for i, note in enumerate(t.get("notes") or []):
                if needle in str(note).lower():
                    where.append(f"note {i + 1}")
            if not where and base and base != needle:
                # The bare filename, only when the full path missed: a note that
                # says "worklog.py" is naming the same file the index knows as
                # templates/hooks/worklog.py, and refusing that match would lose
                # most of the real ones. Reported separately so the weaker
                # basis is visible rather than blended in.
                if base in (t.get("title") or "").lower():
                    where.append("title (filename only)")
                for i, note in enumerate(t.get("notes") or []):
                    if base in str(note).lower():
                        where.append(f"note {i + 1} (filename only)")
            if where:
                hits.append(_task_row(t, "names this path", "updated", where[:MAX_TASK_NOTES]))
        hits = _sort_rows(hits)
        trunc = len(hits) > limit
        hits = hits[:limit]
        rows += hits
        st.update(rows=len(hits), truncated=trunc)
        if not hits:
            st["detail"] = (f"no task's title or notes mention '{rel}' or '{base}'. Nothing "
                            f"requires a note to name a file, so this is not evidence no task "
                            f"touched it.")
    states.append(st)

    return _envelope("around_path", _sort_rows(rows), states, limit,
                     extra={"path": rel, "path_as_given": raw.strip(),
                            "mark_kinds": list(kinds),
                            "since": args.get("since"), "until": args.get("until")},
                     caveats=(CORRELATION_CAVEAT, WORKLOG_PROSE_CAVEAT, SPINE_CAVEAT,
                              transcripts.TOOL_RESULTS_NOT_INDEXED))


def around_commit(args: dict, project_dir, limit: int, enabled=None) -> dict:
    """One commit, the conversation near it in time, and the tasks open when it
    landed.

    The history index is the ANCHOR here, not one contributor among three: with
    it off or absent there is no instant to be 'around', so this refuses with the
    fix rather than returning session rows anchored on nothing.
    """
    raw = str(args.get("hash") or "").strip().lower()
    if not raw or not store.HEX.match(raw):
        return {"ok": False, "error": "around_commit needs `hash` (a full or abbreviated commit "
                                      "hash)"}
    minutes = _minutes(args.get("minutes"))
    rows, states = [], []

    conn, st = _open_history(project_dir, enabled)
    if conn is None:
        return {"ok": False,
                "error": f"around_commit cannot resolve a commit without the history index. "
                         f"{st.get('detail')}"}
    try:
        found = [dict(r) for r in conn.execute(
            "SELECT hash, short_hash, author, date, epoch, subject FROM commits "
            "WHERE hash = ? OR hash LIKE ? ESCAPE '\\' ORDER BY epoch DESC, hash LIMIT ?",
            (raw, store._like(raw) + "%", store.MAX_AMBIGUOUS + 1)).fetchall()]
        exact = [r for r in found if (r.get("hash") or "") == raw]
        if exact:
            found = exact[:1]
        if not found:
            return {"ok": False,
                    "error": f"no commit in the index starts with '{raw}'. It may predate the "
                             f"index or postdate the last refresh - run a refresh, or ask "
                             f"`recent` for a hash that is in it"}
        if len(found) > 1:
            shown = [r.get("short_hash") or (r.get("hash") or "")[:12]
                     for r in found[:store.MAX_AMBIGUOUS]]
            return {"ok": False,
                    "error": f"'{raw}' is ambiguous - it matches {len(shown)} commits: "
                             f"{', '.join(shown)}. Give more characters of the hash"}
        anchor = found[0]
        at = anchor.get("epoch")
        files = [dict(r) for r in conn.execute(
            "SELECT path, additions, deletions FROM files_changed WHERE hash = ? "
            "ORDER BY path LIMIT ?", (anchor.get("hash"), limit + 1)).fetchall()]
        files_truncated = len(files) > limit
        files = files[:limit]
        rows.append(_commit_row(anchor, "the anchor"))
        st.update(rows=1)
    except Exception as exc:
        _close(conn)
        return {"ok": False, "error": f"around_commit failed reading the history index: {exc}"}
    finally:
        _close(conn)
    states.append(st)

    if at is None:
        return {"ok": False,
                "error": f"commit {anchor.get('short_hash')} carries no epoch in the index, so "
                         f"nothing can be placed around it. Run a full refresh "
                         f"(teamme_librarian_refresh with full=true)."}
    lo, hi = at - minutes * 60, at + minutes * 60

    conn, st = _open_sessions(project_dir, enabled)
    if conn is not None:
        try:
            got, trunc = _turns_nearest(conn, at, lo, hi, limit)
            rows += got
            st.update(rows=len(got), truncated=trunc)
            if not got:
                st["detail"] = (f"no indexed turn falls within {minutes} minute(s) of "
                                f"{anchor.get('date')}. The session index may not reach back that "
                                f"far, or may be behind - check teamme_librarian_status.")
        except Exception as exc:
            st = _state("sessions", "error", f"the session query failed: {exc}")
        finally:
            _close(conn)
    states.append(st)

    tasks, st = read_worklog(project_dir)
    if tasks:
        # Two classes of task row, and the stronger one gets the slots first.
        #
        # A task that MOVED inside the window is the near-exact evidence this
        # join can offer - a task marked done twenty seconds before a commit
        # landed is not "open at that instant" and would otherwise be dropped
        # from the answer about the very commit that closed it. That is a real
        # case on this repository, and it is why "open at the instant" alone is
        # not enough.
        moved, seen = [], set()
        for t in tasks:
            e = _task_epochs(t)
            for key, what in (("status_changed", f"moved to `{t.get('status')}`"),
                              ("created", "was filed")):
                when = e.get(key)
                if when is None or not (lo <= when <= hi):
                    continue
                if key == "status_changed" and e.get("created") == when:
                    # Never transitioned since it was filed: saying it "moved
                    # to open" would invent an event the ledger never recorded.
                    what = f"was filed as `{t.get('status')}`"
                row = _task_row(t, f"{what} inside the window ({abs(when - at)}s from the commit)",
                                key)
                row["seconds_from_anchor"] = when - at
                moved.append((abs(when - at), row))
                seen.add(t.get("id"))
                break
        moved.sort(key=lambda pair: pair[0])
        picked = [row for _, row in moved[:limit]]
        trunc = len(moved) > limit
        unreadable = 0
        if len(picked) < limit:
            rest, more, unreadable = _tasks_open_at(tasks, at, limit - len(picked))
            picked += [r for r in rest if r.get("id") not in seen]
            trunc = trunc or more
        rows += picked
        st.update(rows=len(picked), truncated=trunc)
        details = []
        if moved:
            details.append(f"{len(moved)} task(s) moved or were filed inside the window; those "
                           f"rows are listed first and are the strongest link this join has.")
        if unreadable:
            details.append(f"{unreadable} task(s) carry no readable `created` timestamp and were "
                           f"left out of the overlap test.")
        if not picked:
            details.append("no task moved inside the window, and none was open at that instant "
                           "by the inference described above.")
        if details:
            st["detail"] = " ".join(details)
    states.append(st)

    return _envelope("around_commit", _sort_rows(rows), states, limit,
                     extra={"anchor": {"hash": anchor.get("hash"),
                                       "short_hash": anchor.get("short_hash"),
                                       "subject": _one_line(anchor.get("subject")),
                                       "author": anchor.get("author"),
                                       "date": anchor.get("date"), "epoch": at},
                            "files_changed": files,
                            "files_truncated": files_truncated,
                            "window_minutes": minutes,
                            "window": [_iso(lo), _iso(hi)]},
                     caveats=(CORRELATION_CAVEAT,
                              "a task listed here was open at that instant - which the ledger "
                              "does not record directly either: it stamps only the CURRENT status "
                              "and when it last changed, so 'open then' is inferred from "
                              "`created` and `status_changed`, and each row says which inference "
                              "was used.",
                              SPINE_CAVEAT,
                              transcripts.TOOL_RESULTS_NOT_INDEXED))


def around_task(args: dict, project_dir, limit: int, enabled=None) -> dict:
    """One task's own record, the commits that landed in its window, and the
    session region it was worked in."""
    raw = str(args.get("task") or "").strip()
    if not raw:
        return {"ok": False, "error": "around_task needs `task` - a work-log id such as T12"}
    tasks, wl_state = read_worklog(project_dir)
    if not tasks:
        return {"ok": False,
                "error": f"around_task cannot run without a work log. {wl_state.get('detail')}"}
    want = raw.upper()
    task = next((t for t in tasks if str(t.get("id", "")).upper() == want), None)
    if task is None:
        ids = [str(t.get("id")) for t in tasks][:MAX_TASK_IDS_LISTED]
        more = f" (and {len(tasks) - len(ids)} more)" if len(tasks) > len(ids) else ""
        return {"ok": False,
                "error": f"no task '{raw}' in {worklog_path(project_dir)}. It holds: "
                         f"{', '.join(ids)}{more}"}

    kinds = None
    asked = args.get("kind")
    if isinstance(asked, str) and asked.strip():
        if asked.strip() not in transcripts.MARK_KINDS:
            return {"ok": False, "error": f"unknown mark kind '{asked}'. One of: "
                                          f"{', '.join(transcripts.MARK_KINDS)}"}
        kinds = (asked.strip(),)
    pad = args.get("pad_minutes")
    pad = DEFAULT_PAD_MINUTES if pad is None else _minutes(pad) if pad else 0
    pad = min(pad, MAX_MINUTES)
    now = _now_epoch()
    lo, hi, window_note = _task_window(task, now)
    notes = [_one_line(n, MAX_NOTE_CHARS) for n in (task.get("notes") or [])]
    notes_truncated = len(notes) > MAX_TASK_NOTES

    record = _task_row(task, "the anchor", "status_changed")
    record["notes_preview"] = notes[:MAX_TASK_NOTES]
    record["notes_clipped_at_chars"] = MAX_NOTE_CHARS
    rows = [record]
    states = [dict(wl_state, rows=1, truncated=notes_truncated)]

    if lo is None:
        states.append(_state("history", "skipped", window_note))
        states.append(_state("sessions", "skipped", window_note))
        return _envelope("around_task", rows, states, limit,
                         extra={"task": task.get("id"), "window": None,
                                "window_basis": window_note},
                         caveats=(CORRELATION_CAVEAT, SPINE_CAVEAT))

    # The pad is why a task marked `done` at 13:54:21 still finds the commit that
    # closed it at 13:54:41. The ledger stamps the transition when the agent
    # records it, which on this team is seconds BEFORE the commit is written, so
    # an unpadded window systematically misses the one commit most likely to
    # matter. It is reported in `window_basis` every time rather than applied
    # quietly - a widened window is a weaker claim, and the caller has to be able
    # to see that it was widened.
    if pad:
        lo, hi = lo - pad * 60, hi + pad * 60
        window_note += (f" Padded by {pad} minute(s) either side, giving {_iso(lo)} .. "
                        f"{_iso(hi)}: a status is recorded seconds before or after the commit or "
                        f"message it refers to, so an exact window misses the boundary. Pass "
                        f"`pad_minutes: 0` for the unpadded window.")

    conn, st = _open_history(project_dir, enabled)
    if conn is not None:
        try:
            got, trunc = _commits_between(conn, lo, hi, limit,
                                          "landed while the task was open (time overlap only)")
            rows += got
            st.update(rows=len(got), truncated=trunc)
            if not got:
                st["detail"] = "no indexed commit landed inside that window."
        except Exception as exc:
            st = _state("history", "error", f"the history query failed: {exc}")
        finally:
            _close(conn)
    states.append(st)

    regions, regions_truncated = [], False
    conn, st = _open_sessions(project_dir, enabled)
    if conn is not None:
        try:
            regions, regions_truncated = _sessions_in_window(conn, lo, hi, limit)
            (got, trunc), note = _marks_spine(
                conn, lo, hi, limit,
                "said while the task was open (time overlap only)", kinds)
            rows += got
            st.update(rows=len(got), truncated=bool(trunc or regions_truncated))
            if note:
                st["detail"] = note
            if not got:
                st["detail"] = ("no indexed mark point falls inside that window - the session "
                                "index may not reach back that far, or may be behind.")
        except Exception as exc:
            st = _state("sessions", "error", f"the session query failed: {exc}")
        finally:
            _close(conn)
    states.append(st)

    return _envelope("around_task", _sort_rows(rows), states, limit,
                     extra={"task": task.get("id"),
                            "title": _one_line(task.get("title")),
                            "status": task.get("status"),
                            "window": [_iso(lo), _iso(hi)],
                            "window_hours": round((hi - lo) / 3600.0, 2),
                            "window_basis": window_note,
                            "pad_minutes": pad,
                            "mark_kinds": list(kinds) if kinds else None,
                            "session_regions": regions,
                            "session_regions_truncated": regions_truncated,
                            "notes_total": len(task.get("notes") or []),
                            "notes_truncated": notes_truncated},
                     caveats=(
                         "the task's ACTIVE WINDOW is inferred, not recorded. " + window_note,
                         CORRELATION_CAVEAT,
                         SPINE_CAVEAT,
                         transcripts.TOOL_RESULTS_NOT_INDEXED))


def timeline(args: dict, project_dir, limit: int, enabled=None) -> dict:
    """Everything all three stores hold between two instants."""
    now = _now_epoch()
    since, until = args.get("since"), args.get("until")
    lo = store._epoch(since) if since else None
    hi = store._epoch(until) if until else None
    if since and lo is None:
        return {"ok": False, "error": f"could not read `since` as a date: {since!r}"}
    if until and hi is None:
        return {"ok": False, "error": f"could not read `until` as a date: {until!r}"}
    if lo is None and hi is None:
        lo, hi = now - 24 * 3600, now
        defaulted = "neither `since` nor `until` was given, so this is the last 24 hours"
    else:
        defaulted = None
        if lo is None:
            lo = 0
        if hi is None:
            hi = now
    if lo > hi:
        return {"ok": False,
                "error": f"`since` ({_iso(lo)}) is after `until` ({_iso(hi)}) - nothing can fall "
                         f"between them"}

    rows, states = [], []

    conn, st = _open_history(project_dir, enabled)
    if conn is not None:
        try:
            got, trunc = _commits_between(conn, lo, hi, limit, "landed in this range")
            rows += got
            st.update(rows=len(got), truncated=trunc)
            if not got:
                st["detail"] = "no indexed commit landed in this range."
        except Exception as exc:
            st = _state("history", "error", f"the history query failed: {exc}")
        finally:
            _close(conn)
    states.append(st)

    kinds = SPINE_MARK_KINDS
    asked = args.get("kind")
    if isinstance(asked, str) and asked.strip():
        if asked.strip() not in transcripts.MARK_KINDS:
            return {"ok": False,
                    "error": f"unknown mark kind '{asked}'. One of: "
                             f"{', '.join(transcripts.MARK_KINDS)}"}
        kinds = (asked.strip(),)

    conn, st = _open_sessions(project_dir, enabled)
    if conn is not None:
        try:
            got, trunc = _marks_in_window(conn, lo, hi, limit, "said in this range", kinds)
            rows += got
            st.update(rows=len(got), truncated=trunc)
            if not got:
                st["detail"] = (f"no indexed mark point of kind(s) {', '.join(kinds)} falls in "
                                f"this range.")
        except Exception as exc:
            st = _state("sessions", "error", f"the session query failed: {exc}")
        finally:
            _close(conn)
    states.append(st)

    tasks, st = read_worklog(project_dir)
    if tasks:
        # A task is EVENTS here, not one row: it was filed at one instant, its
        # status last moved at another, and a note was last added at a third.
        # Collapsing those into one row would put a task filed weeks ago into
        # today's range, or leave it out of the day it was actually finished.
        events, unreadable = [], 0
        for t in tasks:
            e = _task_epochs(t)
            if e["created"] is None:
                unreadable += 1
            seen = set()
            for key, kind, relation in (
                    ("created", "task_created", "filed in this range"),
                    ("status_changed", "task_status",
                     f"moved to `{t.get('status')}` in this range"),
                    ("updated", "task_note", "last touched in this range")):
                at = e.get(key)
                if at is None or at in seen or not (lo <= at <= hi):
                    seen.add(at)
                    continue
                seen.add(at)
                row = _task_row(t, relation, key)
                row["kind"] = kind
                events.append(row)
        events = _sort_rows(events)
        trunc = len(events) > limit
        events = events[:limit]
        rows += events
        st.update(rows=len(events), truncated=trunc)
        if unreadable:
            st["detail"] = f"{unreadable} task(s) carry no readable `created` timestamp."
        elif not events:
            st["detail"] = "no task was filed, moved or touched in this range."
    states.append(st)

    return _envelope("timeline", _sort_rows(rows), states, limit,
                     extra={"since": _iso(lo), "until": _iso(hi),
                            "range_hours": round((hi - lo) / 3600.0, 2),
                            "defaulted": defaulted,
                            "mark_kinds": list(kinds)},
                     caveats=(
                         "this is a time range, so nothing here is claimed to be RELATED - the "
                         "rows share an interval and nothing more.",
                         "a task contributes up to three events (filed, last status change, last "
                         "touched); the ledger records no other instants, so work done on a task "
                         "between two transitions leaves no event of its own.",
                         SPINE_CAVEAT,
                         transcripts.TOOL_RESULTS_NOT_INDEXED))


QUERIES = {
    "around_path": around_path,
    "around_commit": around_commit,
    "around_task": around_task,
    "timeline": timeline,
}


def query(name: str, args: dict = None, project_dir=None, enabled=None) -> dict:
    """One of QUERY_NAMES. Never raises: every failure is a result dict."""
    args = args if isinstance(args, dict) else {}
    name = (name or "").strip()
    fn = QUERIES.get(name)
    if fn is None:
        return {"ok": False,
                "error": f"unknown cross-index query '{name}'. One of: {', '.join(QUERY_NAMES)}"}
    limit = _clamp(args.get("limit") or args.get("n"))
    try:
        return fn(args, project_dir, limit, enabled)
    except Exception as exc:
        return {"ok": False, "error": f"query failed: {exc}"}
