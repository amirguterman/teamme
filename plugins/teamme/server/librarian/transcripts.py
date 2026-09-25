#!/usr/bin/env python3
"""Everything teamme assumes about Claude Code's session transcripts.

The harness writes one append-only JSONL file per session, under a per-project
directory, inside its own config directory. That file is the record: it is on
disk, it survives compaction because it is a file rather than context, and it is
append-only, so it can be indexed incrementally by byte offset and never
re-read. This module is the ONLY place that knows any of that - where the files
live, and what a line in one looks like. `sessions.py` consumes normalized turns
from here and knows nothing about the harness's format.

That containment is deliberate and is the same discipline `preflight.py` applies
to the plugin install record: this is teamme's SECOND assumption about Claude
Code's on-disk layout, so it sits between one banner comment and the next, and
is read live on every call rather than cached, so an upgrade that moves things
is self-correcting instead of silently wrong.

Nothing here raises at the boundary and nothing here trusts its input. A
transcript is written by another process, possibly while this one reads it: the
final line can be half-written, a record can be enormous, a line can be
unparsable, a field can be any type at all. Every one of those is skipped and
COUNTED, never fatal, and a partial final line is left unconsumed so the next
refresh picks it up whole.
"""

import json
import os
import pathlib
import re
from datetime import datetime, timezone

# A line longer than this is skipped without being parsed. The harness writes
# whole-file snapshots into some record types, and a single line can be megabytes
# of content this index has no use for; parsing one costs memory for nothing.
MAX_LINE_BYTES = 4 * 1024 * 1024

# Per-turn text cap. Long enough that a whole prompt or answer normally fits,
# bounded so one pathological record cannot dominate the index.
MAX_TURN_CHARS = 20000

# A mark's one-line label.
MAX_LABEL_CHARS = 200
MAX_DETAIL_CHARS = 300

# How many directories, and how many lines of the newest file in each, the
# fallback scan may read when the slug does not resolve.
SCAN_MAX_DIRS = 200
SCAN_MAX_LINES = 40


# =========================================================================== #
# CLAUDE CODE'S ON-DISK LAYOUT - the whole of teamme's assumption about it
#
# Today: $CLAUDE_CONFIG_DIR/projects/<slug>/<session-id>.jsonl, falling back to
# ~/.claude/projects/... when CLAUDE_CONFIG_DIR is unset.
#
# The slug is the project's absolute path with every character that is not a
# letter or a digit replaced by '-'. That was not guessed: it was derived by
# comparing every directory name in two real projects/ trees against the `cwd`
# field recorded inside the files in it, including a path containing a space,
# and it matched on every directory whose files carried a session-root cwd.
#
# It is still an inference about somebody else's format, so it is never the only
# route: if the slug does not resolve, `locate()` SCANS the projects/ tree and
# identifies the directory by the `cwd` recorded inside the transcripts
# themselves, which is ground truth rather than an encoding guess. If neither
# works it returns a problem naming exactly where it looked. It never guesses a
# directory, and it never caches one - an upgrade that moves the store is then
# self-correcting.
# =========================================================================== #

def config_dirs() -> list:
    """The candidate Claude Code config directories, best first. Read live."""
    out, seen = [], set()
    for raw in (os.environ.get("CLAUDE_CONFIG_DIR"), None):
        try:
            if raw:
                p = pathlib.Path(raw).expanduser()
            else:
                p = pathlib.Path.home() / ".claude"
        except Exception:
            continue
        key = str(p)
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


def project_slug(project_dir) -> str:
    """The directory name the harness uses for a project path."""
    try:
        path = str(pathlib.Path(project_dir).expanduser().resolve())
    except Exception:
        path = str(project_dir)
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def _newest_jsonl(directory):
    try:
        files = [p for p in directory.iterdir()
                 if p.is_file() and p.suffix == ".jsonl"]
    except Exception:
        return None
    best, best_m = None, -1.0
    for p in files:
        try:
            m = p.stat().st_mtime
        except Exception:
            continue
        if m > best_m:
            best, best_m = p, m
    return best


def _declared_cwds(path, max_lines: int = SCAN_MAX_LINES) -> set:
    """The `cwd` values the first few records of a transcript declare."""
    out = set()
    try:
        with open(str(path), "r", encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh):
                if i >= max_lines:
                    break
                if len(line) > 200000:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if isinstance(rec, dict) and isinstance(rec.get("cwd"), str):
                    out.add(rec["cwd"])
    except Exception:
        pass
    return out


