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


def sessions_dir(project_dir=None) -> pathlib.Path:
    """The session librarian's whole footprint. Never committed - see
    librarian/config.py: it indexes a conversation, and a conversation contains
    everything anyone typed."""
    return librarians_dir(project_dir) / "sessions"


def project_root_of(path):
    """The project that owns a librarian file, derived from the PATH rather than
    from the environment.

    `.claude/librarians/` is the one piece of layout every librarian shares, so
    the owning project can be read straight off the path. Deriving it from
    CLAUDE_PROJECT_DIR or the cwd instead would let a guard protect one project
    while a different one's index was being written. Returns None for a path
    that is not under a librarians directory, and the caller does nothing.
    """
    try:
        p = pathlib.Path(path).expanduser().resolve()
    except Exception:
        return None
    for parent in p.parents:
        if parent.name == "librarians" and parent.parent.name == ".claude":
            return parent.parent.parent
    return None


def ignore_guard(path) -> dict:
    """Put teamme's .gitignore block in place before a librarian file is created.

    THE chokepoint for the privacy guarantee. Every librarian index is opened
    through connect_file() below, and every record is appended through
    append_and_insert()/rewrite_records(), so calling this here means no
    ordering of tool calls - refresh, query, status, or a librarian that does
    not exist yet - can produce an un-ignored index. The alternative, a call at
    each site, is the shape of the bug this closes: one site was missed in
    0.6.0 and an index of conversation text went un-ignored for a whole release.

    Never raises, and never refuses the caller: a .gitignore that cannot be
    written is reported (the refresh result says the index could not be
    protected) rather than being allowed to fail an index.
    """
    root = project_root_of(path)
    if root is None:
        return None
    try:
        from . import config       # deferred: config imports this module
        return config.ensure_ignored(root)
    except Exception as exc:
        return {"ok": False, "path": None, "changed": False, "wrote": [], "notes": [],
                "problem": f"teamme's ignore rule could not be put in place: {exc}"}


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


CORRUPT_MARKERS = ("not a database", "malformed", "encrypted", "unsupported file format")


def connect_file(path, schema: str = None, create: bool = True) -> sqlite3.Connection:
    """An open connection to one SQLite file, with `schema` applied.

    Generic on purpose: every librarian's index is derived and disposable in the
    same way, so the pragmas and the discard-a-corrupt-file rule below exist
    once rather than once per librarian.
    """
    p = pathlib.Path(path)
    ignore_guard(p)         # before the first byte exists - see ignore_guard above
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
    if schema:
        conn.executescript(schema)
        conn.commit()
    return conn


def connect_or_reset_file(path, schema: str = None):
    """`connect_file`, but a corrupt database is thrown away instead of raising.

    Every librarian index is derived and disposable by design, so a file SQLite
    cannot read is not an error to report - it is a file to delete and rebuild.
    Returns (connection, note): `note` is set when the file had to be discarded,
    so the caller can say so out loud rather than silently losing rows. A merely
    LOCKED database is not corrupt and is never deleted.
    """
    try:
        return connect_file(path, schema), None
    except sqlite3.OperationalError:
        raise
    except sqlite3.DatabaseError as exc:
        if not any(m in str(exc).lower() for m in CORRUPT_MARKERS):
            raise
    for suffix in ("", "-wal", "-shm"):
        try:
            pathlib.Path(str(path) + suffix).unlink()
        except Exception:
            pass
    return connect_file(path, schema), (
        f"{path} could not be read as a database and was discarded - it is derived and "
        f"disposable, and the next refresh rebuilds it")


def connect(project_dir=None, create: bool = True) -> sqlite3.Connection:
    """An open connection with the schema present. Raises only if SQLite itself
    cannot open the file; callers wrap it."""
    conn = connect_file(db_path(project_dir), None, create)
    ensure_schema(conn)
    return conn


def connect_or_reset(project_dir=None):
    """`connect`, but a corrupt index.db is thrown away instead of raising."""
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
        ignore_guard(path)      # the .db line is not a choice even here
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
    ignore_guard(path)
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
               "search_subjects", "commit_detail",
               "changes_with", "coupling_between", "hotspots")

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


# --------------------------------------------------------------------------- #
# co-change - edges read out of the commit stream, never out of source
# --------------------------------------------------------------------------- #
#
# Files that changed in the same commit are related, and how often they did is
# the weight of the edge. This needs no parser, so it works on every language in
# the repository plus its configs, docs and tests - a static index would need a
# parser per language and would silently produce nothing on any stack nobody
# wrote one for, which is the "indistinguishable from not working" failure the
# stdlib-only rule exists to prevent.
#
# It is a QUERY layer. Every edge below is computed from the files_changed rows
# the history indexer already writes; nothing here adds a table, an index pass
# or a byte of storage.

