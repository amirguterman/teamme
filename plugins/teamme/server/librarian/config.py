"""Per-project librarian configuration: which librarians are on, and whether the
record is committed.

One file, `.claude/librarians/config.json`, owned by the MCP server rather than
by hand-editing:

    {
      "librarians": { "history": { "enabled": true } },
      "commit_record": false
    }

Two properties matter more than anything this file does:

1. `load()` NEVER raises and never guesses. Missing, unreadable, unparseable,
   the wrong shape, a librarian name this release has never heard of - every
   one of them degrades to the DOCUMENTED defaults (every known librarian
   enabled, `commit_record` false) and says why in `problems`. Same posture as
   the server's `manifest_version()`: an honest known-good beats a confidently
   wrong answer, and beats an exception on the way to answering at all.

2. `commit_record` is ENACTED, not merely recorded. Flipping it writes or
   removes the project's `.gitignore` entry for the append-only record, so the
   choice takes effect instead of sitting in a file nobody reads. The entry is
   written inside a marked block and ONLY lines inside that block are ever
   removed - someone else's ignore rule that happens to match the same path is
   not ours to delete.

The `.db` is a separate matter and is not a choice: it is always ignored,
whatever `commit_record` says, because a binary index cannot be merged. So is
`.claude/librarians/sessions/`, for a stronger reason - it indexes conversation
text, and `commit_record` must not be able to put that in a repository. And so
is the `indexed_head` marker, which says what THIS machine has indexed: sharing
it would hand a teammate a marker that is already behind the commit carrying it.

Neither of those two depends on anyone calling the configure tool. `ensure_ignored()`
is called from `store.connect_file()` - the one funnel every librarian index is
opened through - so the block is in place before the first byte of an index
exists, on every path, including a refresh or a query by a user who never
touches the configuration at all.

Stdlib only.
"""

import json
import os
import pathlib

try:
    from . import store
except ImportError:  # loaded as a loose module rather than a package member
    import store  # type: ignore

# Known to THIS release. Callers that have their own list (the MCP server does)
# pass it in; this default only exists so the module is usable on its own.
KNOWN_LIBRARIANS = ("history", "sessions")

DEFAULT_ENABLED = True
DEFAULT_COMMIT_RECORD = False

CONFIG_NAME = "config.json"

# The .gitignore block. Everything between these two lines is teamme's to
# rewrite; everything outside them is the user's and is never touched.
GITIGNORE_BEGIN = "# teamme librarians - managed by the teamme_librarian_configure tool"
GITIGNORE_END = "# end teamme librarians"
# A second line inside the block, not a second marker: _strip_blocks() keys on
# GITIGNORE_BEGIN alone, so a block written by an older release is still
# recognized and rewritten rather than duplicated. It is here because the block
# is no longer only written by the configure tool - a refresh writes it too, and
# a user reading their own .gitignore deserves to know what put it there.
GITIGNORE_NOTE = ("# Written automatically when a librarian index is created. Everything between "
                  "these two markers is teamme's; nothing outside them is ever removed.")

# Always ignored, regardless of commit_record - three entries, for three
# different reasons, none of them a fork the user gets to take:
#
#   the .db     a SQLite file is binary and unmergeable, so two people indexing
#               different commits produce irreconcilable files.
#   sessions/   the session librarian indexes CONVERSATIONS. A transcript holds
#               everything anyone typed, including a secret pasted in by
#               accident, and an index of one is a second copy of that. It is
#               machine-local by construction and commit_record does not reach
#               it. The whole directory is ignored rather than a file inside it,
#               so nothing a later release adds under there can leak by being
#               forgotten here.
#   indexed_head  machine-local derived state, like the .db: it says what THIS
#               machine has indexed, which is not a fact about the project. And
#               committing it is loop fuel for the freshness gate it feeds - the
#               commit that carries a refreshed marker is itself unindexed, so
#               the marker would arrive on a teammate's machine already behind
#               the HEAD it was committed with.
DB_IGNORE = ".claude/librarians/index.db"
SESSIONS_IGNORE = ".claude/librarians/sessions/"
MARKER_IGNORE = ".claude/librarians/*/indexed_head"
ALWAYS_IGNORED = (DB_IGNORE, SESSIONS_IGNORE, MARKER_IGNORE)
# The append-only record. Ignored only when commit_record is false.
RECORD_IGNORE = ".claude/librarians/*/commits.jsonl"

