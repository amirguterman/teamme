#!/usr/bin/env python3
"""The history librarian's indexer: git log, incrementally, into the store.

Mechanical extraction only - hash, author, date, subject, body, parents, files
and their diffstat. No model, no reasoning, no scope tags. That split is the
point: this half is deterministic python3 that costs nothing and could later be
driven by a hook without putting a model in anyone's editing loop; the reasoned
half (why a change was made, which changes belong together) is a librarian
agent's job, arrives as extra records in the same JSONL, and is preserved by a
full reindex rather than overwritten.

Incremental by construction. The store remembers the last indexed hash, so a
refresh reads `git log <hash>..HEAD` and nothing else. Re-reading history that
is already indexed is the one thing that would make "index every commit"
unaffordable.

Three conditions are reported rather than raised:
  - the marker is unreachable (rebase, force-push, shallow clone) - falls back
    to a full reindex and SAYS SO in the result, never silently
  - git missing, or not a repository - a clean error payload naming the cause
  - an empty repository - success with zero rows, which is not an error

Output is parsed from NUL-delimited git output with explicit delimiters, and
decoded with errors="replace": real repositories contain commit subjects and
paths with newlines, quotes and bytes that are not UTF-8.
"""

import os
import re
import subprocess
import time
from datetime import datetime, timezone

try:
    from . import store
except ImportError:  # loaded as a loose module rather than a package member
    import store  # type: ignore

REC = b"\x01"     # start of a log entry
FIELD = b"\x1f"   # between header fields
ENDHDR = b"\x02"  # end of the header, start of the numstat block

# %b, not %B: `subject` already carries the first line, so the two together are
# the whole message with nothing duplicated.
FORMAT = ("format:%x01%H%x1f%h%x1f%an%x1f%ae%x1f%aI%x1f%at%x1f%P%x1f%s%x1f%b%x02")

HEX = re.compile(r"^[0-9a-f]{7,64}$")
GIT_TIMEOUT = 600


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _git_env() -> dict:
    env = dict(os.environ)
    # Deterministic, non-interactive, and never paged - this runs unattended.
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_PAGER"] = "cat"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env.pop("GIT_DIR", None)
    env.pop("GIT_WORK_TREE", None)
    return env