# The damping threshold. A commit touching more than this many files is treated
# as a SWEEP - a reformat, a license-header pass, a mass rename, an initial
# import - and is left out of every co-change count. One such commit couples
# every file it touched to every other, which is enough to dominate the ranking
# of a whole repository. 25 is a default, not a law: a normal change to one
# thing stays under it in most repositories, `max_files` overrides it per call,
# and EVERY co-change result reports how many commits were considered and how
# many were skipped - a ranking whose largest input is invisible cannot be
# inspected, only believed.
DEFAULT_MAX_COMMIT_FILES = 25
MAX_MAX_COMMIT_FILES = 100000

# Fewer considered commits than this and the ranking is thin enough that the
# result says so rather than reading as a finding.
YOUNG_HISTORY = 20

CO_CHANGE_CAVEATS = (
    "co-change is CORRELATION, not a call graph: these files changed in the same commits, which "
    "may mean one depends on the other, or may only mean both answer to the same cause. Say "
    "'changes with' - never 'depends on', 'imports' or 'requires'.",
    "a real dependency that never changed has NO edge here. Stable code is invisible to this "
    "signal, so a thin or empty result is not evidence that nothing is related.",
    "one shared commit is weak evidence; a row is marked `weak` when that is all there is behind "
    "it. A row with 40 shared commits and a row with 1 are not the same claim.",
)

HOTSPOT_CAVEATS = (
    "this ranks how OFTEN a path changed, not how important it is: a file at the top may be a hub, "
    "or may just be a log, a changelog or a lockfile that every change touches.",
    "it counts commits, not lines, and a path that never changed does not appear at all.",
)


def _breadth(args: dict):
    """(max_files, note). Anything unreadable falls back to the default and the
    note says so - a cap silently not applied would change every number below."""
    raw = args.get("max_files")
    if raw is None or raw == "":
        return DEFAULT_MAX_COMMIT_FILES, None
    if isinstance(raw, bool):
        # int(True) is 1, which would silently consider only single-file commits
        # and quietly empty the answer. A setting misread is worse than refused.
        return DEFAULT_MAX_COMMIT_FILES, (
            f"`max_files` was the boolean {raw!r}, not a file count - the default "
            f"{DEFAULT_MAX_COMMIT_FILES} was used instead")
    try:
        n = int(raw)
    except Exception:
        return DEFAULT_MAX_COMMIT_FILES, (
            f"`max_files` was {raw!r}, which is not a whole number - the default "
            f"{DEFAULT_MAX_COMMIT_FILES} was used instead")
    if n < 1:
        return DEFAULT_MAX_COMMIT_FILES, (
            f"`max_files` was {n}, which would consider no commit at all - the default "
            f"{DEFAULT_MAX_COMMIT_FILES} was used instead")
    if n > MAX_MAX_COMMIT_FILES:
        return MAX_MAX_COMMIT_FILES, (
            f"`max_files` was {n}; it was clamped to {MAX_MAX_COMMIT_FILES}, which in practice "
            f"damps nothing")
    if str(raw).strip() != str(n):
        # 3.9 read as 3 changes every count below it - say so rather than round
        # silently.
        return n, f"`max_files` was {raw!r} and was read as {n}"
    return n, None


def _path_terms(project_dir, raw: str, alias: str = "f"):
    """(path, sql, params) matching a file exactly or a directory by prefix - the
    same rule commits_touching follows, so `path` means one thing everywhere."""
    path = _rel_path(project_dir, raw)
    sql = f"({alias}.path = ? OR {alias}.path LIKE ? ESCAPE '\\')"
    return path, sql, [path, _like(path.rstrip("/")) + "/%"]


# `broad` is the set of sweeps; the damped universe is everything else. The
# single ? is always the FIRST parameter of any statement using either form.
#
# Two forms, because which one is faster depends on the shape of the query, and
# the difference is large enough to matter (measured on a synthetic 5,000-commit
# / 75,000-file-row index): a path-anchored query touches few rows and is 7x
# faster testing `NOT IN broad` directly (17ms vs 114ms), while a query that
# aggregates every path already reads everything and is 2x faster against the
# materialized `kept` set (113ms vs 218ms).
BROAD_CTE = "broad AS (SELECT hash FROM files_changed GROUP BY hash HAVING COUNT(*) > ?)"
KEPT_CTE = (BROAD_CTE + ", kept AS (SELECT DISTINCT hash FROM files_changed "
            "WHERE hash NOT IN (SELECT hash FROM broad))")