IGNORE_REASONS = {
    DB_IGNORE: ("# the SQLite index is derived from the record and rebuildable from it; binary, "
                "so never committed whatever commit_record says"),
    SESSIONS_IGNORE: ("# the session index holds conversation text; machine-local always, and "
                      "commit_record does not apply to it"),
    MARKER_IGNORE: ("# what this machine has indexed, not a fact about the project - derived like "
                    "the .db, and committing it would re-arm the freshness gate that asked for "
                    "the refresh"),
    RECORD_IGNORE: ("# commit_record is false: the librarian record stays out of git. "
                    "Flip it with teamme_librarian_configure, not by hand"),
}


# --------------------------------------------------------------------------- #
# paths
# --------------------------------------------------------------------------- #

def config_path(project_dir=None) -> pathlib.Path:
    return store.librarians_dir(project_dir) / CONFIG_NAME


def gitignore_path(project_dir=None) -> pathlib.Path:
    return store.project_root(project_dir) / ".gitignore"


# --------------------------------------------------------------------------- #
# reading
# --------------------------------------------------------------------------- #

def defaults(names=None) -> dict:
    names = tuple(names or KNOWN_LIBRARIANS)
    return {
        "librarians": {n: {"enabled": DEFAULT_ENABLED} for n in names},
        "commit_record": DEFAULT_COMMIT_RECORD,
    }


def load(project_dir=None, names=None) -> dict:
    """The effective configuration. Never raises, never returns a partial shape.

    Always returns every key a caller could ask for, so no caller has to guard:

      librarians       {name: {"enabled": bool}} for every KNOWN name, and only
                       known names - an entry for a librarian this release does
                       not ship is ignored rather than fatal, and is reported in
                       `unknown`
      commit_record    bool
      exists           whether config.json is on disk at all
      source           "file" or "defaults" - what the answer actually came from
      problems         [] when the file was clean; otherwise every reason a
                       documented default was substituted
      unknown          librarian names present in the file that this release
                       does not ship
      extra            keys in the file that are not ours, preserved on write
    """
    names = tuple(names or KNOWN_LIBRARIANS)
    out = defaults(names)
    out.update({
        "path": str(config_path(project_dir)),
        "exists": False,
        "source": "defaults",
        "problems": [],
        "unknown": [],
        "extra": {},
    })

    try:
        path = config_path(project_dir)
        out["path"] = str(path)
        if not path.is_file():
            return out
        out["exists"] = True
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return out
    except Exception as exc:
        # Unreadable, not UTF-8, not JSON, a directory where a file should be -
        # all one answer: the documented defaults, plus the reason.
        out["problems"].append(f"{CONFIG_NAME} could not be read ({exc}); using defaults")
        return out

    if not isinstance(raw, dict):
        out["problems"].append(
            f"{CONFIG_NAME} is {type(raw).__name__}, not a JSON object; using defaults"
        )
        return out

    out["source"] = "file"

    libs = raw.get("librarians")
    if libs is None:
        pass  # absent is not a problem: every known librarian keeps its default
    elif not isinstance(libs, dict):
        out["problems"].append(
            f"`librarians` is {type(libs).__name__}, not an object; every librarian keeps "
            f"its default (enabled={str(DEFAULT_ENABLED).lower()})"
        )
    else:
        for key, value in libs.items():
            name = str(key).strip().lower()
            if name not in names:
                out["unknown"].append(str(key))
                continue
            if isinstance(value, bool):
                out["librarians"][name]["enabled"] = value          # {"history": true}
            elif isinstance(value, dict):
                flag = value.get("enabled", DEFAULT_ENABLED)
                if isinstance(flag, bool):
                    out["librarians"][name]["enabled"] = flag
                else:
                    out["problems"].append(
                        f"librarians.{name}.enabled is {type(flag).__name__}, not true/false; "
                        f"treating {name} as enabled={str(DEFAULT_ENABLED).lower()}"
                    )
            else:
                out["problems"].append(
                    f"librarians.{name} is {type(value).__name__}, not an object; "
                    f"treating it as enabled={str(DEFAULT_ENABLED).lower()}"
                )

    if "commit_record" in raw:
        flag = raw.get("commit_record")
        if isinstance(flag, bool):
            out["commit_record"] = flag
        else:
            out["problems"].append(
                f"commit_record is {type(flag).__name__}, not true/false; "
                f"treating it as {str(DEFAULT_COMMIT_RECORD).lower()}"
            )

    # Anything else in the file belongs to a release we are not - keep it, so a
    # newer version's key does not get silently dropped by an older one.
    out["extra"] = {k: v for k, v in raw.items() if k not in ("librarians", "commit_record")}
    if out["unknown"]:
        out["problems"].append(
            "ignored librarian name(s) this release does not ship: " + ", ".join(out["unknown"])
        )
    return out