def locate(project_dir) -> dict:
    """Where this project's transcripts live.

    Returns {"ok", "dir", "source", "slug", "searched", "problem"} and never
    raises. `source` is "slug" when the encoded directory name resolved and
    "scan" when it was identified by the cwd recorded inside the files - which
    is worth knowing, because the second means the encoding assumption above no
    longer holds and this module is the place to fix it.
    """
    try:
        root = str(pathlib.Path(project_dir).expanduser().resolve())
    except Exception:
        root = str(project_dir)
    slug = project_slug(root)
    out = {"ok": False, "dir": None, "source": None, "slug": slug,
           "searched": [], "problem": None}

    bases = []
    for cfg in config_dirs():
        base = cfg / "projects"
        bases.append(base)
        out["searched"].append(str(base / slug))

    for base in bases:
        try:
            cand = base / slug
            if cand.is_dir():
                out.update(ok=True, dir=str(cand), source="slug")
                return out
        except Exception:
            continue

    # The slug did not resolve. Ask the files themselves rather than trying a
    # second encoding: a directory whose transcripts say they ran in this
    # project IS this project's directory, whatever it is called.
    for base in bases:
        try:
            entries = sorted([p for p in base.iterdir() if p.is_dir()])[:SCAN_MAX_DIRS]
        except Exception:
            continue
        for d in entries:
            newest = _newest_jsonl(d)
            if newest is None:
                continue
            if root in _declared_cwds(newest):
                out.update(ok=True, dir=str(d), source="scan")
                out["problem"] = (
                    f"the expected directory name '{slug}' does not exist, but "
                    f"{d} records this project as its working directory and was used instead. "
                    f"The name encoding in librarian/transcripts.py may be out of date."
                )
                return out

    where = ", ".join(str(b) for b in bases) or "(no config directory could be determined)"
    out["problem"] = (
        f"no transcript directory for {root}. Looked for '{slug}' under {where}, then scanned "
        f"those trees for a directory whose transcripts record this project as their working "
        f"directory, and found neither. Nothing was indexed. If $CLAUDE_CONFIG_DIR is unset the "
        f"default ~/.claude is used; sessions run under a different config directory are not "
        f"visible from here."
    )
    return out


def _agent_meta(path) -> dict:
    """agentType and description for a subagent thread, from its sidecar.

    Absent, unreadable or the wrong shape is not an error: the thread is indexed
    either way, just without a name for who ran it.
    """
    try:
        meta = json.loads(pathlib.Path(str(path)[: -len(".jsonl")] + ".meta.json")
                          .read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(meta, dict):
        return {}
    out = {}
    if isinstance(meta.get("agentType"), str):
        out["agent"] = meta["agentType"]
    if isinstance(meta.get("description"), str):
        out["title"] = _one_line(meta["description"])
    return out


def list_sessions(directory) -> list:
    """Every transcript in a project directory, newest last. Never raises.

    Two kinds, both real conversations and both indexed:

      <session-id>.jsonl                          the main thread
      <session-id>/subagents/agent-*.jsonl        one per dispatched subagent

    The sidecars are not an optional extra. Measured on this repo's own session,
    they hold 26 MB against the main thread's 8.7 MB: on a team that dispatches
    specialists, most of the work - and most of the reasoning a later question
    asks about - happened in one of them. Each is its own append-only file, so
    each gets its own byte offset and is indexed exactly like a main thread,
    carrying `parent` so the two can be told apart.
    """
    out = []
    try:
        root = pathlib.Path(directory)
        entries = list(root.iterdir())
    except Exception:
        return out
    for p in entries:
        try:
            if not p.is_file() or p.suffix != ".jsonl":
                continue
            st = p.stat()
        except Exception:
            continue
        out.append({"session_id": p.stem, "path": str(p), "parent": None,
                    "agent": None, "title": None,
                    "size": st.st_size, "mtime": st.st_mtime})
        try:
            side = p.parent / p.stem / "subagents"
            kids = sorted(side.iterdir()) if side.is_dir() else []
        except Exception:
            kids = []
        for k in kids:
            try:
                if not k.is_file() or k.suffix != ".jsonl":
                    continue
                kst = k.stat()
            except Exception:
                continue
            meta = _agent_meta(k)
            out.append({"session_id": k.stem, "path": str(k), "parent": p.stem,
                        "agent": meta.get("agent"), "title": meta.get("title"),
                        "size": kst.st_size, "mtime": kst.st_mtime})
    out.sort(key=lambda r: (r["mtime"], r["session_id"]))
    return out


# =========================================================================== #
# ...and the shape of a line in one. Still the harness's format, still contained
# here: `sessions.py` never touches a raw record.
#
# Record types seen in the wild, and what this index does with each:
#   user       a prompt, an injected message, or a tool RESULT. Tool results are
#              not indexed as text - see TOOL_RESULTS_NOT_INDEXED.
#   assistant  visible prose (text), reasoning (thinking) and tool calls
#              (tool_use). Thinking blocks carry an encrypted signature and an
#              EMPTY string: measured across 252 of them in an 8.7 MB
#              transcript, every one was empty. Reasoning is therefore not
#              recoverable from a transcript by anybody, and this index does not
#              pretend otherwise.
#   system     status lines, plus subtype "compact_boundary" - a real compaction
#              event with its own metadata, which is why nothing here needs a
#              PreCompact hook.
#   everything else (attachment, file-history-snapshot, cost-state, ...) carries
#              no conversation and is skipped.
# =========================================================================== #

# Why a search never finds the contents of a file that was read, or the output
# of a command. Stated in every search result rather than left to be discovered.
TOOL_RESULTS_NOT_INDEXED = (
    "tool OUTPUT is not indexed - file contents that were read, command output, search results. "
    "What is indexed is what was typed, what was said, and which tools ran on what. A search that "
    "finds nothing has not proved the text was never on screen."
)

THINKING_NOT_RECORDED = (
    "reasoning is not in the transcript at all: thinking blocks are stored with an encrypted "
    "signature and an empty body, so no index can recover them."
)

MARK_KINDS = ("prompt", "message", "recap", "answer", "tool", "file", "compaction")

# A tool call that WROTE something gets its own mark kind: "when did we change
# X" is a different question from "which tools ran", and folding them together
# makes the first one unanswerable without reading every row.
WRITE_TOOLS = ("write", "edit", "multiedit", "notebookedit", "applypatch")

# The input keys worth putting in a tool mark's label, best first.
TARGET_KEYS = ("file_path", "notebook_path", "path", "command", "pattern", "query",
               "url", "description", "subagent_type", "prompt", "text", "id", "name")

SYSTEM_REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)