def _date_only(epoch):
    try:
        from datetime import datetime, timezone
        return datetime.fromtimestamp(int(epoch), timezone.utc).strftime("%Y-%m-%d")
    except Exception:
        return None


def _damping(conn: sqlite3.Connection, max_files: int, note=None) -> dict:
    """What the cap actually did to this index, in numbers, every time."""
    indexed = conn.execute("SELECT COUNT(*) AS n FROM commits").fetchone()["n"]
    with_files = conn.execute(
        "SELECT COUNT(*) AS n FROM (SELECT DISTINCT hash FROM files_changed)").fetchone()["n"]
    broad = conn.execute(
        "SELECT COUNT(*) AS n FROM (SELECT hash FROM files_changed GROUP BY hash "
        "HAVING COUNT(*) > ?)", (max_files,)).fetchone()["n"]
    out = {
        "max_files": max_files,
        "commits_indexed": indexed,
        "commits_considered": max(with_files - broad, 0),
        "commits_skipped_too_broad": broad,
        # merges and empty commits carry no file rows at all (see history.py),
        # so they contribute no edges either - said out loud rather than folded
        # into the skipped count, which means something else
        "commits_without_file_rows": max(indexed - with_files, 0),
    }
    if note:
        out["note"] = note
    return out


def changes_with(conn: sqlite3.Connection, args: dict, limit: int, project_dir) -> dict:
    """The files that most often changed in the same commits as `path`.

    Every row carries its evidence - how many commits it shares with the anchor,
    how many commits it has of its own, and the most recent shared commit - so a
    ranking position can be checked instead of believed. An empty result says
    WHY it is empty: an unknown path, a path whose every commit was damped away,
    and a path that genuinely changed alone are three different answers.
    """
    raw = args.get("path")
    if not raw:
        return {"ok": False,
                "error": "changes_with needs `path` (a file or directory in the repository)"}
    max_files, note = _breadth(args)
    path, match, match_params = _path_terms(project_dir, raw, "f")
    damping = _damping(conn, max_files, note)

    anchor_all = conn.execute(
        f"SELECT COUNT(*) AS n FROM (SELECT DISTINCT f.hash FROM files_changed f WHERE {match})",
        match_params).fetchone()["n"]
    anchor = conn.execute(
        f"WITH {BROAD_CTE} SELECT COUNT(*) AS n FROM (SELECT DISTINCT f.hash FROM files_changed f "
        f"WHERE {match} AND f.hash NOT IN (SELECT hash FROM broad))",
        [max_files] + match_params).fetchone()["n"]

    sql = (
        f"WITH {BROAD_CTE}, "
        f"anchor AS (SELECT DISTINCT f.hash AS hash FROM files_changed f "
        f"WHERE {match} AND f.hash NOT IN (SELECT hash FROM broad)), "
        # only the partners can appear in the answer, so `totals` is restricted
        # to their paths rather than counting every path in the repository
        "totals AS (SELECT path, COUNT(DISTINCT hash) AS n FROM files_changed "
        "WHERE hash NOT IN (SELECT hash FROM broad) "
        "AND path IN (SELECT path FROM files_changed WHERE hash IN (SELECT hash FROM anchor)) "
        "GROUP BY path) "
        "SELECT f.path AS path, COUNT(*) AS shared_commits, t.n AS partner_commits, "
        # exactly ONE min/max aggregate in this statement, which is the case
        # SQLite documents: the bare c.* columns come from the row that produced
        # MAX(c.epoch) - i.e. the most recent shared commit, not an arbitrary one
        "MAX(c.epoch) AS last_epoch, c.short_hash AS last_short_hash, "
        "c.date AS last_date, c.subject AS last_subject "
        "FROM files_changed f JOIN anchor a ON a.hash = f.hash "
        "JOIN commits c ON c.hash = f.hash JOIN totals t ON t.path = f.path "
        f"WHERE NOT {match} "
        "GROUP BY f.path "
        "ORDER BY shared_commits DESC, "
        # Ties are the common case in a young repository. Break them toward the
        # file that changes MOSTLY with this one, over the file that changes with
        # everything - a changelog, a lockfile, a work log.
        "(CAST(COUNT(*) AS REAL) / (? + t.n - COUNT(*))) DESC, last_epoch DESC, f.path "
        "LIMIT ?"
    )
    params = ([max_files] + match_params + match_params
              + [anchor if anchor else 1, limit + 1])
    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    truncated = len(rows) > limit
    rows = rows[:limit]
    for r in rows:
        shared = r.get("shared_commits") or 0
        partner = r.get("partner_commits") or 0
        union = (anchor + partner - shared) or 1
        r["anchor_commits"] = anchor
        r["share_of_anchor"] = round(shared / anchor, 3) if anchor else None
        r["jaccard"] = round(shared / union, 3)
        r["weak"] = shared <= 1
        r.pop("last_epoch", None)

    caveats = list(CO_CHANGE_CAVEATS)
    if damping["commits_considered"] < YOUNG_HISTORY:
        caveats.append(
            f"only {damping['commits_considered']} commit(s) were considered here - that is little "
            f"history to rank from, so read these as hints, not findings.")

    empty_reason = None
    if not rows:
        if anchor_all == 0:
            empty_reason = (
                f"no commit in the index touches '{path}'. It may be spelled differently, may "
                f"predate the indexed history, or may postdate the last refresh.")
        elif anchor == 0:
            empty_reason = (
                f"'{path}' changed in {anchor_all} commit(s), but every one of them touched more "
                f"than {max_files} files and was skipped as too broad. Raise `max_files` to "
                f"include them - and then read the result knowing sweeps are in it.")
        else:
            empty_reason = (
                f"'{path}' changed in {anchor} considered commit(s) and nothing else changed in "
                f"any of them: it has no co-change partners.")

    return {
        "ok": True,
        "query": "changes_with",
        "path": path,
        "rows": rows,
        "count": len(rows),
        "limit": limit,
        "truncated": truncated,
        "anchor_commits": anchor,
        "anchor_commits_all": anchor_all,
        "damping": damping,
        "caveats": caveats,
        "empty_reason": empty_reason,
    }


