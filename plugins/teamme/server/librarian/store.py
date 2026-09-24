#!/usr/bin/env python3
"""The librarian store: append-only JSONL as the record, SQLite as the index.

Layout inside a project (created at runtime, never copied in by an installer):

  .claude/librarians/history/commits.jsonl   the record. One JSON object per
                                             line, append-only, mergeable, and
                                             the only thing worth committing.
  .claude/librarians/index.db                the index. Derived, disposable,
                                             always gitignored.

`rebuild()` reconstructs the database from the JSONL alone with no git access.
Everything else here is arranged so that stays true: nothing lands in SQLite
that was not first written as a text record.

Concurrency is real - two refreshes can race. The JSONL append takes the same
best-effort O_EXCL lockfile worklog.py uses, with the same fail-open rule: a
lock that cannot be taken within the bound is abandoned and the write proceeds.
A refresh that refuses is worse than a rare duplicate, and duplicates are
harmless here because `hash` is a primary key and every insert is idempotent.

Nothing in this module raises at the boundary: callers get a result dict.
"""

import json
import os
import pathlib
import re
import sqlite3
import time

SCHEMA_VERSION = 1

DEFAULT_LIMIT = 30
MAX_LIMIT = 200

BUSY_TIMEOUT_MS = 5000
LOCK_STALE_SECONDS = 30     # a lock older than this is assumed to be a crashed process
LOCK_TRIES = 40             # bounded retries...
LOCK_WAIT = 0.05            # ...roughly two seconds, then proceed unlocked

META_LAST_INDEXED = "history.last_indexed_hash"
META_SCHEMA_VERSION = "schema_version"
META_LAST_REFRESH = "history.last_refresh_at"

HEX = re.compile(r"^[0-9a-f]{7,64}$")


# --------------------------------------------------------------------------- #
# paths
# --------------------------------------------------------------------------- #

def project_root(project_dir=None) -> pathlib.Path:
    raw = project_dir or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    return pathlib.Path(raw).expanduser().resolve()


def librarians_dir(project_dir=None) -> pathlib.Path:
    return project_root(project_dir) / ".claude" / "librarians"


def db_path(project_dir=None) -> pathlib.Path:
    return librarians_dir(project_dir) / "index.db"


def history_dir(project_dir=None) -> pathlib.Path:
    return librarians_dir(project_dir) / "history"


def commits_jsonl(project_dir=None) -> pathlib.Path:
    return history_dir(project_dir) / "commits.jsonl"


# --------------------------------------------------------------------------- #
# the lock (worklog.py's pattern, generalized to a path)
# --------------------------------------------------------------------------- #

class lock:
    """Best-effort mutual exclusion around an append or a rewrite.

    O_CREAT | O_EXCL rather than fcntl, which is POSIX-only. Never raises and
    never refuses the caller: an unavailable lock is abandoned after
    LOCK_TRIES * LOCK_WAIT seconds and the caller writes anyway, and a lockfile
    left behind by a process that died holding it expires after
    LOCK_STALE_SECONDS.
    """

    def __init__(self, path, enabled: bool = True):
        self.path = pathlib.Path(path)
        self.enabled = enabled
        self.held = False

    def __enter__(self):
        if not self.enabled:
            return self
        p = self.path
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
                self.path.unlink()
            except Exception:
                pass
            self.held = False
        return False


def jsonl_lock(project_dir=None) -> lock:
    return lock(commits_jsonl(project_dir).with_name("commits.lock"))


# --------------------------------------------------------------------------- #
# schema
# --------------------------------------------------------------------------- #