def enabled(project_dir=None, name: str = "history", names=None) -> bool:
    """True unless the config says otherwise. Anything unexpected reads as True:
    a configuration that cannot be read must not be the thing that switches a
    librarian off."""
    try:
        cfg = load(project_dir, names)
        entry = cfg["librarians"].get(str(name).strip().lower())
        if isinstance(entry, dict) and isinstance(entry.get("enabled"), bool):
            return entry["enabled"]
    except Exception:
        pass
    return DEFAULT_ENABLED


# --------------------------------------------------------------------------- #
# writing
# --------------------------------------------------------------------------- #

def _serialize(cfg: dict, names) -> str:
    body = dict(cfg.get("extra") or {})
    body["librarians"] = {n: {"enabled": bool(cfg["librarians"][n]["enabled"])} for n in names}
    body["commit_record"] = bool(cfg.get("commit_record"))
    return json.dumps(body, indent=2, sort_keys=False) + "\n"


def save(project_dir, cfg: dict, names=None) -> dict:
    """Write config.json atomically. Returns {"ok": bool, "problem": str|None}."""
    names = tuple(names or KNOWN_LIBRARIANS)
    path = config_path(project_dir)
    tmp = path.with_name(f".{CONFIG_NAME}.{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with store.lock(path.with_name("config.lock")):
            tmp.write_text(_serialize(cfg, names), encoding="utf-8")
            os.replace(str(tmp), str(path))
        return {"ok": True, "problem": None}
    except Exception as exc:
        try:
            tmp.unlink()
        except Exception:
            pass
        return {"ok": False, "problem": f"could not write {path}: {exc}"}


# --------------------------------------------------------------------------- #
# enacting commit_record: the project's .gitignore
#
# The rule that matters here is NOT idempotence - it is that we only ever delete
# our own lines. A user whose .gitignore already ignores commits.jsonl for their
# own reasons must still have that line after we flip commit_record on. So
# everything we write goes between two marker comments, removal only ever takes
# whole marked blocks, and a block whose end marker somebody deleted is left
# alone and reported rather than guessed at.
# --------------------------------------------------------------------------- #

def _strip_blocks(lines):
    """(lines with every complete teamme block removed, count, mangled?)."""
    out, removed, i, mangled = [], 0, 0, False
    while i < len(lines):
        if lines[i].strip() == GITIGNORE_BEGIN:
            j = i + 1
            while j < len(lines) and lines[j].strip() != GITIGNORE_END:
                j += 1
            if j >= len(lines):
                mangled = True          # no end marker: not ours to guess at
                out.extend(lines[i:])
                return out, removed, mangled
            removed += 1
            i = j + 1
            continue
        out.append(lines[i])
        i += 1
    return out, removed, mangled


def apply_gitignore(project_dir, commit_record: bool) -> dict:
    """Make the project's .gitignore match `commit_record`.

    commit_record false -> the record is ignored (the default: an index that
    goes stale on every commit that does not refresh it is worse than no index
    in the repo at all).
    commit_record true  -> the record is NOT ignored, so it can be committed.

    The .db line is written either way; it is never a choice. A path already
    ignored by an identical line OUTSIDE our block is not repeated inside it.

    Returns {"ok", "path", "changed", "wrote", "notes", "problem"} and never raises.
    """
    path = gitignore_path(project_dir)
    result = {"ok": True, "path": str(path), "changed": False, "wrote": [], "notes": [],
              "problem": None}
    # Read-modify-write, and since ensure_ignored() calls this every time an
    # index is opened, two refreshes can now reach it at once. The lock is the
    # same best-effort one every other write here uses: it fails open rather
    # than refusing, and the worst case it leaves is a duplicated block that the
    # next call collapses - not a .gitignore truncated between a reader and a
    # writer, which would silently drop the user's own lines.
    with store.lock(store.librarians_dir(project_dir) / "gitignore.lock"):
        return _apply_gitignore_locked(path, result, commit_record)


def _apply_gitignore_locked(path, result, commit_record: bool) -> dict:
    try:
        text = path.read_text(encoding="utf-8") if path.is_file() else ""
    except Exception as exc:
        result.update(ok=False, problem=f"could not read {path}: {exc}")
        return result

    original = text
    lines = text.splitlines()
    kept, _removed, mangled = _strip_blocks(lines)
    if mangled:
        result.update(
            ok=False,
            problem=(
                f"the teamme block in {path} has a '{GITIGNORE_BEGIN}' line with no "
                f"'{GITIGNORE_END}' line after it. Nothing was changed: teamme only ever removes "
                f"lines between its own markers, and will not guess where an edited block ends. "
                f"Restore the end marker (or delete the block by hand) and run this tool again."
            ),
        )
        return result

    existing = {ln.strip() for ln in kept}
    wanted = [entry for entry in ALWAYS_IGNORED if entry not in existing]
    if not commit_record and RECORD_IGNORE not in existing:
        wanted.append(RECORD_IGNORE)

    # Say out loud when somebody else's identical rule is doing the ignoring.
    # Silence here would be the worst outcome of the never-delete-a-line-you-did-
    # not-write rule: commit_record true, the record still ignored, and no reason
    # given anywhere.
    for entry in ALWAYS_IGNORED:
        if entry in existing:
            result["notes"].append(
                f"{entry} is already ignored by a line outside teamme's block; left as it is "
                f"rather than repeated inside it")
    if RECORD_IGNORE in existing:
        result["notes"].append(
            (f"the record is STILL IGNORED by `{RECORD_IGNORE}`, a line outside teamme's block. "
             f"teamme never removes an ignore rule it did not write, so commit_record=true will "
             f"not take effect until you remove that line yourself."
             if commit_record else
             f"the record is already ignored by `{RECORD_IGNORE}`, a line outside teamme's block; "
             f"left as it is rather than repeated inside it"))

    while kept and not kept[-1].strip():
        kept.pop()          # only trailing blank lines, so a re-run is byte-identical

    if wanted:
        block = [GITIGNORE_BEGIN, GITIGNORE_NOTE]
        for entry in wanted:
            block.append(IGNORE_REASONS.get(entry, "# managed by teamme"))
            block.append(entry)
        block.append(GITIGNORE_END)
        new_lines = kept + ([""] if kept else []) + block
        result["wrote"] = list(wanted)
    else:
        new_lines = kept

    new_text = "\n".join(new_lines) + ("\n" if new_lines else "")
    if new_text == original:
        return result
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(new_text, encoding="utf-8")
        result["changed"] = True
    except Exception as exc:
        result.update(ok=False, problem=f"could not write {path}: {exc}")
    return result


# --------------------------------------------------------------------------- #
# ensuring the block is there at all - the part 0.6.0 shipped missing
#
# apply_gitignore() above ENACTS a choice: the user said commit_record, and the
# file is made to agree. This is a different job, and it is not a choice at all.
# The .db and the whole sessions/ directory are ignored unconditionally, and
# they have to be ignored BEFORE the first byte of an index exists - not as a
# side effect of an unrelated configuration call that most users never make.
#
# In 0.6.0 apply_gitignore() had exactly one call site, inside configure(), and
# only when commit_record was passed. teamme_librarian_refresh never reached it.
# Both the changelog and the librarian's own prompt tell a user there is "no
# data until the first teamme_librarian_refresh", so the documented path was
# precisely the one that left an index of their conversations sitting in a file
# git would happily add - while the docs promised that directory was always
# ignored. ensure_ignored() is called from store.connect_file(), the single
# funnel every librarian index is opened through, so the guarantee does not
# depend on a future librarian's author remembering it.
# --------------------------------------------------------------------------- #

def ensure_ignored(project_dir=None) -> dict:
    """Make sure teamme's ignore block is in place in `project_dir`. Never raises.

    Idempotent and cheap enough to call every time an index is opened: it
    re-derives the block from the CURRENT config, so it ENACTS commit_record
    rather than overriding it, and writes nothing at all when the file already
    says what it should.

    What it creates: the project's .gitignore, and - through the lock
    apply_gitignore takes - `.claude/librarians/`, which every caller of this
    function is about to create anyway, because the only callers are the paths
    that write an index. It never creates the PROJECT directory: a project_dir
    that does not exist is reported, not brought into being, so a read-only
    query can never be the thing that conjures a project. (Read-only paths -
    status, and a query against an index that does not exist yet - do not reach
    this function at all; they return before anything is opened.)

    Returns the apply_gitignore() shape: {"ok", "path", "changed", "wrote",
    "notes", "problem"}. `ok` false means the index is NOT protected, and every
    caller that is about to write one is expected to say so out loud.
    """
    result = {"ok": False, "path": None, "changed": False, "wrote": [], "notes": [],
              "problem": None}
    try:
        root = store.project_root(project_dir)
        result["path"] = str(root / ".gitignore")
        if not root.is_dir():
            result["problem"] = (
                f"{root} is not an existing directory, so teamme's ignore rule could not be "
                f"written there. Nothing was created.")
            return result
        out = apply_gitignore(root, bool(load(root)["commit_record"]))
        if not (root / ".git").exists():
            # Deliberate, not an oversight: absence of .git here does NOT mean
            # nothing is tracking this directory. It can sit inside a parent
            # repository, or be `git init`ed tomorrow with the index already on
            # disk. An unused .gitignore costs a few bytes; the other mistake
            # costs someone their conversation history. So the rule is written
            # either way, and the situation is stated rather than guessed at.
            out["notes"].append(
                f"there is no .git in {root}: the ignore rule was written anyway, because a "
                f"directory with no repository of its own can still sit inside one, or become "
                f"one later with the index already on disk")
        return out
    except Exception as exc:
        result["problem"] = f"teamme's ignore rule could not be put in place: {exc}"
        return result


def ignored_entries(project_dir=None) -> dict:
    """Read-only: which of teamme's ignore entries are present in .gitignore.

    For status and other callers that must not write. Reports what the file
    says; `git check-ignore` is the only real ground truth, and this deliberately
    does not shell out to git for a status line.
    """
    out = {"path": None, "exists": False, "present": [], "missing": [], "problem": None}
    try:
        path = gitignore_path(project_dir)
        out["path"] = str(path)
        text = path.read_text(encoding="utf-8") if path.is_file() else None
        if text is None:
            out["missing"] = list(ALWAYS_IGNORED)
            return out
        out["exists"] = True
        lines = {ln.strip() for ln in text.splitlines()}
        for entry in ALWAYS_IGNORED:
            (out["present"] if entry in lines else out["missing"]).append(entry)
    except Exception as exc:
        out["problem"] = f"could not read {out['path']}: {exc}"
    return out


# --------------------------------------------------------------------------- #
# the one entry point the tool calls
# --------------------------------------------------------------------------- #

def configure(project_dir=None, librarian=None, enable=None, commit_record=None,
              names=None, rejected=None) -> dict:
    """Read, or change and read back. Never raises.

    With no arguments this reports and writes nothing at all - not even
    config.json - so asking what the configuration is can never be the thing
    that creates it.

    Returns the load() shape plus:
      changes    human-readable list of what actually moved ([] for a read)
      written    whether config.json was written
      gitignore  the apply_gitignore() result, or None if commit_record was not
                 passed (an unchanged value is still re-enacted: the file may
                 have been edited since, and re-enacting is idempotent)
      problems   carries the write/enact failures too, so one field holds
                 everything that went sideways
    """
    names = tuple(names or KNOWN_LIBRARIANS)
    cfg = load(project_dir, names)
    cfg["changes"] = []
    cfg["written"] = False
    cfg["gitignore"] = None
    for note in rejected or []:      # arguments the caller sent in the wrong type
        cfg["problems"].append(note)

    touch = False

    if enable is not None:
        name = str(librarian or names[0]).strip().lower()
        if name not in names:
            cfg["problems"].append(
                f"unknown librarian '{librarian}'; nothing was changed. Available: "
                + ", ".join(names)
            )
            return cfg
        was = cfg["librarians"][name]["enabled"]
        cfg["librarians"][name]["enabled"] = bool(enable)
        touch = True
        cfg["changes"].append(
            f"{name}: enabled {str(was).lower()} -> {str(bool(enable)).lower()}"
            if was != bool(enable) else
            f"{name}: already enabled={str(was).lower()}, unchanged"
        )

    if commit_record is not None:
        was = cfg["commit_record"]
        cfg["commit_record"] = bool(commit_record)
        touch = True
        cfg["changes"].append(
            f"commit_record: {str(was).lower()} -> {str(bool(commit_record)).lower()}"
            if was != bool(commit_record) else
            f"commit_record: already {str(was).lower()}, unchanged"
        )

    if touch:
        wrote = save(project_dir, cfg, names)
        cfg["written"] = bool(wrote["ok"])
        if not wrote["ok"]:
            cfg["problems"].append(wrote["problem"])
            # Report what is actually in force, not what was asked for: a change
            # that could not be saved has not happened, and saying "disabled"
            # about a librarian that is still running would be the worst kind of
            # wrong. For the same reason the .gitignore is left alone - enacting
            # a setting that was never recorded would leave the two disagreeing.
            fresh = load(project_dir, names)
            cfg["librarians"] = fresh["librarians"]
            cfg["commit_record"] = fresh["commit_record"]
            cfg["exists"] = fresh["exists"]
            cfg["source"] = fresh["source"]
            cfg["changes"] = [c + "  -- NOT SAVED, still in force as reported above"
                              for c in cfg["changes"]]
            if commit_record is not None:
                cfg["problems"].append(
                    ".gitignore was left untouched, because a commit_record that could not be "
                    "saved must not be enacted behind the config file's back")
            return cfg
        cfg["exists"] = True
        cfg["source"] = "file"

    if commit_record is not None:
        enacted = apply_gitignore(project_dir, bool(commit_record))
        cfg["gitignore"] = enacted
        if not enacted["ok"] and enacted.get("problem"):
            cfg["problems"].append(enacted["problem"])

    return cfg