def coupling_between(conn: sqlite3.Connection, args: dict, limit: int, project_dir) -> dict:
    """The commits where both paths changed - the evidence behind one edge.

    Damping is deliberately NOT applied to this listing. This query exists so a
    claimed edge can be inspected, and "three of the five shared commits were
    sweeps" is exactly the thing worth seeing; every row is marked `too_broad`
    when it is one, and the counts say how many would survive the cap.
    """
    raw_a = args.get("path")
    raw_b = args.get("other_path") or args.get("other") or args.get("path_b")
    if not raw_a or not raw_b:
        return {"ok": False,
                "error": "coupling_between needs `path` and `other_path` - two files or "
                         "directories in the repository"}
    max_files, note = _breadth(args)
    a, match_a, pa = _path_terms(project_dir, raw_a, "f")
    b, match_b, pb = _path_terms(project_dir, raw_b, "f")
    if a == b:
        return {"ok": False,
                "error": f"`path` and `other_path` both resolve to '{a}'; coupling_between "
                         f"compares two different paths"}

    ctes = ("WITH a AS (SELECT DISTINCT f.hash AS hash FROM files_changed f WHERE " + match_a + "), "
            "b AS (SELECT DISTINCT f.hash AS hash FROM files_changed f WHERE " + match_b + "), "
            "breadth AS (SELECT hash, COUNT(*) AS n FROM files_changed GROUP BY hash) ")
    joins = ("FROM commits c JOIN a ON a.hash = c.hash JOIN b ON b.hash = c.hash "
             "JOIN breadth br ON br.hash = c.hash ")

    totals = conn.execute(
        ctes + "SELECT COUNT(*) AS n, "
               "SUM(CASE WHEN br.n > ? THEN 1 ELSE 0 END) AS broad " + joins,
        pa + pb + [max_files]).fetchone()
    shared_total = totals["n"] or 0
    shared_broad = totals["broad"] or 0

    rows = [dict(r) for r in conn.execute(
        ctes + f"SELECT {COMMIT_COLUMNS_C}, br.n AS files_in_commit " + joins
        + "ORDER BY c.epoch DESC, c.hash LIMIT ?",
        pa + pb + [limit + 1]).fetchall()]
    truncated = len(rows) > limit
    rows = rows[:limit]
    for r in rows:
        r["too_broad"] = bool((r.get("files_in_commit") or 0) > max_files)

    only_a = conn.execute(
        f"SELECT COUNT(*) AS n FROM (SELECT DISTINCT f.hash FROM files_changed f WHERE {match_a})",
        pa).fetchone()["n"]
    only_b = conn.execute(
        f"SELECT COUNT(*) AS n FROM (SELECT DISTINCT f.hash FROM files_changed f WHERE {match_b})",
        pb).fetchone()["n"]

    caveats = list(CO_CHANGE_CAVEATS)
    if shared_total and shared_total == shared_broad:
        caveats.append(
            f"every shared commit here touched more than {max_files} files, so changes_with counts "
            f"NONE of them: on that evidence these two paths have no edge at all.")

    return {
        "ok": True,
        "query": "coupling_between",
        "path": a,
        "other_path": b,
        "rows": rows,
        "count": len(rows),
        "limit": limit,
        "truncated": truncated,
        "shared_commits": shared_total,
        "shared_counted": max(shared_total - shared_broad, 0),
        "shared_too_broad": shared_broad,
        "commits_touching_path": only_a,
        "commits_touching_other_path": only_b,
        "max_files": max_files,
        "damping_applied": False,
        "note": note,
        "caveats": caveats,
    }


