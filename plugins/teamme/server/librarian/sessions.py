#!/usr/bin/env python3
"""The session librarian: an index of this project's conversations.

After a compaction the detail is gone from context, but it is not gone from
disk - the harness writes every session to an append-only JSONL file. Recovering
something from it by re-reading the file costs more context than it returns,
which is the problem this index exists to remove: ask WHERE something was
discussed, then fetch a bounded window around that point.

Three things it is, and is not:

  LAZY, not live. Nothing is captured in flight, because the harness already
  captured it. A refresh reads only the bytes appended since the last one, the
  same incremental property history.py has. So this librarian adds NO hook,
  nothing joins REQUIRED_HOOKS, nothing is copied into a project, and no turn
  pays any latency for it. (An earlier design note on T30 argued the opposite -
  continuous capture through hooks - and was overturned by measuring the file.)

  DERIVED, not a record. history.py keeps an append-only JSONL because its
  record is committable and mergeable. This one deliberately does NOT: the
  transcript is already the record, and a second text copy of a conversation
  would double a private surface for nothing. .claude/librarians/sessions/ holds
  one disposable SQLite index, reconstructible by a full reindex, and is written
  into the same always-ignored .gitignore block as index.db - `commit_record`
  does not reach it and cannot be made to.

  BOUNDED, always. A query that answers "what did we decide about X" with 40k
  tokens of transcript has moved the cost rather than removed it. Every result
  is capped and every cap SAYS it was applied.

WHAT A MARK POINT IS - the definition the schema follows from:

  A mark point is a position in a session a person could later point at and
  name. Not every record is one. Seven kinds:

    prompt      a human typed something
    message     something else was said into the conversation - a peer agent's
                message, a task notification, a slash command's output
    recap       the summary written INTO the conversation by a compaction; what
                survived, in the harness's own words
    answer      the assistant said something visible
    tool        a tool ran
    file        a tool WROTE a file (separated from `tool` because "when did we
                change X" is a different question from "what ran")
    compaction  context was dropped here. Everything before it is what the
                session can no longer see, and it is derivable from the
                transcript itself - no PreCompact hook is involved.

  Marks hang off TURNS. A turn is one content-bearing record, numbered `seq`
  within its session; `seq` is the address every query returns and `window`
  accepts. It is stable across refreshes because the file is append-only.

  A SUBAGENT THREAD is a session too. The harness writes one sidecar file per
  dispatched specialist, and on this repo those hold 26 MB against the main
  thread's 8.7 MB - on a team that dispatches, that is where most of the work,
  and most of the reasoning a later question asks about, actually happened.
  Each sidecar is its own append-only file, so it is indexed exactly like a main
  thread and carries `parent_session` and `agent`. `sessions` lists main threads
  by default and reports the count of children; `search_turns` searches both.

Stdlib only. Nothing here raises at the boundary: callers get a result dict.
"""

import json
import pathlib
import time
from datetime import datetime, timezone

try:
    from . import config, store, transcripts
except ImportError:  # loaded as a loose module rather than a package member
    import config             # type: ignore
    import store              # type: ignore
    import transcripts        # type: ignore

SCHEMA_VERSION = 1

DEFAULT_LIMIT = 20
MAX_LIMIT = 100

# `window` caps. A window is the one query that returns transcript text, so it
# is the one that would recreate the problem if it were unbounded.
DEFAULT_WINDOW = 4            # turns either side of the mark
MAX_WINDOW = 25
MAX_WINDOW_TURN_CHARS = 2000  # per turn in a window
MAX_WINDOW_TOTAL_CHARS = 24000

# A search result shows the text AROUND the hit, never the turn.
SNIPPET_PAD = 140
MAX_SNIPPET_CHARS = 2 * SNIPPET_PAD + 200

# How many mark points the compaction report may list from the dropped region.
MAX_LOST_MARKS = 40