# commit_parents and files_changed are EDGE ROWS, not JSON blobs, on purpose:
# phase 3 wants recursive CTEs over a dependency graph, and a CTE cannot walk a
# blob. `commits.parents` keeps the flat text form for display; the edge table
# is what a query traverses.
SCHEMA = """
CREATE TABLE IF NOT EXISTS commits (
    hash         TEXT PRIMARY KEY,
    short_hash   TEXT,
    author       TEXT,
    author_email TEXT,
    date         TEXT,
    epoch        INTEGER,
    subject      TEXT,
    body         TEXT,
    parents      TEXT,
    indexed_at   TEXT
);
CREATE TABLE IF NOT EXISTS files_changed (
    hash      TEXT NOT NULL,
    path      TEXT NOT NULL,
    additions INTEGER,
    deletions INTEGER
);
CREATE TABLE IF NOT EXISTS commit_parents (
    hash        TEXT NOT NULL,
    parent_hash TEXT NOT NULL,
    position    INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE INDEX IF NOT EXISTS idx_files_path ON files_changed(path);
CREATE INDEX IF NOT EXISTS idx_files_hash ON files_changed(hash);
CREATE INDEX IF NOT EXISTS idx_commits_epoch ON commits(epoch);
CREATE INDEX IF NOT EXISTS idx_parents_hash ON commit_parents(hash);
CREATE INDEX IF NOT EXISTS idx_parents_parent ON commit_parents(parent_hash);
CREATE UNIQUE INDEX IF NOT EXISTS idx_files_unique ON files_changed(hash, path);
CREATE UNIQUE INDEX IF NOT EXISTS idx_parents_unique ON commit_parents(hash, parent_hash);
"""


def connect(project_dir=None, create: bool = True) -> sqlite3.Connection:
    """An open connection with the schema present. Raises only if SQLite itself
    cannot open the file; callers wrap it."""
    p = db_path(project_dir)
    if create:
        p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=BUSY_TIMEOUT_MS / 1000.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    except Exception:
        pass
    try:
        # WAL lets a query read while a refresh writes. Not available on every
        # filesystem (network mounts especially) - fall back silently.
        conn.execute("PRAGMA journal_mode=WAL")
    except Exception:
        pass
    ensure_schema(conn)
    return conn


CORRUPT_MARKERS = ("not a database", "malformed", "encrypted", "unsupported file format")


def connect_or_reset(project_dir=None):
    """`connect`, but a corrupt index.db is thrown away instead of raising.

    The database is derived and disposable by design, so a file that SQLite
    cannot read is not an error condition to report - it is a file to delete and
    rebuild from commits.jsonl. Returns (connection, note): `note` is set when
    the file had to be discarded, so the caller can say so out loud rather than
    silently losing rows. A merely LOCKED database is not corrupt and is never
    deleted.
    """
    try:
        return connect(project_dir), None
    except sqlite3.OperationalError:
        raise
    except sqlite3.DatabaseError as exc:
        if not any(m in str(exc).lower() for m in CORRUPT_MARKERS):
            raise
    path = db_path(project_dir)
    for suffix in ("", "-wal", "-shm"):
        try:
            pathlib.Path(str(path) + suffix).unlink()
        except Exception:
            pass
    conn = connect(project_dir)
    return conn, (f"{path} could not be read as a database and was discarded - it is derived "
                  f"and disposable, and the next refresh rebuilds it from the text record")


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    if get_meta(conn, META_SCHEMA_VERSION) is None:
        set_meta(conn, META_SCHEMA_VERSION, str(SCHEMA_VERSION))
    conn.commit()


def get_meta(conn: sqlite3.Connection, key: str, default=None):
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    except Exception:
        return default
    return row["value"] if row is not None else default


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )


# --------------------------------------------------------------------------- #
# records
# --------------------------------------------------------------------------- #

def valid_commit_record(rec) -> bool:
    return (
        isinstance(rec, dict)
        and rec.get("kind", "commit") == "commit"
        and isinstance(rec.get("hash"), str)
        and bool(HEX.match(rec["hash"]))
    )