# A slash command is recorded as a small pile of tags rather than as what the
# person typed. Indexed raw, every such prompt's label opens with
# "<command-message>intake</command-message>..." and the actual request is
# pushed past the end of the label - so the one query built to show WHERE
# something was asked would show the tag soup instead. Unwrapped to "/intake
# <args>", which is what was typed.
COMMAND_NAME = re.compile(r"<command-name>\s*(.*?)\s*</command-name>", re.S)
COMMAND_ARGS = re.compile(r"<command-args>\s*(.*?)\s*</command-args>", re.S)
COMMAND_TAG = re.compile(r"</?command-[a-z-]+>", re.I)


def _unwrap_command(text: str) -> str:
    """"<command-name>/x</command-name>...<command-args>y</command-args>" -> "/x y".

    Anything that is not that shape is returned untouched: this only ever
    reformats, and a record it does not recognize keeps every character.
    """
    if "<command-name>" not in text:
        return text
    name = COMMAND_NAME.search(text)
    if not name:
        return text
    args = COMMAND_ARGS.search(text)
    rest = COMMAND_NAME.sub("", text)
    rest = COMMAND_ARGS.sub("", rest)
    rest = re.sub(r"<command-message>\s*.*?\s*</command-message>", "", rest, flags=re.S)
    rest = COMMAND_TAG.sub("", rest).strip()
    head = (name.group(1) + " " + (args.group(1) if args else "")).strip()
    return (head + ("\n" + rest if rest else "")).strip()