def git(root, args, timeout: int = 30):
    """(returncode, stdout_bytes, stderr_text). Never raises."""
    try:
        proc = subprocess.run(
            ["git"] + [str(a) for a in args],
            cwd=str(root),
            env=_git_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except FileNotFoundError:
        # FileNotFoundError here means either git or the working directory is
        # missing. Reporting the wrong one sends someone to install a tool they
        # already have.
        if not os.path.isdir(str(root)):
            return (127, b"", f"no such directory: {root}")
        return (127, b"", "git is not installed or not on PATH")
    except subprocess.TimeoutExpired:
        return (124, b"", f"git {args[0] if args else ''} timed out after {timeout}s")
    except Exception as exc:
        return (1, b"", f"could not run git: {exc}")
    return (proc.returncode, proc.stdout or b"", (proc.stderr or b"").decode("utf-8", "replace").strip())


def probe(root) -> dict:
    """What git can tell us about this directory, without indexing anything."""
    if not os.path.isdir(str(root)):
        return {"git": True, "repo": False, "head": None,
                "error": f"no such directory: {root}"}
    rc, out, err = git(root, ["rev-parse", "--is-inside-work-tree"])
    if rc == 127:
        return {"git": False, "repo": False, "head": None, "error": err}
    if rc != 0 or out.strip() != b"true":
        return {"git": True, "repo": False, "head": None,
                "error": err or f"not a git repository: {root}"}
    rc, out, _ = git(root, ["rev-parse", "HEAD"])
    head = out.decode("ascii", "replace").strip() if rc == 0 else None
    if head and not HEX.match(head):
        head = None
    rc, out, _ = git(root, ["rev-parse", "--is-shallow-repository"])
    shallow = (rc == 0 and out.strip() == b"true")
    return {"git": True, "repo": True, "head": head, "error": None, "shallow": shallow}


def reachable(root, rev: str) -> bool:
    if not rev:
        return False
    rc, _, _ = git(root, ["merge-base", "--is-ancestor", rev, "HEAD"])
    return rc == 0


def count_since(root, marker: str):
    """How many commits HEAD has that the marker does not. None if unanswerable."""
    rng = f"{marker}..HEAD" if marker else "HEAD"
    rc, out, _ = git(root, ["rev-list", "--count", rng])
    if rc != 0:
        return None
    try:
        return int(out.decode("ascii", "replace").strip())
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# parsing
# --------------------------------------------------------------------------- #

def _numstat(blob: bytes) -> list:
    """The `add \t del \t path` entries of one log record.

    --no-renames is passed, so every entry names exactly one path; the two-path
    rename form is still handled in case a future caller drops that flag. `-`
    for a count means a binary file, and is stored as NULL rather than 0 - it is
    unknown, not zero.
    """
    parts = [p for p in blob.split(b"\x00") if p.strip(b"\n\r")]
    files, i = [], 0
    while i < len(parts):
        entry = parts[i].lstrip(b"\n\r")
        i += 1
        bits = entry.split(b"\t", 2)
        if len(bits) != 3:
            continue

        def count(raw):
            try:
                return int(raw)
            except Exception:
                return None

        path = bits[2]
        if path == b"" and i + 1 < len(parts):
            # rename: this entry's path is empty and the next two fields are
            # the old and the new path. Record the new one.
            i += 1
            path = parts[i].lstrip(b"\n\r")
            i += 1
        if not path:
            continue
        files.append({
            "path": path.decode("utf-8", "replace"),
            "additions": count(bits[0]),
            "deletions": count(bits[1]),
        })
    return files


def _record(chunk: bytes, indexed_at: str):
    header, sep, rest = chunk.partition(ENDHDR)
    if not sep:
        return None
    fields = header.split(FIELD)
    if len(fields) != 9:
        return None
    h = fields[0].decode("ascii", "replace").strip()
    if not HEX.match(h):
        return None

    def s(b):
        return b.decode("utf-8", "replace")

    try:
        epoch = int(fields[5])
    except Exception:
        epoch = None
    parents = [p for p in s(fields[6]).split() if HEX.match(p)]
    return {
        "kind": "commit",
        "hash": h,
        "short_hash": s(fields[1]).strip(),
        "author": s(fields[2]),
        "author_email": s(fields[3]),
        "date": s(fields[4]).strip(),
        "epoch": epoch,
        "subject": s(fields[7]),
        "body": s(fields[8]).rstrip("\n"),
        "parents": parents,
        "files": _numstat(rest),
        "indexed_at": indexed_at,
    }


def iter_records(root, rev_range: str):
    """Stream `git log <rev_range>` as commit records, oldest first.

    Streamed rather than slurped: a full index of a large repository produces
    output measured in tens of megabytes, and nothing here needs it all in
    memory at once. Yields (record, malformed_count_delta).
    """
    indexed_at = _now()
    args = ["log", "--reverse", "--no-renames", "--numstat", "-z",
            "--format=" + FORMAT]
    if rev_range:
        args.append(rev_range)
    try:
        proc = subprocess.Popen(
            ["git"] + args, cwd=str(root), env=_git_env(),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
    except FileNotFoundError:
        if not os.path.isdir(str(root)):
            raise RuntimeError(f"no such directory: {root}")
        raise RuntimeError("git is not installed or not on PATH")
    except Exception as exc:
        raise RuntimeError(f"could not run git log: {exc}")

    buf = b""
    started = time.time()
    try:
        while True:
            block = proc.stdout.read(1 << 16)
            if not block:
                break
            buf += block
            pieces = buf.split(REC)
            buf = pieces.pop()  # the last piece may be a partial record
            for piece in pieces:
                if not piece.strip(b"\x00\n"):
                    continue
                yield _record(piece, indexed_at)
            if time.time() - started > GIT_TIMEOUT:
                raise RuntimeError(f"git log exceeded {GIT_TIMEOUT}s")
        if buf.strip(b"\x00\n"):
            yield _record(buf, indexed_at)
    finally:
        try:
            err = (proc.stderr.read() or b"").decode("utf-8", "replace").strip()
            proc.stdout.close()
            proc.stderr.close()
            proc.wait(timeout=10)
        except Exception:
            err = ""
            try:
                proc.kill()
            except Exception:
                pass
        if proc.returncode not in (0, None) and err:
            raise RuntimeError(f"git log failed: {err}")


# --------------------------------------------------------------------------- #
# indexing
# --------------------------------------------------------------------------- #

def index(project_dir=None, full: bool = False) -> dict:
    """Bring the history index up to HEAD. Never raises; returns a result dict."""
    started = time.time()
    root = store.project_root(project_dir)
    result = {
        "ok": False,
        "librarian": "history",
        "project_dir": str(root),
        "mode": "full" if full else "incremental",
        "fallback": None,
        "commits_added": 0,
        "files_added": 0,
        "malformed_records": 0,
        "notes": [],
    }

    p = probe(root)
    if not p["git"]:
        result["error"] = "git is not installed or not on PATH - nothing can be indexed"
        result["elapsed_seconds"] = round(time.time() - started, 4)
        return result
    if not p["repo"]:
        err = p.get("error") or ""
        result["error"] = (
            f"no such directory: {root}" if err.startswith("no such directory")
            else f"not a git repository: {root}")
        result["elapsed_seconds"] = round(time.time() - started, 4)
        return result

    conn = None
    try:
        conn, reset_note = store.connect_or_reset(root)
        if reset_note:
            result["notes"].append(reset_note)

        if p["head"] is None:
            # An empty repository is a successful index of nothing.
            result.update(ok=True, head=None, commits_added=0,
                          elapsed_seconds=round(time.time() - started, 4))
            result["notes"].append("repository has no commits yet - indexed nothing")
            result.update(store.counts(conn))
            return result

        marker = store.get_meta(conn, store.META_LAST_INDEXED)

        # index.db is disposable by design: if it is empty (deleted, or a fresh
        # clone of a repo whose commits.jsonl IS committed) but the text record
        # is not, rebuild from the text before asking git for anything.
        if not full and not marker:
            if store.read_records(root)["records"]:
                rb = store.rebuild(root, conn=conn)
                if rb.get("ok"):
                    marker = store.get_meta(conn, store.META_LAST_INDEXED)
                    result["notes"].append(
                        f"index.db was empty - rebuilt {rb['commits']} commit(s) from "
                        f"commits.jsonl before refreshing"
                    )
                    result["rebuilt_from_text"] = rb["commits"]

        if full or not marker:
            mode, rev = "full", ""
            if full:
                result["notes"].append("full reindex requested")
            else:
                result["notes"].append("nothing indexed here yet - full index")
        elif not reachable(root, marker):
            mode, rev = "full", ""
            result["fallback"] = (
                f"the last indexed commit {marker[:12]} is not reachable from HEAD "
                f"(rebase, force-push or shallow clone) - reindexed in full instead of "
                f"incrementally"
            )
        else:
            mode, rev = "incremental", f"{marker}..HEAD"
        result["mode"] = mode
        result["previous_marker"] = marker

        records, malformed = [], 0
        try:
            for rec in iter_records(root, rev):
                if rec is None:
                    malformed += 1
                else:
                    records.append(rec)
        except RuntimeError as exc:
            result["error"] = str(exc)
            result["elapsed_seconds"] = round(time.time() - started, 4)
            return result
        result["malformed_records"] = malformed
        if malformed:
            result["notes"].append(
                f"{malformed} log record(s) could not be parsed and were skipped"
            )

        if mode == "full":
            wrote = store.rewrite_records(root, records)
            if wrote.get("error"):
                result["error"] = wrote["error"]
                result["elapsed_seconds"] = round(time.time() - started, 4)
                return result
            if wrote.get("preserved"):
                result["notes"].append(
                    f"{wrote['preserved']} non-commit record(s) in commits.jsonl preserved"
                )
            # Rebuild from the text that was just written rather than from the
            # in-memory records: every full index therefore exercises the
            # rebuild-from-text path, so it cannot quietly stop working.
            rb = store.rebuild(root, conn=conn)
            if not rb.get("ok"):
                result["error"] = rb.get("error", "rebuild failed")
                result["elapsed_seconds"] = round(time.time() - started, 4)
                return result
            result["commits_added"] = rb["commit_records"]
            result["files_added"] = rb["files_changed"]
            if rb.get("skipped_lines"):
                result["notes"].append(
                    f"{rb['skipped_lines']} unparsable line(s) in commits.jsonl were skipped"
                )
        else:
            written = store.append_and_insert(root, conn, records)
            if written.get("error"):
                result["error"] = written["error"]
                result["elapsed_seconds"] = round(time.time() - started, 4)
                return result
            result["commits_added"] = written["inserted"]
            result["files_added"] = written["files"]
            if written.get("skipped_existing"):
                result["notes"].append(
                    f"{written['skipped_existing']} commit(s) were already indexed by "
                    f"another refresh and were not written twice"
                )

        with conn:
            store.set_meta(conn, store.META_LAST_INDEXED, p["head"])
            store.set_meta(conn, store.META_LAST_REFRESH, _now())

        result["ok"] = True
        result["head"] = p["head"]
        result["last_indexed_hash"] = p["head"]
        result.update(store.counts(conn))
        if p.get("shallow"):
            result["notes"].append(
                "this is a shallow clone - only the commits it actually has were indexed"
            )
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


def status(project_dir=None) -> dict:
    """What the history index holds and how far behind HEAD it is. Never raises."""
    root = store.project_root(project_dir)
    out = {
        "librarian": "history",
        "project_dir": str(root),
        "jsonl": str(store.commits_jsonl(root)),
        "db": str(store.db_path(root)),
        "has_data": False,
    }
    p = probe(root)
    out["git_available"] = p["git"]
    out["is_git_repo"] = p["repo"]
    out["head"] = p["head"]
    if p.get("error"):
        out["error"] = p["error"]
    rec = store.read_records(root)
    out["jsonl_records"] = len(rec["records"])
    out["jsonl_unparsable_lines"] = rec["skipped"]
    out["jsonl_exists"] = rec["exists"]
    if not store.db_path(root).exists():
        out["db_exists"] = False
        out["note"] = (
            "index.db does not exist. It is disposable: the next refresh rebuilds it "
            "from commits.jsonl, or from git if there is no text record either."
        )
        out["has_data"] = bool(rec["records"])
        return out
    out["db_exists"] = True
    conn = None
    try:
        conn, reset_note = store.connect_or_reset(root)
        if reset_note:
            out["note"] = reset_note
        out.update(store.counts(conn))
        out["schema_version"] = store.get_meta(conn, store.META_SCHEMA_VERSION)
        out["last_indexed_hash"] = store.get_meta(conn, store.META_LAST_INDEXED)
        out["last_refresh_at"] = store.get_meta(conn, store.META_LAST_REFRESH)
        out["has_data"] = bool(out.get("commits"))
        if p["repo"] and out.get("last_indexed_hash"):
            if reachable(root, out["last_indexed_hash"]):
                out["commits_behind_head"] = count_since(root, out["last_indexed_hash"])
            else:
                out["commits_behind_head"] = None
                out["marker_unreachable"] = True
                out["note"] = (
                    "the last indexed commit is not reachable from HEAD (rebase, force-push "
                    "or shallow clone) - the next refresh falls back to a full reindex"
                )
        elif p["repo"]:
            out["commits_behind_head"] = count_since(root, "")
    except Exception as exc:
        out["error"] = f"could not read the index: {exc}"
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return out