def insert_commit(conn: sqlite3.Connection, rec: dict) -> int:
    """Idempotent: re-inserting a record already present replaces it rather than
    failing, so a duplicated JSONL line (the cost of the fail-open lock) is
    harmless. Returns the number of file rows written."""
    parents = rec.get("parents") or []
    if isinstance(parents, str):
        parents = parents.split()
    conn.execute(
        "INSERT INTO commits (hash, short_hash, author, author_email, date, epoch, "
        "subject, body, parents, indexed_at) VALUES (?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(hash) DO UPDATE SET short_hash=excluded.short_hash, "
        "author=excluded.author, author_email=excluded.author_email, date=excluded.date, "
        "epoch=excluded.epoch, subject=excluded.subject, body=excluded.body, "
        "parents=excluded.parents, indexed_at=excluded.indexed_at",
        (
            rec.get("hash"),
            rec.get("short_hash"),
            rec.get("author"),
            rec.get("author_email"),
            rec.get("date"),
            rec.get("epoch"),
            rec.get("subject"),
            rec.get("body"),
            " ".join(parents),
            rec.get("indexed_at"),
        ),
    )
    conn.execute("DELETE FROM files_changed WHERE hash = ?", (rec["hash"],))
    conn.execute("DELETE FROM commit_parents WHERE hash = ?", (rec["hash"],))
    files = rec.get("files") or []
    rows = 0
    for f in files:
        if not isinstance(f, dict) or not isinstance(f.get("path"), str):
            continue
        conn.execute(
            "INSERT OR REPLACE INTO files_changed (hash, path, additions, deletions) "
            "VALUES (?,?,?,?)",
            (rec["hash"], f["path"], f.get("additions"), f.get("deletions")),
        )
        rows += 1
    for i, p in enumerate(parents):
        if isinstance(p, str) and HEX.match(p):
            conn.execute(
                "INSERT OR REPLACE INTO commit_parents (hash, parent_hash, position) "
                "VALUES (?,?,?)",
                (rec["hash"], p, i),
            )
    return rows


def append_and_insert(project_dir, conn, records) -> dict:
    """The incremental write: text first, then the index, both under one lock.

    Taking the lock across "which of these are new?" -> append -> insert is what
    keeps two simultaneous refreshes from writing the same commit twice. It is
    still best-effort: the lock fails open after roughly two seconds, and a
    refresh that refuses is worse than a duplicate line. A duplicate costs
    nothing but bytes - `hash` is a primary key, every insert is idempotent, and
    a full refresh rewrites the file without them.
    """
    records = [r for r in records if valid_commit_record(r)]
    if not records:
        return {"appended": 0, "inserted": 0, "files": 0, "skipped_existing": 0}
    path = commits_jsonl(project_dir)
    with jsonl_lock(project_dir) as lk:
        fresh, existing = [], 0
        for r in records:
            try:
                row = conn.execute("SELECT 1 FROM commits WHERE hash = ?", (r["hash"],)).fetchone()
            except Exception:
                row = None
            if row is None:
                fresh.append(r)
            else:
                existing += 1
        if not fresh:
            return {"appended": 0, "inserted": 0, "files": 0,
                    "skipped_existing": existing, "locked": lk.held}
        text = "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in fresh)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(str(path), "a", encoding="utf-8") as fh:
                fh.write(text)
        except Exception as exc:
            return {"appended": 0, "inserted": 0, "files": 0,
                    "error": f"could not append to {path}: {exc}"}
        files = 0
        try:
            with conn:
                for r in fresh:
                    files += insert_commit(conn, r)
        except Exception as exc:
            # The text record is already written, so nothing is lost: the next
            # refresh finds an empty-for-these-hashes index and replays it.
            return {"appended": len(fresh), "inserted": 0, "files": 0,
                    "error": f"indexed nothing - the database write failed: {exc}"}
    return {"appended": len(fresh), "inserted": len(fresh), "files": files,
            "skipped_existing": existing, "locked": lk.held}


def rewrite_records(project_dir, records) -> dict:
    """Replace the commit records wholesale (a full reindex), preserving any
    record whose `kind` is not "commit" - phase 2's reasoned records live in the
    same file and must survive a mechanical reindex. Atomic: temp file in the
    same directory plus os.replace, so a reader never sees a half-written file.
    """
    path = commits_jsonl(project_dir)
    preserved = [r for r in read_records(project_dir)["records"] if r.get("kind", "commit") != "commit"]
    seen, deduped = set(), []
    for r in records:
        if not valid_commit_record(r) or r["hash"] in seen:
            continue
        seen.add(r["hash"])
        deduped.append(r)
    records = deduped
    text = "".join(
        json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in preserved + records
    )
    with jsonl_lock(project_dir):
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
            tmp.write_text(text, encoding="utf-8")
            os.replace(str(tmp), str(path))
        except Exception as exc:
            return {"written": 0, "error": f"could not rewrite {path}: {exc}"}
    return {"written": len(records), "preserved": len(preserved)}