def hotspots(conn: sqlite3.Connection, args: dict, limit: int, project_dir) -> dict:
    """The most-changed paths, optionally under one directory: where to start."""
    max_files, note = _breadth(args)
    scope_sql, scope_params, scope = "", [], None
    raw = args.get("path")
    if raw:
        scope, match, scope_params = _path_terms(project_dir, raw, "f")
        scope_sql = " AND " + match
    damping = _damping(conn, max_files, note)

    # One aggregate pass over the damped rows, then one indexed lookup per
    # RETURNED row for the commit behind its newest change - at most `limit` of
    # them, and they cost well under a millisecond together. The alternative
    # (a second span table joined in, or the bare-column min/max idiom) reads
    # the whole table twice for the same answer.
    sql = (
        f"WITH {KEPT_CTE} "
        "SELECT f.path AS path, COUNT(*) AS commits, MIN(c.epoch) AS first_epoch, "
        "MAX(c.epoch) AS last_epoch "
        "FROM files_changed f JOIN commits c ON c.hash = f.hash "
        f"WHERE f.hash IN (SELECT hash FROM kept){scope_sql} "
        "GROUP BY f.path ORDER BY commits DESC, last_epoch DESC, f.path LIMIT ?"
    )
    rows = [dict(r) for r in conn.execute(
        sql, [max_files] + scope_params + [limit + 1]).fetchall()]
    truncated = len(rows) > limit
    rows = rows[:limit]
    for r in rows:
        r["first_date"] = _date_only(r.pop("first_epoch", None))
        r["weak"] = (r.get("commits") or 0) <= 1
        last = None
        try:
            last = conn.execute(
                "SELECT c.short_hash, c.date, c.subject FROM files_changed f "
                "JOIN commits c ON c.hash = f.hash WHERE f.path = ? AND c.epoch = ? LIMIT 1",
                (r.get("path"), r.pop("last_epoch", None))).fetchone()
        except Exception:
            pass       # a row worth returning without its newest commit named
        r["last_short_hash"] = last["short_hash"] if last else None
        r["last_date"] = last["date"] if last else None
        r["last_subject"] = last["subject"] if last else None

    caveats = list(HOTSPOT_CAVEATS)
    if damping["commits_considered"] < YOUNG_HISTORY:
        caveats.append(
            f"only {damping['commits_considered']} commit(s) were considered here - that is little "
            f"history to rank from, so read these as hints, not findings.")

    empty_reason = None
    if not rows:
        if damping["commits_considered"] == 0:
            empty_reason = (
                f"no commit in the index survived the {max_files}-file cap, so there is nothing to "
                f"rank. Raise `max_files`, or refresh the index.")
        elif scope:
            empty_reason = (f"no considered commit touches anything at or under '{scope}'.")
        else:
            empty_reason = "the considered commits carry no file rows."

    return {
        "ok": True,
        "query": "hotspots",
        "path": scope,
        "rows": rows,
        "count": len(rows),
        "limit": limit,
        "truncated": truncated,
        "damping": damping,
        "caveats": caveats,
        "empty_reason": empty_reason,
    }


CO_CHANGE_QUERIES = {
    "changes_with": changes_with,
    "coupling_between": coupling_between,
    "hotspots": hotspots,
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

    if name in CO_CHANGE_QUERIES:
        # These build their own payloads (evidence columns, damping counts,
        # caveats) rather than the flat row list below. One net around them for
        # the same reason every other statement here has one: a query that
        # raises would surface as a crashed tool, not as an answer.
        try:
            return CO_CHANGE_QUERIES[name](conn, args, limit, project_dir)
        except Exception as exc:
            return {"ok": False, "error": f"query failed: {exc}"}

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