def _clip(text, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _one_line(text, limit: int = MAX_LABEL_CHARS) -> str:
    return _clip(" ".join((text or "").split()), limit)


def _epoch(ts):
    if not isinstance(ts, str) or not ts.strip():
        return None
    try:
        dt = datetime.fromisoformat(ts.strip().replace("Z", "+00:00"))
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    try:
        return int(dt.timestamp())
    except Exception:
        return None


def _blocks(message):
    """The content blocks of a message, whatever shape it arrived in."""
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return [b for b in content if isinstance(b, dict)]
    return []


def _tool_label(block) -> tuple:
    """(kind, label, detail) for one tool_use block."""
    name = block.get("name")
    name = name if isinstance(name, str) and name.strip() else "(unnamed tool)"
    args = block.get("input") if isinstance(block.get("input"), dict) else {}
    target = ""
    for key in TARGET_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            target = value
            break
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            target = str(value)
            break
    kind = "file" if name.split("__")[-1].lower() in WRITE_TOOLS and target else "tool"
    label = _one_line(f"{name} {target}".strip())
    return kind, label, _clip(_one_line(target, MAX_DETAIL_CHARS), MAX_DETAIL_CHARS)


def _is_human(rec) -> bool:
    """Did a person type this? Several fields say so and none is guaranteed to
    be present, so agreement of any one of them is enough and absence of all of
    them is not treated as proof either way - it just is not a `prompt`."""
    if rec.get("isMeta"):
        return False
    origin = rec.get("origin")
    if isinstance(origin, dict) and origin.get("kind") == "human":
        return True
    return rec.get("promptSource") in ("typed", "suggestion_accepted", "queued")


def turn_from_record(rec):
    """One normalized turn, or None for a record that carries no conversation.

    A turn is one content-bearing record. It carries the flattened text that
    search reads, and zero or more MARK POINTS - the addressable things that
    happened in it.
    """
    if not isinstance(rec, dict):
        return None
    rtype = rec.get("type")
    ts = rec.get("timestamp") if isinstance(rec.get("timestamp"), str) else None
    base = {
        "uuid": rec.get("uuid") if isinstance(rec.get("uuid"), str) else None,
        "parent_uuid": rec.get("parentUuid") if isinstance(rec.get("parentUuid"), str) else None,
        "ts": ts,
        "epoch": _epoch(ts),
        "sidechain": bool(rec.get("isSidechain")),
        "meta": bool(rec.get("isMeta")),
        "cwd": rec.get("cwd") if isinstance(rec.get("cwd"), str) else None,
        "git_branch": rec.get("gitBranch") if isinstance(rec.get("gitBranch"), str) else None,
        "version": rec.get("version") if isinstance(rec.get("version"), str) else None,
        "session_id": rec.get("sessionId") if isinstance(rec.get("sessionId"), str) else None,
        "marks": [],
        "text": "",
        "reminders_stripped": 0,
    }

    if rtype == "system":
        if rec.get("subtype") != "compact_boundary":
            return None
        md = rec.get("compactMetadata")
        md = md if isinstance(md, dict) else {}
        trigger = md.get("trigger") if isinstance(md.get("trigger"), str) else "unknown"
        extra = {k: md.get(k) for k in
                 ("trigger", "preTokens", "postTokens", "cumulativeDroppedTokens", "durationMs")
                 if isinstance(md.get(k), (str, int, float))}
        dropped = extra.get("cumulativeDroppedTokens")
        base["role"] = "system"
        base["text"] = ""
        base["marks"] = [{
            "kind": "compaction",
            "label": _one_line(
                f"context compacted ({trigger})"
                + (f", {dropped} tokens dropped" if isinstance(dropped, int) else "")),
            "detail": trigger,
            "extra": extra,
        }]
        return base

    if rtype == "user":
        blocks = _blocks(rec.get("message"))
        if any(b.get("type") == "tool_result" for b in blocks):
            return None            # tool output: see TOOL_RESULTS_NOT_INDEXED
        raw = "\n".join(b.get("text") or "" for b in blocks if b.get("type") == "text")
        if not isinstance(raw, str):
            raw = ""
        cleaned, n = SYSTEM_REMINDER.subn("", raw)
        cleaned = _unwrap_command(cleaned.strip()).strip()
        if not cleaned:
            return None
        base["role"] = "user"
        base["reminders_stripped"] = n
        base["text"] = _clip(cleaned, MAX_TURN_CHARS)
        base["text_chars"] = len(cleaned)
        if rec.get("isCompactSummary"):
            kind = "recap"
        elif _is_human(rec):
            kind = "prompt"
        else:
            kind = "message"
        base["marks"] = [{"kind": kind, "label": _one_line(cleaned),
                          "detail": rec.get("promptSource") if isinstance(
                              rec.get("promptSource"), str) else "", "extra": None}]
        return base

    if rtype == "assistant":
        blocks = _blocks(rec.get("message"))
        prose, marks, lines = [], [], []
        for b in blocks:
            btype = b.get("type")
            if btype == "text":
                text = b.get("text")
                if isinstance(text, str) and text.strip():
                    prose.append(text)
            elif btype == "tool_use":
                kind, label, detail = _tool_label(b)
                marks.append({"kind": kind, "label": label, "detail": detail, "extra": None})
                lines.append(label)
        prose_text = "\n".join(prose).strip()
        if prose_text:
            marks.insert(0, {"kind": "answer", "label": _one_line(prose_text),
                             "detail": "", "extra": None})
        if not marks:
            return None            # a thinking-only record carries nothing indexable
        # Tool labels join the searchable text on purpose: "which turn touched
        # worklog.py" is a search, not a separate query.
        full = "\n".join(([prose_text] if prose_text else []) + lines)
        base["role"] = "assistant"
        base["text"] = _clip(full, MAX_TURN_CHARS)
        base["text_chars"] = len(full)
        base["marks"] = marks
        model = (rec.get("message") or {}).get("model") if isinstance(rec.get("message"), dict) else None
        base["model"] = model if isinstance(model, str) else None
        return base

    return None


def session_title(rec):
    """The harness's own generated title for a session, if this record is one."""
    if isinstance(rec, dict) and rec.get("type") == "ai-title":
        title = rec.get("aiTitle")
        if isinstance(title, str) and title.strip():
            return _one_line(title)
    return None


# --------------------------------------------------------------------------- #
# reading a transcript from a byte offset
# --------------------------------------------------------------------------- #

class Scan:
    """Turns appended to a transcript since `offset`, and nothing before it.

    Iterate it, then read the counters. `consumed` is the offset to remember:
    it advances only past COMPLETE lines, so a final line still being written is
    left for the next refresh rather than indexed half-parsed.
    """

    def __init__(self, path, offset: int = 0):
        self.path = str(path)
        self.offset = max(int(offset or 0), 0)
        self.consumed = self.offset
        self.lines = 0
        self.malformed = 0
        self.oversized = 0
        self.partial_tail = False
        self.first_uuid = None
        self.title = None
        self.error = None

    def _open(self):
        return open(self.path, "rb")

    def head_uuid(self):
        """The uuid of the file's FIRST record - a fingerprint. A transcript is
        append-only, so if this ever changes the file was replaced, and every
        byte offset remembered about it is meaningless."""
        try:
            with self._open() as fh:
                line = fh.readline(MAX_LINE_BYTES)
            rec = json.loads(line.decode("utf-8", "replace"))
            if isinstance(rec, dict) and isinstance(rec.get("uuid"), str):
                return rec["uuid"]
            if isinstance(rec, dict) and isinstance(rec.get("sessionId"), str):
                return "session:" + rec["sessionId"]
        except Exception:
            return None
        return None

    def __iter__(self):
        try:
            fh = self._open()
        except Exception as exc:
            self.error = f"could not read {self.path}: {exc}"
            return
        try:
            try:
                fh.seek(self.offset)
            except Exception:
                self.consumed = 0
                fh.seek(0)
            while True:
                try:
                    raw = fh.readline(MAX_LINE_BYTES)
                except Exception as exc:
                    self.error = f"could not read {self.path}: {exc}"
                    return
                if not raw:
                    return
                if not raw.endswith(b"\n"):
                    if len(raw) < MAX_LINE_BYTES:
                        # The line is still being written. Do NOT consume it:
                        # the next refresh picks it up whole.
                        self.partial_tail = True
                        return
                    # It hit the size cap instead, so it is one enormous record.
                    # Skipping it must still CONSUME it - stopping here would
                    # park the offset in front of it and re-read the same
                    # megabytes on every refresh from now on, which is a session
                    # permanently wedged rather than one record skipped.
                    skipped = len(raw)
                    while True:
                        try:
                            more = fh.readline(MAX_LINE_BYTES)
                        except Exception as exc:
                            self.error = f"could not read {self.path}: {exc}"
                            return
                        if not more:
                            # The file ends inside the oversized record: it is
                            # still being written after all. Consume nothing.
                            self.partial_tail = True
                            return
                        skipped += len(more)
                        if more.endswith(b"\n"):
                            break
                    self.consumed += skipped
                    self.lines += 1
                    self.oversized += 1
                    continue
                self.consumed += len(raw)
                self.lines += 1
                try:
                    rec = json.loads(raw.decode("utf-8", "replace"))
                except Exception:
                    self.malformed += 1
                    continue
                if not isinstance(rec, dict):
                    self.malformed += 1
                    continue
                title = session_title(rec)
                if title:
                    self.title = title
                try:
                    turn = turn_from_record(rec)
                except Exception:
                    # A record shaped in a way this parser did not anticipate is
                    # one lost turn, never a failed index.
                    self.malformed += 1
                    continue
                if turn is not None:
                    yield turn
        finally:
            try:
                fh.close()
            except Exception:
                pass