def read_records(project_dir) -> dict:
    """Every parsable record in the JSONL. A corrupt line is counted and skipped,
    never fatal - the same rule the work log follows for a corrupt ledger."""
    path = commits_jsonl(project_dir)
    out, skipped = [], 0
    try:
        with open(str(path), "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    skipped += 1
                    continue
                if isinstance(rec, dict):
                    out.append(rec)
                else:
                    skipped += 1
    except FileNotFoundError:
        return {"records": [], "skipped": 0, "exists": False}
    except Exception:
        return {"records": out, "skipped": skipped, "exists": True}
    return {"records": out, "skipped": skipped, "exists": True}


# --------------------------------------------------------------------------- #
# rebuild - the property everything else rests on
# --------------------------------------------------------------------------- #

def rebuild(project_dir=None, conn: sqlite3.Connection = None) -> dict:
    """Reconstruct the ENTIRE database from commits.jsonl, with no git access.

    This is what makes index.db disposable: delete it, run this, and the index
    is byte-for-byte equivalent in content. If it ever stops being true, the
    committed artifact has stopped being the record and the binary has started
    being the source of truth - which is the thing that cannot be merged.
    """
    started = time.time()
    own = conn is None
    try:
        if own:
            conn = connect(project_dir)
        data = read_records(project_dir)
        commits = [r for r in data["records"] if valid_commit_record(r)]
        with conn:
            conn.execute("DELETE FROM files_changed")
            conn.execute("DELETE FROM commit_parents")
            conn.execute("DELETE FROM commits")
            conn.execute("DELETE FROM meta")
            set_meta(conn, META_SCHEMA_VERSION, str(SCHEMA_VERSION))
            for rec in commits:
                insert_commit(conn, rec)
            if commits:
                set_meta(conn, META_LAST_INDEXED, commits[-1]["hash"])
        n = counts(conn)
        return {
            "ok": True,
            "source": str(commits_jsonl(project_dir)),
            "records_read": len(data["records"]),
            # commit_records counts LINES replayed; commits counts distinct rows.
            # They differ when the JSONL carries a duplicate - which it can, since
            # the append lock fails open - and conflating them would report a
            # row count that is simply wrong.
            "commit_records": len(commits),
            "commits": n.get("commits"),
            "files_changed": n.get("files_changed"),
            "skipped_lines": data["skipped"],
            "last_indexed_hash": commits[-1]["hash"] if commits else None,
            "elapsed_seconds": round(time.time() - started, 4),
        }
    except Exception as exc:
        return {"ok": False, "error": f"rebuild failed: {exc}",
                "elapsed_seconds": round(time.time() - started, 4)}
    finally:
        if own and conn is not None:
            try:
                conn.close()
            except Exception:
                pass


# --------------------------------------------------------------------------- #
# counts
# --------------------------------------------------------------------------- #

def counts(conn: sqlite3.Connection) -> dict:
    out = {}
    for table in ("commits", "files_changed", "commit_parents"):
        try:
            out[table] = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
        except Exception:
            out[table] = None
    try:
        row = conn.execute("SELECT MIN(epoch) AS lo, MAX(epoch) AS hi FROM commits").fetchone()
        out["oldest_epoch"], out["newest_epoch"] = row["lo"], row["hi"]
    except Exception:
        out["oldest_epoch"] = out["newest_epoch"] = None
    return out


# --------------------------------------------------------------------------- #
# queries - a bounded, parameterized set, never arbitrary SQL
# --------------------------------------------------------------------------- #

QUERY_NAMES = ("recent", "commits_touching", "files_in_commit", "commits_between",
               "search_subjects", "commit_detail")

# The list queries return the SUBJECT only, deliberately: a body is unbounded
# text and thirty of them is the dump this design exists to avoid. The body is
# reachable through exactly one query, commit_detail, one commit at a time and
# capped there.
COMMIT_COLUMNS = "hash, short_hash, author, author_email, date, subject, parents"
COMMIT_COLUMNS_C = ", ".join("c." + c for c in COMMIT_COLUMNS.split(", "))


# How much of a commit body one commit_detail may return, and how many
# candidates an ambiguous abbreviation may list. Both are caps, not errors: the
# answer is truncated and SAYS it was truncated.
MAX_BODY_CHARS = 4000
MAX_AMBIGUOUS = 10


def _like(text: str) -> str:
    """Escape LIKE wildcards so a search for '100%' is a literal search."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _clamp(limit) -> int:
    try:
        n = int(limit)
    except Exception:
        return DEFAULT_LIMIT
    if n <= 0:
        return DEFAULT_LIMIT
    return min(n, MAX_LIMIT)


def _epoch(value: str):
    """A date bound from 'YYYY-MM-DD', an ISO timestamp, or a bare epoch."""
    from datetime import datetime, timezone
    s = str(value).strip()
    if re.fullmatch(r"\d{9,11}", s):
        return int(s)
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def _rel_path(project_dir, path: str) -> str:
    """An absolute path inside the project becomes the repo-relative form git
    recorded. Anything else is left exactly as given."""
    p = str(path).strip()
    try:
        cand = pathlib.Path(p)
        if cand.is_absolute():
            return str(cand.resolve().relative_to(project_root(project_dir)))
    except Exception:
        pass
    return p[2:] if p.startswith("./") else p


def commit_detail(conn: sqlite3.Connection, args: dict, limit: int) -> dict:
    """One commit in full - including the body, which is where the "why" lives
    and which no list query returns.

    Bounded like everything else here: one commit, a body capped at
    MAX_BODY_CHARS with the cap reported, and its file rows capped at `limit`.
    An abbreviation that matches more than one commit is an error listing the
    candidates, never a silently-picked first row.
    """
    raw = str(args.get("hash") or "").strip().lower()
    if not raw or not HEX.match(raw):
        return {"ok": False,
                "error": "commit_detail needs `hash` (a full or abbreviated commit hash)"}
    try:
        rows = [dict(r) for r in conn.execute(
            f"SELECT {COMMIT_COLUMNS}, body FROM commits "
            "WHERE hash = ? OR hash LIKE ? ESCAPE '\\' ORDER BY epoch DESC, hash LIMIT ?",
            (raw, _like(raw) + "%", MAX_AMBIGUOUS + 1),
        ).fetchall()]
    except Exception as exc:
        return {"ok": False, "error": f"query failed: {exc}"}

    exact = [r for r in rows if (r.get("hash") or "") == raw]
    if exact:
        rows = exact[:1]       # a full hash is never ambiguous
    if not rows:
        return {"ok": False,
                "error": f"no commit in the index starts with '{raw}'. It may predate the index "
                         f"or postdate the last refresh - run a refresh, or ask `recent` or "
                         f"`search_subjects` for a hash that is in it"}
    if len(rows) > 1:
        shown = [r.get("short_hash") or (r.get("hash") or "")[:12] for r in rows[:MAX_AMBIGUOUS]]
        more = " (and more)" if len(rows) > MAX_AMBIGUOUS else ""
        return {"ok": False,
                "error": f"'{raw}' is ambiguous - it matches {len(shown)}{more} commits: "
                         f"{', '.join(shown)}. Give more characters of the hash"}

    row = rows[0]
    body = row.get("body") or ""
    row["body_chars"] = len(body)
    row["body_truncated"] = len(body) > MAX_BODY_CHARS
    if row["body_truncated"]:
        row["body"] = body[:MAX_BODY_CHARS]

    files, files_truncated = [], False
    try:
        got = [dict(r) for r in conn.execute(
            "SELECT path, additions, deletions FROM files_changed WHERE hash = ? "
            "ORDER BY path LIMIT ?", (row.get("hash"), limit + 1)).fetchall()]
        files_truncated = len(got) > limit
        files = got[:limit]
    except Exception:
        files = []             # a commit whose file rows cannot be read is still worth returning

    return {
        "ok": True,
        "query": "commit_detail",
        "rows": [row],
        "count": 1,
        "limit": limit,
        "files": files,
        "files_truncated": files_truncated,
        # `truncated` keeps its meaning for every caller: something was left out
        "truncated": bool(files_truncated or row["body_truncated"]),
    }


def query(conn: sqlite3.Connection, name: str, args: dict = None, project_dir=None) -> dict:
    """One of QUERY_NAMES, with bound parameters and a hard row cap.

    Arbitrary SQL is deliberately not available: it is an injection surface, and
    an unbounded dump recreates the very cost the index exists to remove.
    """
    args = args if isinstance(args, dict) else {}
    limit = _clamp(args.get("limit") or args.get("n"))
    fetch = limit + 1  # one extra row is how truncation is detected
    name = (name or "").strip()
    params = []

    if name == "commit_detail":
        return commit_detail(conn, args, limit)

    if name == "recent":
        sql = (f"SELECT {COMMIT_COLUMNS} FROM commits ORDER BY epoch DESC, hash LIMIT ?")
        params = [fetch]
    elif name == "commits_touching":
        raw = args.get("path")
        if not raw:
            return {"ok": False, "error": "commits_touching needs `path`"}
        path = _rel_path(project_dir, raw)
        sql = (
            f"SELECT {COMMIT_COLUMNS_C}, "
            "f.additions AS additions, f.deletions AS deletions, f.path AS path "
            "FROM files_changed f JOIN commits c ON c.hash = f.hash "
            "WHERE f.path = ? OR f.path LIKE ? ESCAPE '\\' "
            "ORDER BY c.epoch DESC, c.hash LIMIT ?"
        )
        params = [path, _like(path.rstrip("/")) + "/%", fetch]
    elif name == "files_in_commit":
        raw = str(args.get("hash") or "").strip().lower()
        if not raw or not HEX.match(raw):
            return {"ok": False, "error": "files_in_commit needs `hash` (a full or abbreviated commit hash)"}
        sql = (
            "SELECT f.hash AS hash, f.path AS path, f.additions AS additions, "
            "f.deletions AS deletions FROM files_changed f "
            "WHERE f.hash = ? OR f.hash LIKE ? ESCAPE '\\' ORDER BY f.path LIMIT ?"
        )
        params = [raw, _like(raw) + "%", fetch]
    elif name == "commits_between":
        since, until = args.get("since"), args.get("until")
        lo = _epoch(since) if since else None
        hi = _epoch(until) if until else None
        if since and lo is None:
            return {"ok": False, "error": f"could not read `since` as a date: {since!r}"}
        if until and hi is None:
            return {"ok": False, "error": f"could not read `until` as a date: {until!r}"}
        where, params = [], []
        if lo is not None:
            where.append("epoch >= ?")
            params.append(lo)
        if hi is not None:
            where.append("epoch <= ?")
            params.append(hi)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        sql = f"SELECT {COMMIT_COLUMNS} FROM commits{clause} ORDER BY epoch DESC, hash LIMIT ?"
        params.append(fetch)
    elif name == "search_subjects":
        text = str(args.get("text") or "").strip()
        if not text:
            return {"ok": False, "error": "search_subjects needs `text`"}
        sql = (
            f"SELECT {COMMIT_COLUMNS} FROM commits WHERE subject LIKE ? ESCAPE '\\' "
            "ORDER BY epoch DESC, hash LIMIT ?"
        )
        params = ["%" + _like(text) + "%", fetch]
    else:
        return {"ok": False,
                "error": f"unknown query '{name}'. One of: {', '.join(QUERY_NAMES)}"}

    try:
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    except Exception as exc:
        return {"ok": False, "error": f"query failed: {exc}"}
    truncated = len(rows) > limit
    return {
        "ok": True,
        "query": name,
        "rows": rows[:limit],
        "count": min(len(rows), limit),
        "limit": limit,
        "truncated": truncated,
    }