META_SCHEMA_VERSION = "schema_version"
META_LAST_REFRESH = "sessions.last_refresh_at"
META_TRANSCRIPT_DIR = "sessions.transcript_dir"

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id    TEXT PRIMARY KEY,
    path          TEXT,
    parent_session TEXT,
    agent         TEXT,
    title         TEXT,
    cwd           TEXT,
    git_branch    TEXT,
    version       TEXT,
    first_ts      TEXT,
    last_ts       TEXT,
    first_epoch   INTEGER,
    last_epoch    INTEGER,
    turns         INTEGER DEFAULT 0,
    prompts       INTEGER DEFAULT 0,
    marks         INTEGER DEFAULT 0,
    compactions   INTEGER DEFAULT 0,
    next_seq      INTEGER DEFAULT 0,
    bytes_indexed INTEGER DEFAULT 0,
    file_size     INTEGER,
    lines_read    INTEGER DEFAULT 0,
    malformed     INTEGER DEFAULT 0,
    partial_tail  INTEGER DEFAULT 0,
    fingerprint   TEXT,
    indexed_at    TEXT
);
CREATE TABLE IF NOT EXISTS turns (
    session_id  TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    uuid        TEXT,
    role        TEXT,
    ts          TEXT,
    epoch       INTEGER,
    sidechain   INTEGER DEFAULT 0,
    text        TEXT,
    text_chars  INTEGER,
    truncated   INTEGER DEFAULT 0,
    PRIMARY KEY (session_id, seq)
);
CREATE TABLE IF NOT EXISTS marks (
    session_id  TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    pos         INTEGER NOT NULL,
    kind        TEXT NOT NULL,
    label       TEXT,
    detail      TEXT,
    extra       TEXT,
    ts          TEXT,
    epoch       INTEGER,
    sidechain   INTEGER DEFAULT 0,
    PRIMARY KEY (session_id, seq, pos)
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE INDEX IF NOT EXISTS idx_turns_epoch ON turns(epoch);
CREATE INDEX IF NOT EXISTS idx_marks_kind ON marks(kind);
CREATE INDEX IF NOT EXISTS idx_marks_epoch ON marks(epoch);
CREATE INDEX IF NOT EXISTS idx_marks_session_seq ON marks(session_id, seq);
CREATE INDEX IF NOT EXISTS idx_sessions_parent ON sessions(parent_session);
"""


# --------------------------------------------------------------------------- #
# paths and connection
# --------------------------------------------------------------------------- #

def db_path(project_dir=None) -> pathlib.Path:
    return store.sessions_dir(project_dir) / "index.db"


def connect(project_dir=None):
    return store.connect_file(db_path(project_dir), SCHEMA)


def connect_or_reset(project_dir=None):
    return store.connect_or_reset_file(db_path(project_dir), SCHEMA)


def index_lock(project_dir=None):
    return store.lock(store.sessions_dir(project_dir) / "index.lock")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _get_meta(conn, key, default=None):
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    except Exception:
        return default
    return row["value"] if row is not None else default


def _set_meta(conn, key, value):
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )


def _span(value) -> int:
    """Turns either side of a window's anchor. Unlike a row limit, ZERO is a
    real answer here - "just this turn" is a thing to ask for - so it is honoured
    rather than read as "unspecified"."""
    if value is None or isinstance(value, bool):
        return DEFAULT_WINDOW
    try:
        n = int(value)
    except Exception:
        return DEFAULT_WINDOW
    return min(max(n, 0), MAX_WINDOW)


def _clamp(value, default: int = DEFAULT_LIMIT, cap: int = MAX_LIMIT) -> int:
    try:
        n = int(value)
    except Exception:
        return default
    if n <= 0:
        return default
    return min(n, cap)


# --------------------------------------------------------------------------- #
# indexing
# --------------------------------------------------------------------------- #

def _session_row(conn, session_id):
    try:
        row = conn.execute("SELECT * FROM sessions WHERE session_id = ?",
                           (session_id,)).fetchone()
    except Exception:
        return None
    return dict(row) if row is not None else None


def _forget(conn, session_id):
    for table in ("marks", "turns"):
        conn.execute(f"DELETE FROM {table} WHERE session_id = ?", (session_id,))
    conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))


def index_one(conn, entry: dict, full: bool = False) -> dict:
    """Bring ONE session up to the end of its file. Never raises."""
    session_id = entry["session_id"]
    path = entry["path"]
    out = {"session_id": session_id, "mode": "incremental", "turns_added": 0,
           "marks_added": 0, "malformed": 0, "oversized": 0, "reset": None,
           "partial_tail": False, "error": None}

    prior = None if full else _session_row(conn, session_id)
    offset = int((prior or {}).get("bytes_indexed") or 0)
    seq = int((prior or {}).get("next_seq") or 0)

    scan = transcripts.Scan(path, offset)
    fingerprint = scan.head_uuid()

    if prior is not None:
        size = entry.get("size") or 0
        prior_fp = prior.get("fingerprint")
        if size < offset:
            out["reset"] = (
                f"{session_id}: the file is smaller than the {offset} bytes already indexed, so it "
                f"was replaced rather than appended to - reindexed in full")
        elif prior_fp and fingerprint and prior_fp != fingerprint:
            out["reset"] = (
                f"{session_id}: the file's first record changed, so it is not the file that was "
                f"indexed before - reindexed in full")
        if out["reset"]:
            prior, offset, seq = None, 0, 0
            scan = transcripts.Scan(path, 0)

    if prior is None:
        out["mode"] = "full"
        try:
            _forget(conn, session_id)
        except Exception:
            pass

    turns = prompts = marks = compactions = 0
    first_ts = (prior or {}).get("first_ts")
    first_epoch = (prior or {}).get("first_epoch")
    last_ts = (prior or {}).get("last_ts")
    last_epoch = (prior or {}).get("last_epoch")
    cwd = (prior or {}).get("cwd")
    branch = (prior or {}).get("git_branch")
    version = (prior or {}).get("version")
    title = (prior or {}).get("title") or entry.get("title")

    try:
        for turn in scan:
            text = turn.get("text") or ""
            chars = int(turn.get("text_chars") or len(text))
            conn.execute(
                "INSERT OR REPLACE INTO turns (session_id, seq, uuid, role, ts, epoch, "
                "sidechain, text, text_chars, truncated) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (session_id, seq, turn.get("uuid"), turn.get("role"), turn.get("ts"),
                 turn.get("epoch"), 1 if turn.get("sidechain") else 0, text, chars,
                 1 if chars > len(text) else 0),
            )
            for pos, mark in enumerate(turn.get("marks") or []):
                extra = mark.get("extra")
                conn.execute(
                    "INSERT OR REPLACE INTO marks (session_id, seq, pos, kind, label, detail, "
                    "extra, ts, epoch, sidechain) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (session_id, seq, pos, mark.get("kind"), mark.get("label"),
                     mark.get("detail"),
                     json.dumps(extra, ensure_ascii=False) if isinstance(extra, dict) else None,
                     turn.get("ts"), turn.get("epoch"),
                     1 if turn.get("sidechain") else 0),
                )
                marks += 1
                if mark.get("kind") == "prompt":
                    prompts += 1
                elif mark.get("kind") == "compaction":
                    compactions += 1
            if turn.get("ts"):
                if not first_ts:
                    first_ts, first_epoch = turn["ts"], turn.get("epoch")
                last_ts, last_epoch = turn["ts"], turn.get("epoch")
            cwd = turn.get("cwd") or cwd
            branch = turn.get("git_branch") or branch
            version = turn.get("version") or version
            seq += 1
            turns += 1
    except Exception as exc:
        # Whatever was consumed before the failure is committed with the offset
        # that matches it, so the next refresh resumes rather than replays.
        out["error"] = f"stopped part-way through {path}: {exc}"

    if scan.error:
        out["error"] = out["error"] or scan.error
    title = scan.title or title
    out.update(turns_added=turns, marks_added=marks, malformed=scan.malformed,
               oversized=scan.oversized, partial_tail=scan.partial_tail)

    try:
        conn.execute(
            "INSERT INTO sessions (session_id, path, parent_session, agent, title, cwd, "
            "git_branch, version, first_ts, "
            "last_ts, first_epoch, last_epoch, turns, prompts, marks, compactions, next_seq, "
            "bytes_indexed, file_size, lines_read, malformed, partial_tail, fingerprint, "
            "indexed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(session_id) DO UPDATE SET path=excluded.path, "
            "parent_session=excluded.parent_session, agent=excluded.agent, "
            "title=COALESCE(excluded.title, sessions.title), cwd=excluded.cwd, "
            "git_branch=excluded.git_branch, version=excluded.version, "
            "first_ts=COALESCE(sessions.first_ts, excluded.first_ts), "
            "first_epoch=COALESCE(sessions.first_epoch, excluded.first_epoch), "
            "last_ts=COALESCE(excluded.last_ts, sessions.last_ts), "
            "last_epoch=COALESCE(excluded.last_epoch, sessions.last_epoch), "
            "turns=sessions.turns+excluded.turns, prompts=sessions.prompts+excluded.prompts, "
            "marks=sessions.marks+excluded.marks, "
            "compactions=sessions.compactions+excluded.compactions, "
            "next_seq=excluded.next_seq, bytes_indexed=excluded.bytes_indexed, "
            "file_size=excluded.file_size, lines_read=sessions.lines_read+excluded.lines_read, "
            "malformed=sessions.malformed+excluded.malformed, "
            "partial_tail=excluded.partial_tail, fingerprint=excluded.fingerprint, "
            "indexed_at=excluded.indexed_at",
            (session_id, path, entry.get("parent"), entry.get("agent"), title, cwd, branch,
             version, first_ts, last_ts, first_epoch,
             last_epoch, turns, prompts, marks, compactions, seq, scan.consumed,
             entry.get("size"), scan.lines, scan.malformed,
             1 if scan.partial_tail else 0, fingerprint, _now()),
        )
    except Exception as exc:
        out["error"] = out["error"] or f"could not record {session_id}: {exc}"
    return out


def index(project_dir=None, full: bool = False, session=None) -> dict:
    """Bring the session index up to the end of every transcript. Never raises."""
    started = time.time()
    root = store.project_root(project_dir)
    result = {
        "ok": False,
        "librarian": "sessions",
        "project_dir": str(root),
        "mode": "full" if full else "incremental",
        "sessions_seen": 0,
        "sessions_touched": 0,
        "turns_added": 0,
        "marks_added": 0,
        "malformed_lines": 0,
        "bytes_read": 0,
        "notes": [],
    }

    where = transcripts.locate(root)
    result["transcript_dir"] = where.get("dir")
    result["transcript_dir_source"] = where.get("source")
    if not where.get("ok"):
        result["error"] = where.get("problem") or "no transcript directory could be located"
        result["elapsed_seconds"] = round(time.time() - started, 4)
        return result
    if where.get("problem"):
        result["notes"].append(where["problem"])

    # Before anything is written. store.connect_file() guards this too, so no
    # ordering of tool calls can slip past it, but the result of the refresh is
    # where a user is actually looking - and an index of conversation text that
    # git can see is the one failure they most need told to their face.
    result["gitignore"] = ig = config.ensure_ignored(root)
    if not ig.get("ok"):
        result["index_unprotected"] = True
        result["notes"].append(
            f"THE INDEX COULD NOT BE PROTECTED: teamme's ignore rule is not in place in "
            f"{ig.get('path')} ({ig.get('problem')}). This index holds conversation text and "
            f"git can currently see it. Fix that file, or delete "
            f"{store.sessions_dir(root)} until you can.")
    else:
        for note in ig.get("notes") or []:
            result["notes"].append(note)

    files = transcripts.list_sessions(where["dir"])
    result["sessions_seen"] = len(files)
    if session:
        wanted = str(session).strip()
        files = [f for f in files if f["session_id"] == wanted]
        if not files:
            result["error"] = (
                f"no transcript named '{wanted}' in {where['dir']}. Ask the `sessions` query for "
                f"the ids that are there.")
            result["elapsed_seconds"] = round(time.time() - started, 4)
            return result
    if not files:
        result["ok"] = True
        result["notes"].append(
            f"{where['dir']} holds no transcripts yet - indexed nothing")
        result["elapsed_seconds"] = round(time.time() - started, 4)
        result.update(counts_only(root))
        return result

    conn = None
    try:
        with index_lock(root):
            conn, reset_note = connect_or_reset(root)
            if reset_note:
                result["notes"].append(reset_note)
                full = True
            bytes_before = 0
            try:
                row = conn.execute("SELECT COALESCE(SUM(bytes_indexed),0) AS n "
                                   "FROM sessions").fetchone()
                bytes_before = row["n"] or 0
            except Exception:
                pass
            for entry in files:
                with conn:
                    one = index_one(conn, entry, full=full)
                result["turns_added"] += one["turns_added"]
                result["marks_added"] += one["marks_added"]
                result["malformed_lines"] += one["malformed"]
                if one["turns_added"] or one["mode"] == "full":
                    result["sessions_touched"] += 1
                if one.get("reset"):
                    result["notes"].append(one["reset"])
                if one.get("error"):
                    result["notes"].append(one["error"])
                if one.get("oversized"):
                    result["notes"].append(
                        f"{one['session_id']}: {one['oversized']} line(s) were too large to parse "
                        f"and were skipped")
                if one.get("partial_tail"):
                    result["notes"].append(
                        f"{one['session_id']}: the last line is incomplete - the session is still "
                        f"being written. It was left unconsumed and the next refresh picks it up.")
            with conn:
                _set_meta(conn, META_SCHEMA_VERSION, str(SCHEMA_VERSION))
                _set_meta(conn, META_LAST_REFRESH, _now())
                _set_meta(conn, META_TRANSCRIPT_DIR, where["dir"])
            result.update(_counts(conn))
            result["bytes_read"] = max((result.get("bytes_indexed") or 0) - bytes_before, 0)
            result["ok"] = True
        if result["malformed_lines"]:
            result["notes"].append(
                f"{result['malformed_lines']} unparsable line(s) across all transcripts were "
                f"skipped")
    except Exception as exc:
        result["error"] = f"indexing failed: {exc}"
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    result["elapsed_seconds"] = round(time.time() - started, 4)
    return result


def _counts(conn) -> dict:
    out = {}
    for table in ("sessions", "turns", "marks"):
        try:
            out[table] = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
        except Exception:
            out[table] = None
    try:
        row = conn.execute(
            "SELECT MIN(first_epoch) AS lo, MAX(last_epoch) AS hi, "
            "COALESCE(SUM(bytes_indexed),0) AS b, COALESCE(SUM(compactions),0) AS c "
            "FROM sessions").fetchone()
        out["oldest_epoch"], out["newest_epoch"] = row["lo"], row["hi"]
        out["bytes_indexed"], out["compactions"] = row["b"], row["c"]
    except Exception:
        out["oldest_epoch"] = out["newest_epoch"] = None
        out["bytes_indexed"] = out["compactions"] = None
    return out


def counts_only(project_dir=None) -> dict:
    conn = None
    try:
        conn, _ = connect_or_reset(project_dir)
        return _counts(conn)
    except Exception:
        return {}
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def status(project_dir=None) -> dict:
    """What the session index holds, and how far behind the files it is."""
    root = store.project_root(project_dir)
    out = {
        "librarian": "sessions",
        "project_dir": str(root),
        "db": str(db_path(root)),
        "record": "the transcripts themselves - this librarian keeps no second copy",
        "has_data": False,
    }
    where = transcripts.locate(root)
    out["transcript_dir"] = where.get("dir")
    out["transcript_dir_source"] = where.get("source")
    out["searched"] = where.get("searched")
    if not where.get("ok"):
        out["error"] = where.get("problem")
    elif where.get("problem"):
        out["note"] = where["problem"]

    files = transcripts.list_sessions(where["dir"]) if where.get("ok") else []
    out["transcripts_on_disk"] = len(files)
    out["bytes_on_disk"] = sum(f["size"] for f in files)

    if not db_path(root).exists():
        out["db_exists"] = False
        out["not_indexed_yet"] = True
        if where.get("ok"):
            out["note"] = (
                f"nothing has been indexed here yet. {len(files)} transcript(s) totalling "
                f"{out['bytes_on_disk']} byte(s) are on disk and a refresh would read them.")
        return out
    out["db_exists"] = True
    conn = None
    try:
        conn, reset_note = connect_or_reset(root)
        if reset_note:
            out["note"] = reset_note
        out.update(_counts(conn))
        out["schema_version"] = _get_meta(conn, META_SCHEMA_VERSION)
        out["last_refresh_at"] = _get_meta(conn, META_LAST_REFRESH)
        out["has_data"] = bool(out.get("turns"))
        indexed = {}
        try:
            for row in conn.execute("SELECT session_id, bytes_indexed FROM sessions"):
                indexed[row["session_id"]] = row["bytes_indexed"] or 0
        except Exception:
            pass
        behind = sum(max(f["size"] - indexed.get(f["session_id"], 0), 0) for f in files)
        out["bytes_behind"] = behind
        out["sessions_not_indexed"] = len([f for f in files if f["session_id"] not in indexed])
    except Exception as exc:
        out["error"] = f"could not read the index: {exc}"
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return out


# --------------------------------------------------------------------------- #
# queries - bounded, named, parameterized. Never arbitrary SQL, never a dump.
# --------------------------------------------------------------------------- #

QUERY_NAMES = ("sessions", "search_turns", "window", "compaction")

SEARCH_CAVEATS = (
    transcripts.TOOL_RESULTS_NOT_INDEXED,
    transcripts.THINKING_NOT_RECORDED,
    "these are MARK POINTS, not the conversation. Each row says where something was said; fetch "
    "the text with the `window` query, one mark at a time.",
)


def _snippet(text: str, needle: str) -> dict:
    """The text either side of the first hit - never the turn."""
    text = text or ""
    low, low_needle = text.lower(), (needle or "").lower()
    at = low.find(low_needle) if low_needle else -1
    if at < 0:
        return {"snippet": transcripts._clip(text, MAX_SNIPPET_CHARS),
                "snippet_at": None, "before": False, "after": len(text) > MAX_SNIPPET_CHARS}
    start = max(at - SNIPPET_PAD, 0)
    end = min(at + len(needle) + SNIPPET_PAD, len(text))
    body = " ".join(text[start:end].split())
    return {
        "snippet": transcripts._clip(body, MAX_SNIPPET_CHARS),
        "snippet_at": at,
        "before": start > 0,
        "after": end < len(text),
    }


def q_sessions(conn, args: dict, limit: int, project_dir) -> dict:
    """This project's sessions, newest first, with turn counts and date span."""
    where, params = [], []
    parent = args.get("parent")
    if isinstance(parent, str) and parent.strip():
        where.append("parent_session = ?")            # the subagents of one session
        params.append(parent.strip())
    elif not args.get("include_subagents"):
        # Main threads only by default. A session that dispatched forty
        # specialists would otherwise bury itself under its own children; the
        # count is reported on the parent row instead, and `parent` fetches them.
        where.append("parent_session IS NULL")
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    rows = [dict(r) for r in conn.execute(
        "SELECT session_id, parent_session, agent, title, first_ts, last_ts, first_epoch, "
        "last_epoch, turns, prompts, marks, compactions, file_size, bytes_indexed, partial_tail, "
        "git_branch, version FROM sessions" + clause +
        " ORDER BY COALESCE(last_epoch, 0) DESC, session_id LIMIT ?",
        params + [limit + 1]).fetchall()]
    truncated = len(rows) > limit
    rows = rows[:limit]
    kids = {}
    try:
        for r in conn.execute(
                "SELECT parent_session AS p, COUNT(*) AS n, COALESCE(SUM(turns),0) AS t "
                "FROM sessions WHERE parent_session IS NOT NULL GROUP BY parent_session"):
            kids[r["p"]] = (r["n"], r["t"])
    except Exception:
        pass
    for r in rows:
        n, t = kids.get(r["session_id"], (0, 0))
        r["subagent_threads"] = n
        r["subagent_turns"] = t
        size = r.get("file_size") or 0
        done = r.get("bytes_indexed") or 0
        r["fully_indexed"] = bool(size and done >= size)
        r["bytes_behind"] = max(size - done, 0)
        r["still_being_written"] = bool(r.pop("partial_tail", 0))
        r["span_days"] = None
        if r.get("first_epoch") and r.get("last_epoch"):
            r["span_days"] = round((r["last_epoch"] - r["first_epoch"]) / 86400.0, 2)
    return {"ok": True, "query": "sessions", "rows": rows, "count": len(rows),
            "limit": limit, "truncated": truncated,
            "empty_reason": (None if rows else
                             "the index holds no sessions. Run teamme_librarian_refresh "
                             "{\"librarian\": \"sessions\"} first.")}


def q_search_turns(conn, args: dict, limit: int, project_dir) -> dict:
    """A literal substring of what was typed or said, answered as mark points."""
    text = args.get("text")
    if not isinstance(text, str) or not text.strip():
        return {"ok": False,
                "error": "search_turns needs `text` - a literal substring. Wildcards are not "
                         "special and nothing is interpreted as a pattern."}
    needle = text.strip()
    where = ["t.text LIKE ? ESCAPE '\\'"]
    params = ["%" + store._like(needle) + "%"]

    session = args.get("session")
    if isinstance(session, str) and session.strip():
        where.append("t.session_id = ?")
        params.append(session.strip())
    kind = args.get("kind")
    if isinstance(kind, str) and kind.strip():
        if kind.strip() not in transcripts.MARK_KINDS:
            return {"ok": False,
                    "error": f"unknown mark kind '{kind}'. One of: "
                             f"{', '.join(transcripts.MARK_KINDS)}"}
        where.append("m.kind = ?")
        params.append(kind.strip())
    if args.get("main_thread_only"):
        # A subagent thread is a separate FILE, so the test is the session's
        # parentage, not the per-record sidechain flag.
        where.append("t.session_id IN (SELECT session_id FROM sessions "
                     "WHERE parent_session IS NULL)")

    sql = (
        "SELECT m.session_id AS session_id, m.seq AS seq, m.pos AS pos, m.kind AS kind, "
        "m.label AS label, m.ts AS ts, t.role AS role, "
        "t.text AS _text, s.title AS session_title, s.agent AS agent, "
        "s.parent_session AS parent_session "
        "FROM marks m JOIN turns t ON t.session_id = m.session_id AND t.seq = m.seq "
        "LEFT JOIN sessions s ON s.session_id = m.session_id "
        "WHERE " + " AND ".join(where) +
        " ORDER BY COALESCE(m.epoch, 0) DESC, m.session_id, m.seq DESC, m.pos LIMIT ?"
    )
    rows = [dict(r) for r in conn.execute(sql, params + [limit + 1]).fetchall()]
    truncated = len(rows) > limit
    rows = rows[:limit]
    for r in rows:
        body = r.pop("_text", "") or ""
        r.update(_snippet(body, needle))
        r["sidechain"] = bool(r.get("parent_session"))
        r["fetch_with"] = {"query": "window", "session": r["session_id"], "seq": r["seq"]}

    hits = None
    try:
        hits = conn.execute(
            "SELECT COUNT(*) AS n FROM turns t WHERE t.text LIKE ? ESCAPE '\\'",
            ["%" + store._like(needle) + "%"]).fetchone()["n"]
    except Exception:
        pass

    return {
        "ok": True, "query": "search_turns", "text": needle, "rows": rows,
        "count": len(rows), "limit": limit, "truncated": truncated,
        "turns_matching": hits,
        "caveats": list(SEARCH_CAVEATS),
        "empty_reason": (None if rows else
                         f"no indexed turn contains '{needle}'. It is a literal substring match, "
                         f"case-insensitive; try fewer words. Note the caveats below - tool output "
                         f"is not indexed, so text that only ever appeared in a file read or a "
                         f"command's output is not searchable."),
    }


def q_window(conn, args: dict, limit: int, project_dir) -> dict:
    """A bounded slice of one session around one mark point."""
    session = args.get("session")
    if not isinstance(session, str) or not session.strip():
        return {"ok": False, "error": "window needs `session` (a session id from the `sessions` "
                                      "or `search_turns` query)"}
    session = session.strip()
    try:
        seq = int(args.get("seq"))
    except Exception:
        return {"ok": False, "error": "window needs `seq` - the mark point's position, as "
                                      "returned by search_turns or compaction"}
    before, after = _span(args.get("before")), _span(args.get("after"))

    row = conn.execute("SELECT * FROM sessions WHERE session_id = ?", (session,)).fetchone()
    if row is None:
        return {"ok": False,
                "error": f"no session '{session}' in the index. Ask the `sessions` query for the "
                         f"ids that are there."}
    lo, hi = max(seq - before, 0), seq + after
    rows = [dict(r) for r in conn.execute(
        "SELECT seq, role, ts, sidechain, text, text_chars, truncated FROM turns "
        "WHERE session_id = ? AND seq BETWEEN ? AND ? ORDER BY seq",
        (session, lo, hi)).fetchall()]
    if not rows:
        return {"ok": False,
                "error": f"session '{session}' has no turn at seq {seq} (it holds "
                         f"{row['turns']} turn(s), numbered 0..{max((row['next_seq'] or 1) - 1, 0)})"}

    marks = {}
    try:
        for m in conn.execute(
                "SELECT seq, pos, kind, label, detail FROM marks "
                "WHERE session_id = ? AND seq BETWEEN ? AND ? ORDER BY seq, pos",
                (session, lo, hi)):
            marks.setdefault(m["seq"], []).append(
                {"kind": m["kind"], "label": m["label"], "detail": m["detail"]})
    except Exception:
        pass

    total, cut_at = 0, None
    out = []
    for r in rows:
        body = r.get("text") or ""
        per = body
        r["turn_truncated"] = False
        if len(per) > MAX_WINDOW_TURN_CHARS:
            per = per[:MAX_WINDOW_TURN_CHARS]
            r["turn_truncated"] = True
        if total + len(per) > MAX_WINDOW_TOTAL_CHARS:
            cut_at = r["seq"]
            break
        total += len(per)
        r["text"] = per
        r["sidechain"] = bool(r.get("sidechain"))
        r["marks"] = marks.get(r["seq"], [])
        r["is_anchor"] = (r["seq"] == seq)
        out.append(r)

    return {
        "ok": True, "query": "window", "session": session,
        "session_title": row["title"], "seq": seq,
        "range": [lo, out[-1]["seq"] if out else lo],
        "asked_range": [lo, hi],
        "rows": out, "count": len(out), "chars": total,
        "truncated": bool(cut_at is not None or any(r["turn_truncated"] for r in out)),
        "stopped_at_seq": cut_at,
        "caps": {"per_turn_chars": MAX_WINDOW_TURN_CHARS,
                 "total_chars": MAX_WINDOW_TOTAL_CHARS,
                 "max_turns_either_side": MAX_WINDOW},
        "caveats": [transcripts.TOOL_RESULTS_NOT_INDEXED,
                    transcripts.THINKING_NOT_RECORDED],
    }


def q_compaction(conn, args: dict, limit: int, project_dir) -> dict:
    """What fell out of context at the most recent compaction.

    Derived entirely from the transcript's own compact_boundary record - no
    PreCompact hook is involved, and none is needed.
    """
    session = args.get("session")
    params, where = [], ["m.kind = 'compaction'"]
    if isinstance(session, str) and session.strip():
        where.append("m.session_id = ?")
        params.append(session.strip())
    row = conn.execute(
        "SELECT m.session_id AS session_id, m.seq AS seq, m.ts AS ts, m.epoch AS epoch, "
        "m.label AS label, m.extra AS extra, s.title AS title "
        "FROM marks m LEFT JOIN sessions s ON s.session_id = m.session_id "
        "WHERE " + " AND ".join(where) +
        " ORDER BY COALESCE(m.epoch, 0) DESC, m.seq DESC LIMIT 1", params).fetchone()

    if row is None:
        total = conn.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()["n"]
        return {
            "ok": True, "query": "compaction", "found": False, "rows": [],
            "count": 0, "limit": limit, "truncated": False,
            "empty_reason": (
                f"no compaction is recorded in {'that session' if session else 'any indexed session'}"
                f" ({total} session(s) indexed). Either none has happened, or the transcript has "
                f"not been refreshed since it did."),
        }

    try:
        extra = json.loads(row["extra"]) if row["extra"] else {}
    except Exception:
        extra = {}
    sid, seq = row["session_id"], row["seq"]

    span = conn.execute(
        "SELECT MIN(seq) AS lo, COUNT(*) AS turns, MIN(ts) AS first_ts, MAX(ts) AS last_ts "
        "FROM turns WHERE session_id = ? AND seq < ?", (sid, seq)).fetchone()
    by_kind = {r["kind"]: r["n"] for r in conn.execute(
        "SELECT kind, COUNT(*) AS n FROM marks WHERE session_id = ? AND seq < ? GROUP BY kind",
        (sid, seq))}
    after = conn.execute(
        "SELECT COUNT(*) AS n FROM turns WHERE session_id = ? AND seq > ?",
        (sid, seq)).fetchone()["n"]

    # The SPINE of what was dropped: the prompts and the recap, newest last.
    # Not the answers, and not the tool calls - those are what `window` is for.
    cap = min(limit, MAX_LOST_MARKS)
    spine = [dict(r) for r in conn.execute(
        "SELECT seq, pos, kind, label, ts FROM marks "
        "WHERE session_id = ? AND seq < ? AND kind IN ('prompt','recap','compaction') "
        "ORDER BY seq DESC, pos LIMIT ?", (sid, seq, cap + 1)).fetchall()]
    spine_truncated = len(spine) > cap
    spine = list(reversed(spine[:cap]))
    for m in spine:
        m["fetch_with"] = {"query": "window", "session": sid, "seq": m["seq"]}

    return {
        "ok": True, "query": "compaction", "found": True,
        "session": sid, "session_title": row["title"], "seq": seq, "ts": row["ts"],
        "trigger": extra.get("trigger"),
        "tokens_before": extra.get("preTokens"),
        "tokens_after": extra.get("postTokens"),
        "tokens_dropped": extra.get("cumulativeDroppedTokens"),
        "turns_before": span["turns"] if span else 0,
        "turns_after": after,
        "first_ts": span["first_ts"] if span else None,
        "last_ts": span["last_ts"] if span else None,
        "marks_before_by_kind": by_kind,
        "rows": spine, "count": len(spine), "limit": cap, "truncated": spine_truncated,
        "caveats": [
            "this boundary comes from the transcript's own compact_boundary record - the file "
            "records the compaction, so no hook is needed to notice one.",
            "everything listed here is still ON DISK and indexed; it is only out of the model's "
            "context. Fetch any of it with the `window` query at the seq given.",
            transcripts.TOOL_RESULTS_NOT_INDEXED,
        ],
    }


QUERIES = {
    "sessions": q_sessions,
    "search_turns": q_search_turns,
    "window": q_window,
    "compaction": q_compaction,
}


def query(conn, name: str, args: dict = None, project_dir=None) -> dict:
    """One of QUERY_NAMES, bound-parameterized and row-capped. Never raises."""
    args = args if isinstance(args, dict) else {}
    name = (name or "").strip()
    fn = QUERIES.get(name)
    if fn is None:
        return {"ok": False,
                "error": f"unknown session query '{name}'. One of: {', '.join(QUERY_NAMES)}"}
    limit = _clamp(args.get("limit") or args.get("n"))
    try:
        return fn(conn, args, limit, project_dir)
    except Exception as exc:
        return {"ok": False, "error": f"query failed: {exc}"}
