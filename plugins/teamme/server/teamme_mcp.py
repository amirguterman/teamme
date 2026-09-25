#!/usr/bin/env python3
"""teamme's MCP server: the visible gate on the whole plugin.

Stdlib only, stdio, newline-delimited JSON-RPC 2.0. There is no `mcp` package
here on purpose - the plugin must work on a machine with nothing installed but
python3, and this server existing IS the check for that: if python3 is missing
or broken, the process never starts and the harness shows `teamme: failed to
connect` in /mcp. That is the signal. Everything below only has to get the
gating right once the process can run at all.

Tools:
  teamme_status        always available - the preflight diagnosis, imported from
                       templates/hooks/preflight.py, never reimplemented here
                       (not-installed / installed-outdated / installed-not-live /
                       live, plus "unknown" if preflight itself cannot be loaded)
  teamme_install       scaffolds or repairs: copies missing hook scripts, creates
                       .claude/intake/, merges the hooks block into
                       .claude/settings.json without clobbering anything already
                       there. Idempotent; overwrites an existing file only with
                       force=true
  teamme_worklog       GATED on the scaffolding being installed
  teamme_intake_phase  GATED on the scaffolding being installed
  teamme_librarian_*   status / refresh / query / configure over the librarian indexes,
                       implemented in server/librarian/ and NOT gated on the
                       scaffolding - an index of this repository's own history
                       does not depend on intake.md existing. It needs a git
                       repository, and says so when there is not one. refresh
                       and query additionally REFUSE for a librarian this
                       project has disabled; status and configure never do - a
                       status tool that will not report status, or a switch you
                       cannot reach to switch back on, would both be traps

The two gated tools refuse before install and name `teamme_install` in the
refusal, so a model that reaches for the work log in an unscaffolded project is
told exactly what to run instead of silently writing state nothing reads.

A refusal is a tool result with isError, never a protocol error and never a
crash: this server cannot deny a file write or block a prompt - it has no hook
registration at all.
"""

import json
import os
import pathlib
import re
import shutil
import subprocess
import sys

SERVER_NAME = "teamme"
DEFAULT_PROTOCOL = "2025-06-18"
KNOWN_PROTOCOLS = {"2024-11-05", "2025-03-26", "2025-06-18"}
CALL_TIMEOUT = 30

PLUGIN_ROOT = pathlib.Path(__file__).resolve().parent.parent

UNKNOWN_VERSION = "0.0.0+unknown"


def manifest_version(root=None) -> str:
    """The plugin's version, read live from plugin.json - never a second copy.

    The manifest is the single source of truth for the version; a number
    duplicated here would drift on the next bump, exactly as it did through
    0.2.0 and 0.3.0. Resolved from this file's own directory, since the
    harness launches the server from an arbitrary working directory.

    Degrades to UNKNOWN_VERSION - missing, unreadable, unparseable, wrong
    shape, or no usable `version` key - and never raises. An honest unknown
    in the handshake beats a confidently wrong number, and beats a server
    that dies before it can answer at all.
    """
    try:
        base = pathlib.Path(root) if root is not None else PLUGIN_ROOT
        manifest = json.loads(
            (base / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
        )
        version = manifest.get("version") if isinstance(manifest, dict) else None
        if isinstance(version, str) and version.strip():
            return version.strip()
    except Exception:
        pass
    return UNKNOWN_VERSION


SERVER_VERSION = manifest_version()

_PREFLIGHT = None


# --------------------------------------------------------------------------- #
# plumbing
# --------------------------------------------------------------------------- #

def templates_dir() -> pathlib.Path:
    """Where the scaffolding lives inside the plugin."""
    local = PLUGIN_ROOT / "templates"
    if local.is_dir():
        return local
    env = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if env:
        cand = pathlib.Path(env) / "templates"
        if cand.is_dir():
            return cand
    return local


def preflight(project_dir=None):
    """The one implementation of the check logic, loaded from the plugin's own
    templates. Falls back to the project's installed copy if the plugin's is
    missing, and to None if neither can be loaded."""
    global _PREFLIGHT
    if _PREFLIGHT is not None:
        return _PREFLIGHT
    import importlib.util

    candidates = [templates_dir() / "hooks" / "preflight.py"]
    if project_dir:
        candidates.append(pathlib.Path(project_dir) / ".claude" / "hooks" / "preflight.py")
    for path in candidates:
        try:
            if not path.is_file():
                continue
            spec = importlib.util.spec_from_file_location("teamme_preflight", path)
            if spec is None or spec.loader is None:
                continue
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            _PREFLIGHT = mod
            return mod
        except Exception:
            continue
    return None


def required_hooks(project_dir=None) -> tuple:
    """The hook scripts an install must end up with, read from preflight.py's
    REQUIRED_HOOKS - that tuple is the single authority. install() copies from
    templates/hooks/ and reports anything REQUIRED_HOOKS names that the plugin
    does not actually ship, so the two lists can never silently drift into a
    permanently "missing", unrepairable hook."""
    mod = preflight(project_dir)
    names = getattr(mod, "REQUIRED_HOOKS", ()) if mod is not None else ()
    try:
        return tuple(n for n in names if isinstance(n, str) and n)
    except Exception:
        return ()


def hook_freshness(installed: pathlib.Path, template: pathlib.Path, project_dir=None) -> str:
    """'same' / 'differs' / 'unknown', delegated to preflight.py.

    Deliberately NOT a byte comparison written here: preflight's _check_hooks
    calls the same function, and two copies of "does this installed hook match
    the shipped one" is precisely how the installer came to skip a file it called
    `differs` while the health check called that install fresh. If preflight
    cannot be loaded at all, `unknown` keeps the conservative behaviour - an
    existing file is left alone unless force=true.
    """
    mod = preflight(project_dir)
    fn = getattr(mod, "hook_freshness", None) if mod is not None else None
    if fn is None:
        return "unknown"
    try:
        verdict = fn(installed, template)
    except Exception:
        return "unknown"
    return verdict if verdict in ("same", "differs", "unknown") else "unknown"


def resolve_project(args: dict) -> pathlib.Path:
    raw = (args or {}).get("project_dir") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    return pathlib.Path(raw).expanduser().resolve()


def diagnose(root: pathlib.Path) -> dict:
    mod = preflight(root)
    if mod is None:
        return {
            "ok": False,
            "installed": False,
            "state": "unknown",
            "project_dir": str(root),
            "checks": [],
            "summary": "preflight.py could not be loaded from the plugin - reinstall teamme",
        }
    try:
        # The server always knows where the shipped templates are; the hook
        # usually does not. Handing the hint over is what lets teamme_status
        # check hook freshness at all. An older installed copy of preflight.py
        # (loaded as the fallback) takes one argument - fall back to that.
        try:
            return mod.diagnose(str(root), str(templates_dir()))
        except TypeError:
            return mod.diagnose(str(root))
    except Exception as exc:
        return {
            "ok": False,
            "installed": False,
            "state": "unknown",
            "project_dir": str(root),
            "checks": [],
            "summary": f"preflight failed: {exc}",
        }


def text_result(text: str, is_error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": bool(is_error)}


# --------------------------------------------------------------------------- #
# the librarian substrate
# --------------------------------------------------------------------------- #

_LIBRARIAN = None
_LIBRARIAN_ERROR = None

LIBRARIANS = ("history", "sessions")


def librarian():
    """(store, history, config, sessions, cross) from the plugin's server/librarian/, or None.

    The substrate lives in the PLUGIN and is never copied into a project: it is
    invoked here, so it can never join the list of hook scripts an install is
    required to have. That list growing is what made a working older install
    report itself uninstalled once before.
    """
    global _LIBRARIAN, _LIBRARIAN_ERROR
    if _LIBRARIAN is not None or _LIBRARIAN_ERROR is not None:
        return _LIBRARIAN
    try:
        import importlib
        here = str(pathlib.Path(__file__).resolve().parent)
        if here not in sys.path:
            sys.path.insert(0, here)
        _LIBRARIAN = (
            importlib.import_module("librarian.store"),
            importlib.import_module("librarian.history"),
            importlib.import_module("librarian.config"),
            importlib.import_module("librarian.sessions"),
            importlib.import_module("librarian.cross"),
        )
    except Exception as exc:
        _LIBRARIAN_ERROR = str(exc)
        _LIBRARIAN = None
    return _LIBRARIAN


def librarian_missing() -> dict:
    return text_result(
        "the librarian substrate could not be loaded from the plugin "
        f"({_LIBRARIAN_ERROR or 'server/librarian/ is missing'}). Nothing was read or written. "
        "Reinstall or update teamme.",
        is_error=True,
    )


def librarian_disabled(root: pathlib.Path, name: str, _config) -> dict:
    """The refusal result if `name` is switched off in this project, else None.

    A REFUSAL, not a harness guarantee - the same honesty the teamme_install
    gate owes. This server registers no hooks and cannot stop anything; a caller
    that ignores the refusal and reads the index another way is not prevented
    from doing so. What this does guarantee is that a project which has turned a
    librarian off never has that librarian's tools quietly answer as if it were
    on. Reading the config can never be what disables a librarian either: any
    unreadable or malformed config reads as ENABLED (see librarian/config.py).
    """
    try:
        if _config.enabled(root, name, LIBRARIANS):
            return None
    except Exception:
        return None  # fail open: a gate that cannot be read is not a gate
    return text_result(
        f"the {name} librarian is disabled in {root}, so this tool will not read or write its "
        f"index. Nothing happened.\n\n"
        f"Re-enable it with the `teamme_librarian_configure` tool:\n"
        f'  {{"librarian": "{name}", "enabled": true}}\n\n'
        f"`teamme_librarian_status` still works while a librarian is disabled and will show you "
        f"the current setting. The setting lives in .claude/librarians/config.json, per project.\n"
        f"This is a refusal by this tool, not a harness guarantee - teamme registers no hook that "
        f"could enforce it.",
        is_error=True,
    )


# --------------------------------------------------------------------------- #
# the gate
# --------------------------------------------------------------------------- #

def gate(root: pathlib.Path, script: str, tool: str):
    """None if the tool may run, else the refusal result that names teamme_install."""
    path = root / ".claude" / "hooks" / script
    if path.is_file():
        return None
    d = diagnose(root)
    return text_result(
        f"{tool} is unavailable: teamme is not installed in {root} "
        f"(state: {d.get('state', 'unknown')}).\n"
        f".claude/hooks/{script} does not exist, so there is nothing to read or write yet.\n\n"
        f"Run the `teamme_install` tool first. It copies the hook scripts, creates .claude/intake/ "
        f"and merges the hooks block into .claude/settings.json without touching anything already "
        f"there. Then run `teamme_status` to see what is still outstanding.",
        is_error=True,
    )


def run_script(root: pathlib.Path, script: str, argv: list) -> dict:
    """Delegate to the project's installed script - its CLI is the contract, and
    reimplementing it here is how two copies drift apart."""
    path = root / ".claude" / "hooks" / script
    env = dict(os.environ)
    env["CLAUDE_PROJECT_DIR"] = str(root)
    try:
        proc = subprocess.run(
            [sys.executable or "python3", str(path)] + [str(a) for a in argv],
            cwd=str(root),
            env=env,
            capture_output=True,
            text=True,
            timeout=CALL_TIMEOUT,
        )
    except Exception as exc:
        return text_result(f"could not run {script}: {exc}", is_error=True)
    out = (proc.stdout or "") + (proc.stderr or "")
    return text_result(out.strip() or f"{script} {' '.join(argv)}: no output", proc.returncode != 0)


# --------------------------------------------------------------------------- #
# install
# --------------------------------------------------------------------------- #

def _sig(command: str):
    """Identity of a hook command: which script, with which trailing arguments.
    Lets a re-install recognise its own entry even if the quoting changed."""
    scripts = re.findall(r"([A-Za-z0-9_.\-]+\.py)", command or "")
    if not scripts:
        return ("", command.strip())
    script = scripts[-1]
    tail = command.split(script, 1)[1] if script in command else ""
    args = tuple(t for t in re.findall(r"[A-Za-z0-9_\-]+", tail))
    return (script, args)


def _event_sigs(groups) -> set:
    found = set()
    if not isinstance(groups, list):
        return found
    for g in groups:
        if not isinstance(g, dict):
            continue
        for h in g.get("hooks") or []:
            if isinstance(h, dict) and isinstance(h.get("command"), str):
                found.add(_sig(h["command"]))
    return found


def merge_hooks(settings: dict, template: dict) -> list:
    """Append only the hook entries this project does not already have. Every
    other key in settings, and every hook the user added, is left untouched."""
    added = []
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}
        settings["hooks"] = hooks
    for event, groups in (template.get("hooks") or {}).items():
        if not isinstance(groups, list):
            continue
        existing = hooks.get(event)
        if not isinstance(existing, list):
            existing = []
            hooks[event] = existing
        have = _event_sigs(existing)
        for g in groups:
            sigs = _event_sigs([g])
            if sigs and sigs <= have:
                continue
            existing.append(g)
            have |= sigs
            for s in sigs:
                added.append(f"{event}: {s[0]} {' '.join(s[1])}".strip())
    return added


def install(root: pathlib.Path, force: bool = False) -> dict:
    changed, skipped, problems = [], [], []
    tdir = templates_dir()

    src_hooks = tdir / "hooks"
    dst_hooks = root / ".claude" / "hooks"
    try:
        dst_hooks.mkdir(parents=True, exist_ok=True)
        available = {p.name: p for p in src_hooks.glob("*.py")}
        scripts = [available[n] for n in sorted(available)]
        if not scripts:
            problems.append(f"no hook templates found in {src_hooks} - the plugin install looks broken")
        # Everything REQUIRED_HOOKS names must be shippable, or teamme_status
        # would report it missing forever with no way to repair it.
        unshipped = [n for n in required_hooks(root) if n not in available]
        if unshipped:
            problems.append(
                f"the plugin does not ship {', '.join(unshipped)}, but preflight requires "
                f"{'them' if len(unshipped) > 1 else 'it'} - teamme_status will keep reporting "
                f"{'those' if len(unshipped) > 1 else 'that'} as missing until teamme itself is "
                f"reinstalled or updated"
            )
        for src in scripts:
            dst = dst_hooks / src.name
            if dst.is_file():
                verdict = hook_freshness(dst, src, root)
                if verdict == "same":
                    skipped.append(f".claude/hooks/{src.name} (already current)")
                    continue
                if not force:
                    why = ("differs from the plugin's copy" if verdict == "differs"
                           else "could not be compared with the plugin's copy")
                    skipped.append(
                        f".claude/hooks/{src.name} ({why} - left as is; "
                        f"call again with force=true to overwrite)"
                    )
                    continue
                shutil.copyfile(src, dst)
                changed.append(f"replaced .claude/hooks/{src.name}")
                continue
            shutil.copyfile(src, dst)
            changed.append(f"copied .claude/hooks/{src.name}")
    except Exception as exc:
        problems.append(f"hook scripts: {exc}")

    try:
        d = root / ".claude" / "intake"
        if d.is_dir():
            skipped.append(".claude/intake/ (already exists)")
        else:
            d.mkdir(parents=True, exist_ok=True)
            changed.append("created .claude/intake/")
    except Exception as exc:
        problems.append(f".claude/intake/: {exc}")

    try:
        tpl_path = tdir / "settings.hooks.json"
        template = json.loads(tpl_path.read_text())
        sp = root / ".claude" / "settings.json"
        if sp.is_file():
            raw = sp.read_text()
            try:
                settings = json.loads(raw)
                if not isinstance(settings, dict):
                    raise ValueError("settings.json is not a JSON object")
            except Exception as exc:
                settings = None
                problems.append(
                    f".claude/settings.json could not be parsed ({exc}) - left exactly as it is "
                    f"rather than overwritten. Fix the JSON, then run teamme_install again."
                )
        else:
            settings = {}
        if settings is not None:
            added = merge_hooks(settings, template)
            if added:
                sp.parent.mkdir(parents=True, exist_ok=True)
                sp.write_text(json.dumps(settings, indent=2) + "\n")
                changed.append("merged into .claude/settings.json: " + "; ".join(added))
            else:
                skipped.append(".claude/settings.json (all hook entries already registered)")
    except Exception as exc:
        problems.append(f"settings merge: {exc}")

    if not (root / ".claude" / "commands" / "intake.md").is_file():
        problems.append(
            ".claude/commands/intake.md is still missing. It is written for this project rather "
            "than copied verbatim, so run /teamme:init-team to generate it."
        )

    d = diagnose(root)
    lines = [f"teamme_install in {root}", ""]
    lines.append("changed:")
    lines += [f"  - {c}" for c in changed] or ["  - nothing (already installed)"]
    if skipped:
        lines += ["", "left alone:"] + [f"  - {s}" for s in skipped]
    if problems:
        lines += ["", "still needs attention:"] + [f"  - {p}" for p in problems]
    lines += ["", f"state: {d.get('state')} - {d.get('summary')}"]
    if d.get("state") == "installed-not-live":
        lines.append(
            "The hooks are registered but have not fired yet. Approve them with /hooks or start a "
            "new session; the next session start records the heartbeat that proves they run."
        )
    elif d.get("state") == "installed-outdated":
        lines.append(
            "This project is still short part of the scaffolding - see the failing checks above. "
            "It IS an existing teamme install, so keep repairing it (teamme_install, with "
            "force=true if a hook script was left in place because it differs); do not run the "
            "installer over the team that is already here."
        )
    return text_result("\n".join(lines))


# --------------------------------------------------------------------------- #
# tools
# --------------------------------------------------------------------------- #

PROJECT_DIR_PROP = {
    "project_dir": {
        "type": "string",
        "description": "Project root. Defaults to CLAUDE_PROJECT_DIR, then the working directory.",
    }
}

WORKLOG_ACTIONS = ["add", "list", "next", "show", "start", "dispatch", "block", "unblock",
                   "done", "defer", "decline", "drop", "note", "priority", "lane", "stats"]
PHASE_ACTIONS = ["status", "show", "begin", "approve", "reground", "release", "clear"]

TOOLS = [
    {
        "name": "teamme_status",
        "description": (
            "Report whether teamme is installed in this project and whether its hooks are actually "
            "firing (not-installed / installed-outdated / installed-not-live / live), one line "
            "per check with the fix "
            "for each failure. Always available; run it first when anything looks off."
        ),
        "inputSchema": {"type": "object", "properties": dict(PROJECT_DIR_PROP), "additionalProperties": False},
    },
    {
        "name": "teamme_install",
        "description": (
            "Scaffold or repair teamme in this project: copy any missing hook scripts, create "
            ".claude/intake/, and merge the hooks block into .claude/settings.json while preserving "
            "everything already there. Idempotent. Run this when teamme_status says not-installed, "
            "and to REPAIR an install when it says installed-outdated - an outdated install must be "
            "repaired this way, never by re-running /teamme:init-team over an existing team. A hook "
            "script that is present but differs from the plugin's copy is reported and left alone; "
            "pass force=true to replace it."
        ),
        "inputSchema": {
            "type": "object",
            "properties": dict(
                PROJECT_DIR_PROP,
                force={
                    "type": "boolean",
                    "description": "Overwrite hook scripts that differ from the plugin's copy. Off by default.",
                },
            ),
            "additionalProperties": False,
        },
    },
    {
        "name": "teamme_worklog",
        "description": (
            "Read or update the project's durable work log (tasks, priorities, statuses, notes). "
            "Requires teamme to be installed: run teamme_install first if it is not."
        ),
        "inputSchema": {
            "type": "object",
            "properties": dict(
                PROJECT_DIR_PROP,
                action={"type": "string", "enum": WORKLOG_ACTIONS},
                id={"type": "string", "description": "Task id, e.g. T3. Required by every action except add, list, next, stats."},
                text={
                    "type": "string",
                    "description": (
                        "Title for add; reason or note text for block, defer, decline, drop, note; "
                        "for dispatch, the agent it went to - optional, and it falls back to the "
                        "task's lane."
                    ),
                },
                priority={"type": "string", "enum": ["P0", "P1", "P2"]},
                lane={"type": "string", "description": "Owning specialist agent."},
                all={"type": "boolean", "description": "For list: include closed tasks."},
            ),
            "required": ["action"],
            "additionalProperties": False,
        },
    },
    {
        "name": "teamme_intake_phase",
        "description": (
            "Read or move the transient intake phase lock (idle / grounding / approved), which is "
            "what denies project edits while a brief is still being written. Requires teamme to be "
            "installed: run teamme_install first if it is not."
        ),
        "inputSchema": {
            "type": "object",
            "properties": dict(
                PROJECT_DIR_PROP,
                action={"type": "string", "enum": PHASE_ACTIONS},
                task={"type": "string", "description": "Work-log task id, required by begin."},
                force={"type": "boolean", "description": "For begin: take over an active lock."},
            ),
            "required": ["action"],
            "additionalProperties": False,
        },
    },
    {
        "name": "teamme_librarian_status",
        "description": (
            "Report what the project's librarian indexes hold: which have data, the last indexed "
            "commit, how many commits have landed since, and the row counts - and for the session "
            "index, where this project's transcripts were found, how many bytes of them are "
            "indexed and how many are not. Always available and read-only - it does not require "
            "teamme's scaffolding, and it reports the absence of a git repository or a transcript "
            "directory rather than failing. INTENDED CALLER: a librarian "
            "agent. Other agents should ask the librarian rather than the index; phase 1 cannot "
            "enforce that, so it is a convention, not a guarantee."
        ),
        "inputSchema": {
            "type": "object",
            "properties": dict(PROJECT_DIR_PROP),
            "additionalProperties": False,
        },
    },
    {
        "name": "teamme_librarian_refresh",
        "description": (
            "Bring a librarian index up to date. Incremental by default: it reads only the commits "
            "since the last indexed hash, so running it often is cheap. Pass full=true to reindex "
            "from scratch, which also rewrites the append-only record. Writes two things in the "
            "project - .claude/librarians/history/commits.jsonl (the record: text, mergeable, the "
            "only part worth committing) and .claude/librarians/index.db (derived and disposable; "
            "gitignore it - a binary file cannot be merged). If the marker is unreachable after a "
            "rebase or force-push it falls back to a full reindex and says so. The `sessions` "
            "librarian works the same way over a different source: the session transcripts the "
            "harness already writes, read incrementally BY BYTE OFFSET, so nothing is captured in "
            "flight and no hook exists for it. Its index lives in .claude/librarians/sessions/ "
            "and is ALWAYS gitignored - it holds conversation text, so commit_record does not "
            "apply to it. INTENDED CALLER: a librarian agent."
        ),
        "inputSchema": {
            "type": "object",
            "properties": dict(
                PROJECT_DIR_PROP,
                librarian={"type": "string", "enum": list(LIBRARIANS),
                           "description": "Which index to refresh. Defaults to history."},
                full={"type": "boolean",
                      "description": "Reindex everything instead of only what is new. Off by default."},
                session={"type": "string",
                         "description": "sessions only: refresh just this session id. Omit to "
                                        "refresh every transcript for this project."},
            ),
            "additionalProperties": False,
        },
    },
    {
        "name": "teamme_librarian_query",
        "description": (
            "Ask the history index a bounded question: recent (the latest commits), "
            "commits_touching (every commit that changed a path or anything under it), "
            "files_in_commit, commits_between (a date range), search_subjects (a literal substring "
            "of the commit subject), commit_detail (ONE commit in full, including the message BODY "
            "and its changed files - the list queries return the subject line only, so this is the "
            "query to reach for when the question is *why* a change was made). Three more read "
            "CO-CHANGE out of the same commit stream, which is how this index answers 'what is "
            "related to this?' without parsing any source: changes_with (the files that most often "
            "changed in the same commits as a path, each row carrying its evidence - how many "
            "shared commits and the most recent one), coupling_between (the commits where two "
            "paths both changed, so a claimed edge can be inspected rather than believed), and "
            "hotspots (the most-changed paths, optionally under one directory). Co-change is "
            "CORRELATION, NOT A CALL GRAPH: report it as 'changes with', never as 'depends on' or "
            "'imports'. Commits touching more than `max_files` files (default 25) are skipped when "
            "counting edges, because one sweep - a reformat, a rename, an initial import - couples "
            "everything it touched; every co-change answer says how many commits it considered and "
            "how many it skipped. Parameterized and row-capped on purpose - there is no "
            "arbitrary SQL, and an answer that would be an unbounded dump is truncated with a "
            "notice instead. INTENDED CALLER: a librarian agent, which reads these rows and "
            "answers in prose; other agents should consult the librarian rather than this tool. "
            "Phase 1 does not enforce that.\n\n"
            "FOUR MORE queries ask the SESSION index - this project's own conversations, indexed "
            "from the transcripts the harness writes - and exist to recover what a compaction "
            "dropped out of context WITHOUT re-reading a multi-megabyte file: sessions (this "
            "project's sessions, newest first, with turn counts and date spans), search_turns (a "
            "literal substring of what was typed or said, answered as MARK POINTS - where it was "
            "discussed, with a short snippet, never the conversation), window (a bounded slice of "
            "one session around one mark point - the only query that returns conversation text, "
            "capped per turn and in total), and compaction (what fell out of context at the most "
            "recent compaction boundary, with the spine of the dropped region and a seq for each "
            "point so any of it can be fetched). The working order is search_turns or compaction "
            "to LOCATE, then window to READ. Tool output - file contents that were read, command "
            "output - is not indexed, and reasoning is not in the transcript at all; every result "
            "says so. The query name alone says which index it belongs to, so `librarian` does "
            "not need to be passed.\n\n"
            "FOUR MORE are CROSS-INDEX and belong to no single librarian: they read the history "
            "index, the session index AND the work log (.claude/intake/worklog.json, read live "
            "and never indexed) together, and answer 'what happened around this file / commit / "
            "task / time'. around_path (commits that touched it, sessions where it was read or "
            "written, tasks whose notes name it), around_commit (the session turns near it in "
            "time and the tasks open when it landed), around_task (its own record, the commits "
            "in its inferred active window, the session region it was worked in), and timeline "
            "(everything from all three stores between two instants). EVERY link between stores "
            "is TIME OVERLAP, not a recorded relationship: report them as 'active while' or "
            "'around', NEVER as 'implements' or 'caused' - the same discipline co-change follows "
            "with 'changes with'. The id-based join was measured on real data and does not exist: "
            "commit messages and work-log notes cite each other only incidentally, so a join "
            "keyed on citation would return almost nothing while looking like 'nothing happened'. "
            "Each row carries its own address - a hash, a session id and seq, a task id - so the "
            "next question goes deeper with commit_detail or window; this is a SPINE, it points "
            "rather than pastes. Each store is gated and capped SEPARATELY and the answer names "
            "any store that contributed nothing and why (disabled, absent, empty, behind), "
            "because an empty index and an empty answer otherwise look identical."
        ),
        "inputSchema": {
            "type": "object",
            "properties": dict(
                PROJECT_DIR_PROP,
                librarian={"type": "string", "enum": list(LIBRARIANS),
                           "description": "Which index to ask. Defaults to history."},
                query={"type": "string",
                       "enum": ["recent", "commits_touching", "files_in_commit",
                                "commits_between", "search_subjects", "commit_detail",
                                "changes_with", "coupling_between", "hotspots",
                                "sessions", "search_turns", "window", "compaction",
                                "around_path", "around_commit", "around_task",
                                "timeline"]},
                path={"type": "string",
                      "description": "For commits_touching, changes_with and coupling_between: a "
                                     "repo-relative file or directory. For hotspots: an optional "
                                     "directory to rank within. An absolute path inside the "
                                     "project is accepted."},
                other_path={"type": "string",
                            "description": "For coupling_between: the second path, compared "
                                           "against `path`."},
                max_files={"type": "integer",
                           "description": "Co-change damping. A commit touching more than this "
                                          "many files is treated as a sweep and left out of the "
                                          "counts. Defaults to 25; every answer reports how many "
                                          "commits were skipped by it."},
                hash={"type": "string", "description": "For files_in_commit and commit_detail: a full or "
                                                       "abbreviated commit hash. An abbreviation matching more "
                                                       "than one commit is reported, never silently resolved."},
                since={"type": "string", "description": "For commits_between and timeline, and "
                                                        "optionally around_path: YYYY-MM-DD or an "
                                                        "ISO timestamp."},
                until={"type": "string", "description": "For commits_between and timeline, and "
                                                        "optionally around_path: YYYY-MM-DD or an "
                                                        "ISO timestamp."},
                text={"type": "string", "description": "For search_subjects and search_turns: a literal "
                                                       "substring; wildcards are not special."},
                session={"type": "string",
                         "description": "For window (required), and to scope search_turns or "
                                        "compaction to one session. A session id from the "
                                        "`sessions` query."},
                parent={"type": "string",
                        "description": "For sessions: list the subagent threads dispatched by "
                                       "this session id, instead of the main threads."},
                include_subagents={"type": "boolean",
                                   "description": "For sessions: list subagent threads alongside "
                                                  "main threads. Off by default - the parent row "
                                                  "reports how many it has."},
                seq={"type": "integer",
                     "description": "For window: the mark point to centre on, as returned by "
                                    "search_turns or compaction."},
                before={"type": "integer",
                        "description": "For window: turns before the mark. Defaults to 4, max 25."},
                after={"type": "integer",
                       "description": "For window: turns after the mark. Defaults to 4, max 25."},
                kind={"type": "string",
                      "enum": ["prompt", "message", "recap", "answer", "tool", "file",
                               "compaction"],
                      "description": "For search_turns, timeline and around_task: restrict to "
                                     "one kind of mark point. For around_path only `file` (a "
                                     "tool that wrote the path) and `tool` (anything else that "
                                     "named it) apply - the other kinds carry prose, not paths."},
                main_thread_only={"type": "boolean",
                                  "description": "For search_turns: exclude subagent turns."},
                task={"type": "string",
                      "description": "For around_task: a work-log task id, such as T12."},
                minutes={"type": "integer",
                         "description": "For around_commit: how far either side of the commit "
                                        "counts as 'around' it. Defaults to 120, max 43200 "
                                        "(30 days)."},
                pad_minutes={"type": "integer",
                             "description": "For around_task: how far the task's inferred active "
                                            "window is widened either side. Defaults to 15, "
                                            "because a status is stamped seconds before or after "
                                            "the commit it refers to; pass 0 for the exact "
                                            "window. Always reported in the answer."},
                limit={"type": "integer", "description": "Row cap. Defaults to 30 for history "
                                                         "queries and 20 for session ones; hard "
                                                         "maximum 200 and 100."},
            ),
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "teamme_librarian_configure",
        "description": (
            "Read or change this project's librarian settings, kept in "
            ".claude/librarians/config.json: which librarians are enabled here, and whether the "
            "append-only record is committed to git. Called with no arguments it only reports - it "
            "writes nothing, not even the config file. A tool rather than a file edit on purpose, "
            "so an agent with no Bash or Write access can still reach the setting. Disabling a "
            "librarian makes teamme_librarian_refresh and teamme_librarian_query REFUSE for it "
            "(teamme_librarian_status keeps working and keeps reporting the setting) - that is a "
            "refusal by those tools, not a harness guarantee: this server registers no hook and "
            "cannot stop anything. A plugin-shipped librarian agent is visible in every project "
            "and cannot be hidden per project, which is why disabling works this way. Setting "
            "commit_record is ENACTED, not just recorded: teamme writes or removes the "
            ".gitignore entry for the record inside its own marked block, and never removes an "
            "ignore line it did not write. The index.db entry is not a choice - a binary index "
            "cannot be merged, so it stays ignored either way. Missing, unreadable or malformed "
            "config reads as the documented defaults (every librarian enabled, commit_record "
            "false), never as an error and never as disabled."
        ),
        "inputSchema": {
            "type": "object",
            "properties": dict(
                PROJECT_DIR_PROP,
                librarian={"type": "string", "enum": list(LIBRARIANS),
                           "description": "Which librarian `enabled` applies to. Defaults to history."},
                enabled={"type": "boolean",
                         "description": "Switch that librarian on or off in this project. Omit to leave it alone."},
                commit_record={"type": "boolean",
                               "description": ("true: the record (.claude/librarians/*/commits.jsonl) is committed "
                                               "with the code, so its .gitignore entry is removed. false (the "
                                               "default): it stays out of git. Omit to leave it alone.")},
            ),
            "additionalProperties": False,
        },
    },
]


STATE_ADVICE = {
    "not-installed": (
        "Nothing has been installed here yet: run /teamme:init-team to set the project up, or "
        "`teamme_install` for the hook scaffolding alone."
    ),
    "installed-outdated": (
        "teamme IS installed here - this is an older install missing part of the current "
        "scaffolding. Repair it with the `teamme_install` tool or /teamme:team-doctor. Do not run "
        "the installer over the team that is already here."
    ),
    "installed-not-live": (
        "Registered but never fired here: approve the hooks with /hooks, or restart Claude Code. "
        "Nothing is being enforced until a session start records the heartbeat."
    ),
}


def tool_status(root: pathlib.Path, args: dict) -> dict:
    mod = preflight(root)
    d = diagnose(root)
    # .get, never an assumption that the state is one of the known literals -
    # diagnose() synthesizes "unknown" when preflight itself could not be loaded.
    advice = STATE_ADVICE.get(d.get("state") or "")
    if mod is not None:
        try:
            lines = list(mod.render(d))
            if advice:
                lines += ["", advice]
            return text_result("\n".join(lines))
        except Exception:
            pass
    return text_result(json.dumps(d, indent=2) + (("\n\n" + advice) if advice else ""))


def tool_worklog(root: pathlib.Path, args: dict) -> dict:
    refusal = gate(root, "worklog.py", "teamme_worklog")
    if refusal:
        return refusal
    action = str(args.get("action") or "").strip()
    if action not in WORKLOG_ACTIONS:
        return text_result(f"unknown worklog action '{action}'. One of: {', '.join(WORKLOG_ACTIONS)}", True)
    text = (args.get("text") or "").strip()
    tid = (args.get("id") or "").strip()
    argv = [action]
    if action == "add":
        if not text:
            return text_result("teamme_worklog add needs `text` (the task title).", True)
        argv.append(text)
        if args.get("priority"):
            argv += ["--priority", str(args["priority"])]
        if args.get("lane"):
            argv += ["--lane", str(args["lane"])]
    elif action == "list":
        if args.get("all"):
            argv.append("--all")
    elif action in ("next", "stats"):
        pass
    else:
        if not tid:
            return text_result(f"teamme_worklog {action} needs `id` (a task id such as T3).", True)
        argv.append(tid)
        if action == "priority":
            extra = (args.get("priority") or text).strip()
        elif action == "lane":
            extra = (args.get("lane") or text).strip()
        else:
            extra = text
        if extra:
            argv.append(extra)
    return run_script(root, "worklog.py", argv)


def tool_intake_phase(root: pathlib.Path, args: dict) -> dict:
    refusal = gate(root, "intake-state.py", "teamme_intake_phase")
    if refusal:
        return refusal
    action = str(args.get("action") or "").strip()
    if action not in PHASE_ACTIONS:
        return text_result(f"unknown phase action '{action}'. One of: {', '.join(PHASE_ACTIONS)}", True)
    argv = [action]
    if action == "begin":
        task = (args.get("task") or "").strip()
        if not task:
            return text_result("teamme_intake_phase begin needs `task` (the work-log task id).", True)
        argv.append(task)
    if args.get("force") and action == "begin":
        argv.append("--force")
    return run_script(root, "intake-state.py", argv)


# --------------------------------------------------------------------------- #
# librarian tools
#
# Deliberately NOT gated on teamme's scaffolding: an index of this repository's
# own history does not depend on intake.md existing. What it does depend on is a
# git repository, and the absence of one is reported as a result, never raised.
# --------------------------------------------------------------------------- #

def _pick_librarian(args: dict):
    name = str((args or {}).get("librarian") or "history").strip().lower()
    if name not in LIBRARIANS:
        return None, text_result(
            f"unknown librarian '{name}'. Available: {', '.join(LIBRARIANS)}", True
        )
    return name, None


def _ago(epoch) -> str:
    try:
        import datetime
        return datetime.datetime.fromtimestamp(int(epoch), datetime.timezone.utc).isoformat(
            timespec="seconds")
    except Exception:
        return "?"


def _render_sessions_status(st: dict) -> list:
    """The session index's own status lines.

    Deliberately says where the transcripts were found and HOW - by the encoded
    directory name, or by reading the working directory recorded inside the
    files. The second means teamme's assumption about the harness's layout has
    drifted, and that is worth seeing rather than silently working.
    """
    out = []
    if st.get("transcript_dir"):
        out.append(f"  transcripts:     {st['transcript_dir']}"
                   + ("  (found by scanning: the expected directory name did not resolve)"
                      if st.get("transcript_dir_source") == "scan" else ""))
        out.append(f"  on disk:         {st.get('transcripts_on_disk', 0)} session file(s), "
                   f"{st.get('bytes_on_disk', 0)} byte(s)")
    else:
        out.append("  transcripts:     NOT FOUND - nothing can be indexed")
        for where in st.get("searched") or []:
            out.append(f"    looked in:     {where}")
    if st.get("has_data"):
        out.append("  data:            yes")
        out.append(f"  rows:            {st.get('sessions')} session(s), {st.get('turns')} turn(s), "
                   f"{st.get('marks')} mark point(s), {st.get('compactions')} compaction(s)")
        if st.get("oldest_epoch"):
            out.append(f"  covers:          {_ago(st['oldest_epoch'])} .. {_ago(st['newest_epoch'])}")
        out.append(f"  indexed:         {st.get('bytes_indexed', 0)} byte(s)")
        behind = st.get("bytes_behind")
        if behind is not None:
            out.append(f"  behind:          {behind} byte(s)"
                       + (" - up to date" if behind == 0
                          else ' - run teamme_librarian_refresh {"librarian": "sessions"}'))
        if st.get("sessions_not_indexed"):
            out.append(f"  not indexed:     {st['sessions_not_indexed']} session file(s)")
        if st.get("last_refresh_at"):
            out.append(f"  last refresh:    {st['last_refresh_at']}")
    elif st.get("transcript_dir"):
        out.append('  data:            no - run teamme_librarian_refresh {"librarian": "sessions"}')
    if st.get("error"):
        out.append(f"  problem:         {st['error']}")
    if st.get("note"):
        out.append(f"  note:            {st['note']}")
    out.append(f"  index:           {st.get('db')} (derived, disposable)")
    out.append("  record:          the transcripts themselves - this librarian keeps no second "
               "copy of them")
    out += _render_sessions_privacy(st)
    return out


def _render_sessions_privacy(st: dict) -> list:
    """The privacy line, checked rather than asserted.

    status() writes nothing, so it cannot repair the rule - but it can refuse to
    repeat a promise the file does not keep. The entry is ensured on every index
    open, so a missing one here means the last attempt failed.
    """
    lib = librarian()
    if lib is None:
        return ["  privacy:         .claude/librarians/sessions/ should be gitignored - teamme "
                "could not load its own librarian code to check"]
    _config = lib[2]
    try:
        state = _config.ignored_entries(st.get("project_dir"))
    except Exception as exc:
        return [f"  privacy:         could not read the project's .gitignore ({exc})"]
    if _config.SESSIONS_IGNORE in (state.get("present") or []):
        return ["  privacy:         .claude/librarians/sessions/ is gitignored by "
                f"{state.get('path')}; commit_record does not apply to it"]
    if not st.get("db_exists"):
        # Nothing to be exposed yet. The rule is written before the index is,
        # so its absence here is the expected state, not a warning.
        return ["  privacy:         .claude/librarians/sessions/ will be added to "
                f"{state.get('path')} before the index is created; commit_record does not "
                "apply to it"]
    return [
        "  privacy:         NOT IGNORED. `" + _config.SESSIONS_IGNORE + "` is not in "
        f"{state.get('path')}"
        + (f" ({state['problem']})" if state.get("problem") else ""),
        "                   This index holds conversation text and git can see it. A refresh "
        "puts the rule back; if it cannot, it says so.",
    ]


def tool_librarian_status(root: pathlib.Path, args: dict) -> dict:
    lib = librarian()
    if lib is None:
        return librarian_missing()
    _store, _history, _config, _sessions, _cross = lib
    cfg = _config.load(root, LIBRARIANS)
    lines = [f"librarian indexes in {root}", ""]
    for name in LIBRARIANS:
        try:
            st = (_history.status(root) if name == "history"
                  else _sessions.status(root) if name == "sessions" else {})
        except Exception as exc:  # a status call must never be the thing that breaks
            lines += [f"{name}: could not be read ({exc})", ""]
            continue
        lines.append(f"{name}:")
        lines.append(
            "  enabled:         "
            + ("yes" if cfg["librarians"].get(name, {}).get("enabled", True) else
               'NO - refresh and query refuse for it. Re-enable with teamme_librarian_configure '
               '{"librarian": "' + name + '", "enabled": true}')
        )
        if name == "sessions":
            lines += _render_sessions_status(st)
            lines.append("")
            continue
        if st.get("has_data"):
            lines.append("  data:            yes")
        elif st.get("is_git_repo") is False:
            lines.append("  data:            no - this is not a git repository, so there is "
                         "nothing for the history index to read")
            if st.get("error"):
                lines.append(f"  git says:        {st['error']}")
        else:
            lines.append("  data:            no - run teamme_librarian_refresh")
            if st.get("error"):
                lines.append(f"  problem:         {st['error']}")
        if st.get("commits") is not None:
            lines.append(
                f"  rows:            {st.get('commits')} commit(s), "
                f"{st.get('files_changed')} file change(s), "
                f"{st.get('commit_parents')} parent edge(s)"
            )
        if st.get("oldest_epoch"):
            lines.append(f"  covers:          {_ago(st['oldest_epoch'])} .. {_ago(st['newest_epoch'])}")
        if st.get("last_indexed_hash"):
            lines.append(f"  last indexed:    {st['last_indexed_hash'][:12]}"
                         + (f" at {st['last_refresh_at']}" if st.get("last_refresh_at") else ""))
        # The marker the copied librarian-gate hook reads. It is written from the
        # same statement that sets the database's own last-indexed hash, so the
        # two can only disagree if something outside the indexer touched one of
        # them - a hand edit, a restore from git, a half-finished write. That is
        # the single failure this design has, so it is stated here rather than
        # left to be discovered as a push reminder counting from the wrong commit.
        if st.get("has_data") or st.get("marker_published"):
            if st.get("marker_matches_index") is False:
                lines.append(
                    f"  MARKER DIVERGED: {st.get('marker')} says {str(st.get('marker_head'))[:12]}, "
                    f"the index says {str(st.get('last_indexed_hash'))[:12]}. The push reminder "
                    f"counts from the marker, so it is counting from the wrong commit. Run "
                    f"teamme_librarian_refresh - it rewrites both together."
                )
            elif st.get("marker_published"):
                lines.append(f"  indexed_head:    published, agrees with the index "
                             f"(read by the librarian-gate hook; never committed)")
            else:
                lines.append("  indexed_head:    not published - the push reminder stays silent "
                             "until the next refresh writes it")
        if st.get("head"):
            lines.append(f"  HEAD:            {st['head'][:12]}")
        behind = st.get("commits_behind_head")
        if behind is not None:
            lines.append(
                f"  behind HEAD:     {behind} commit(s)"
                + (" - up to date" if behind == 0 else " - run teamme_librarian_refresh")
            )
        lines.append(
            f"  record:          {st.get('jsonl')} "
            f"({st.get('jsonl_records', 0)} line(s)"
            + (f", {st['jsonl_unparsable_lines']} unparsable" if st.get("jsonl_unparsable_lines") else "")
            + ")"
        )
        lines.append(f"  index:           {st.get('db')} (derived, disposable, never commit it)")
        if st.get("note"):
            lines.append(f"  note:            {st['note']}")
        lines.append("")
    lines.append("configuration:")
    lines.append(f"  file:            {cfg['path']}"
                 + ("" if cfg.get("exists") else " (absent - the documented defaults are in force)"))
    lines.append(f"  commit_record:   {str(bool(cfg.get('commit_record'))).lower()}"
                 + ("  - the record is committed with the code"
                    if cfg.get("commit_record") else
                    "  - the record is kept out of git"))
    for problem in cfg.get("problems") or []:
        lines.append(f"  config problem:  {problem}")
    lines.append("  change it with:  teamme_librarian_configure")
    lines.append("")
    lines.append(
        "The .jsonl is the record and is the only part worth committing; the .db is rebuilt "
        "from it on demand and must stay out of git - a binary file cannot be merged."
    )
    return text_result("\n".join(lines))


# The refresh result carries the unprotected-index warning in `notes` too, so a
# caller that is not this server still gets the whole story. Here it would be
# printed twice, so the note is dropped in favour of the fuller block below. If
# this prefix ever drifts, the failure is a duplicated warning - never a lost
# one.
UNPROTECTED_NOTE = "THE INDEX COULD NOT BE PROTECTED"


def _notes(r: dict) -> list:
    return [n for n in (r.get("notes") or []) if not str(n).startswith(UNPROTECTED_NOTE)]


def _render_ignore_state(r: dict, reassurance) -> list:
    """What happened to the .gitignore, printed at the bottom of a refresh.

    Two rules. A failure is stated in full, because a refresh that wrote an
    index git can see is exactly the moment a user needs to know. And the
    reassuring line is printed ONLY when it is true - a doc that says "always
    gitignored" over an index that is not is how T40 got past everyone.
    """
    ig = r.get("gitignore") or {}
    if r.get("index_unprotected") or (ig and not ig.get("ok")):
        return [
            "  UNPROTECTED:  teamme could not put its ignore rule in place in "
            f"{ig.get('path')}:",
            f"                {ig.get('problem')}",
            "                The index was still written and is usable, but git can see it. "
            "Fix that file and refresh again.",
        ]
    out = []
    if ig.get("changed"):
        out.append(f"  protected:    wrote {', '.join(ig.get('wrote') or [])} to "
                   f"{ig.get('path')}")
    if reassurance:
        out.append(f"  {reassurance}")
    return out


def tool_librarian_refresh(root: pathlib.Path, args: dict) -> dict:
    lib = librarian()
    if lib is None:
        return librarian_missing()
    _store, _history, _config, _sessions, _cross = lib
    name, refusal = _pick_librarian(args)
    if refusal:
        return refusal
    off = librarian_disabled(root, name, _config)
    if off:
        return off
    full = bool(args.get("full"))
    try:
        if name == "sessions":
            r = _sessions.index(root, full=full, session=args.get("session"))
        else:
            r = _history.index(root, full=full)
    except Exception as exc:  # belt and braces: index() already returns its errors
        return text_result(f"teamme_librarian_refresh failed: {exc}", True)
    if not r.get("ok"):
        return text_result(
            f"teamme_librarian_refresh ({name}) did nothing: {r.get('error')}\n"
            f"project_dir: {root}",
            True,
        )
    if name == "sessions":
        lines = [
            f"teamme_librarian_refresh (sessions): {r['mode']} index in "
            f"{r.get('elapsed_seconds')}s",
            f"  transcripts:  {r.get('transcript_dir')}"
            + ("  (found by scanning, not by name)"
               if r.get("transcript_dir_source") == "scan" else ""),
            f"  read:         {r.get('bytes_read', 0)} new byte(s) from "
            f"{r.get('sessions_touched', 0)} of {r.get('sessions_seen', 0)} session file(s)",
            f"  added:        {r.get('turns_added', 0)} turn(s), "
            f"{r.get('marks_added', 0)} mark point(s)",
            f"  now holds:    {r.get('sessions')} session(s), {r.get('turns')} turn(s), "
            f"{r.get('marks')} mark point(s), {r.get('compactions')} compaction(s)",
        ]
        for note in _notes(r):
            lines.append(f"  note:         {note}")
        lines += _render_ignore_state(r, "the index is machine-local and always gitignored - it "
                                         "holds conversation text, so commit_record does not "
                                         "apply to it")
        return text_result("\n".join(lines), bool(r.get("index_unprotected")))
    lines = [
        f"teamme_librarian_refresh ({name}): {r['mode']} index in {r.get('elapsed_seconds')}s",
        f"  added:        {r.get('commits_added', 0)} commit(s), {r.get('files_added', 0)} file change(s)",
        f"  now holds:    {r.get('commits')} commit(s), {r.get('files_changed')} file change(s)",
    ]
    if r.get("head"):
        lines.append(f"  indexed to:   {r['head'][:12]}")
    if r.get("fallback"):
        lines.append(f"  FELL BACK:    {r['fallback']}")
    for note in _notes(r):
        lines.append(f"  note:         {note}")
    if r.get("malformed_records"):
        lines.append(f"  skipped:      {r['malformed_records']} unparsable log record(s)")
    lines += _render_ignore_state(r, None)
    return text_result("\n".join(lines), bool(r.get("index_unprotected")))


def _render_rows(q: dict) -> str:
    rows = q.get("rows") or []
    if not rows:
        return "no matching rows"
    out = []
    for row in rows:
        if "subject" in row:
            head = f"{row.get('short_hash') or ''} {row.get('date') or ''} {row.get('author') or ''}"
            line = f"{head.strip()}  {row.get('subject') or ''}"
            if row.get("path"):
                line += f"  [{row['path']} +{row.get('additions')}/-{row.get('deletions')}]"
        else:
            line = (f"{row.get('path')}  +{row.get('additions')}/-{row.get('deletions')}"
                    f"  ({(row.get('hash') or '')[:8]})")
        out.append("  " + line)
    return "\n".join(out)


def _render_detail(q: dict, store) -> str:
    """commit_detail is one commit, not a row list, and the body is the point of
    it - so it gets its own rendering rather than being flattened into a line."""
    row = (q.get("rows") or [{}])[0]
    out = [
        f"  commit:   {row.get('hash') or ''}",
        f"  author:   {row.get('author') or ''} <{row.get('author_email') or ''}>",
        f"  date:     {row.get('date') or ''}",
        f"  parents:  {row.get('parents') or '(none - a root commit)'}",
        f"  subject:  {row.get('subject') or ''}",
    ]
    body = (row.get("body") or "").rstrip()
    if body:
        out.append("  body:")
        out += ["    " + line for line in body.splitlines()]
        if row.get("body_truncated"):
            out.append(f"    ... BODY TRUNCATED at {store.MAX_BODY_CHARS} of "
                       f"{row.get('body_chars')} characters; this tool never returns an unbounded "
                       f"dump. The whole text is in the record, "
                       f".claude/librarians/history/commits.jsonl.")
    else:
        out.append("  body:     (none - a subject-only commit)")
    files = q.get("files") or []
    out.append(f"  files:    {len(files)}" + (" (truncated)" if q.get("files_truncated") else ""))
    for f in files:
        out.append(f"    {f.get('path')}  +{f.get('additions')}/-{f.get('deletions')}")
    if q.get("files_truncated"):
        out.append(f"    ... TRUNCATED at {q.get('limit')} file(s). Raise `limit` for the rest.")
    if not files:
        out.append("    (no file rows - a merge commit records none, by design)")
    return "\n".join(out)


def _short_subject(text, width: int = 60) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= width else text[: width - 1] + "…"


def _render_damping(d: dict) -> list:
    """How the co-change cap changed the numbers above it. Printed every time,
    including when it skipped nothing: a ranking whose largest input is
    invisible can only be believed, not checked."""
    if not d:
        return []
    out = [
        f"  evidence base:  {d.get('commits_considered')} commit(s) considered of "
        f"{d.get('commits_indexed')} indexed; {d.get('commits_skipped_too_broad')} skipped as too "
        f"broad (more than {d.get('max_files')} files in one commit - a sweep couples everything "
        f"it touched)",
    ]
    if d.get("commits_without_file_rows"):
        out.append(f"                  {d['commits_without_file_rows']} indexed commit(s) carry no "
                   f"file rows at all (merges record none) and contribute no edges")
    if d.get("note"):
        out.append(f"                  {d['note']}")
    out.append(f"                  change the cap with `max_files` (now {d.get('max_files')})")
    return out


def _render_caveats(q: dict) -> list:
    items = q.get("caveats") or []
    if not items:
        return []
    return ["  reading this:"] + [f"    - {item}" for item in items]


def _render_cochange(which: str, q: dict, store) -> str:
    """The three co-change answers. Each row prints its evidence on its own line,
    so a position in the ranking can be checked rather than taken on faith."""
    out = []
    rows = q.get("rows") or []

    if which == "changes_with":
        out.append(f"  files that CHANGE WITH {q.get('path')} - correlation, not a call graph")
        out.append(f"  '{q.get('path')}' changed in {q.get('anchor_commits')} considered commit(s)"
                   + (f" ({q.get('anchor_commits_all')} in the index before damping)"
                      if q.get("anchor_commits_all") != q.get("anchor_commits") else "")
                   + f"; {len(rows)} partner(s) shown, most shared commits first")
        for r in rows:
            mark = "  WEAK - a single shared commit" if r.get("weak") else ""
            out.append(f"    {r.get('shared_commits')}x  {r.get('path')}{mark}")
            out.append(f"         changed in {r.get('partner_commits')} considered commit(s) of its "
                       f"own; overlap {r.get('jaccard')}; last together "
                       f"{r.get('last_short_hash')} {(r.get('last_date') or '')[:10]} "
                       f"\"{_short_subject(r.get('last_subject'))}\"")

    elif which == "coupling_between":
        out.append(f"  commits where BOTH {q.get('path')} and {q.get('other_path')} changed")
        out.append(f"  {q.get('shared_commits')} shared commit(s): {q.get('shared_counted')} counted "
                   f"as evidence, {q.get('shared_too_broad')} too broad (more than "
                   f"{q.get('max_files')} files) and ignored by changes_with")
        out.append(f"  on its own: {q.get('path')} in {q.get('commits_touching_path')} commit(s), "
                   f"{q.get('other_path')} in {q.get('commits_touching_other_path')}")
        out.append("  every shared commit is listed below, sweeps included and marked - this query "
                   "exists to be inspected, so it damps nothing")
        for r in rows:
            mark = "  [TOO BROAD - not counted as evidence]" if r.get("too_broad") else ""
            out.append(f"    {r.get('short_hash')} {(r.get('date') or '')[:10]} "
                       f"{r.get('author') or ''}  {_short_subject(r.get('subject'), 70)}")
            out.append(f"         {r.get('files_in_commit')} file(s) in that commit{mark}")
        if q.get("note"):
            out.append(f"  note: {q['note']}")

    else:  # hotspots
        scope = q.get("path")
        out.append("  most-changed paths" + (f" under {scope}" if scope else " in the repository")
                   + " - how often, not how important")
        for r in rows:
            mark = "  WEAK - changed once" if r.get("weak") else ""
            out.append(f"    {r.get('commits')}x  {r.get('path')}{mark}")
            out.append(f"         {r.get('first_date')} .. {(r.get('last_date') or '')[:10]}; last "
                       f"{r.get('last_short_hash')} \"{_short_subject(r.get('last_subject'))}\"")

    if not rows:
        out.append(f"  no rows. {q.get('empty_reason') or ''}".rstrip())
    if q.get("truncated"):
        out.append(f"  ... TRUNCATED at {q.get('limit')} rows. Narrow the question or raise `limit` "
                   f"(max {store.MAX_LIMIT}); this tool never returns an unbounded dump.")
    out += _render_damping(q.get("damping"))
    out += _render_caveats(q)
    return "\n".join(out)


def _repo_signal_notes(history, root) -> list:
    """What git knows that the index cannot: a shallow clone is missing the very
    history these rankings are computed from, and it should say so rather than
    rank confidently over a truncated stream. Never raises and never blocks the
    answer - a probe that fails simply adds nothing."""
    try:
        p = history.probe(root)
    except Exception:
        return []
    if not isinstance(p, dict):
        return []
    if p.get("shallow"):
        return ["    - this is a SHALLOW clone: git is missing older commits, so the co-change "
                "counts above are computed from a truncated history. `git fetch --unshallow` and "
                "refresh before treating them as a ranking."]
    return []


def _route_query(args: dict, which: str, _store, _sessions, _cross):
    """(librarian, refusal) for a query name.

    Query names are unique across librarians, so `librarian` does not have to be
    passed - but if it IS passed and disagrees with the query, that is refused
    rather than silently overridden: a caller who asked for the wrong index
    should be told, not quietly given the right one.

    The cross-index queries answer to "cross", which is NOT a librarian: they
    read both indexes and the work log, gate each one separately, and report
    which of them contributed nothing. Passing `librarian` with one of them is
    refused for the same reason as any other disagreement - it names a single
    index for a question that deliberately spans three stores.
    """
    if which in _sessions.QUERY_NAMES:
        owner = "sessions"
    elif which in _store.QUERY_NAMES:
        owner = "history"
    elif which in _cross.QUERY_NAMES:
        owner = "cross"
    else:
        return None, text_result(
            f"unknown query '{which}'.\n"
            f"  history:  {', '.join(_store.QUERY_NAMES)}\n"
            f"  sessions: {', '.join(_sessions.QUERY_NAMES)}\n"
            f"  cross:    {', '.join(_cross.QUERY_NAMES)}", True)
    asked = args.get("librarian")
    if asked is not None:
        asked = str(asked).strip().lower()
        if asked not in LIBRARIANS:
            return None, text_result(
                f"unknown librarian '{asked}'. Available: {', '.join(LIBRARIANS)}", True)
        if owner == "cross":
            return None, text_result(
                f"'{which}' reads the history index, the session index AND the work log "
                f"together, so it belongs to no single librarian and `librarian` does not apply. "
                f"Nothing was read. Drop the argument - each store is gated on its own and the "
                f"answer says which of them had nothing.", True)
        if asked != owner:
            return None, text_result(
                f"'{which}' is a {owner} query, but librarian was given as '{asked}'. Nothing was "
                f"read. Pass librarian '{owner}', or drop the argument - the query name already "
                f"says which index it belongs to.", True)
    return owner, None


def _render_session_rows(which: str, q: dict, _sessions) -> str:
    """The four session answers. Every one of them returns POSITIONS by default;
    `window` is the only one that returns conversation text, and it is capped
    twice - per turn and in total."""
    out = []
    rows = q.get("rows") or []

    if which == "sessions":
        out.append("  this project's sessions, newest first")
        for r in rows:
            out.append(f"    {r.get('session_id')}  {r.get('title') or '(untitled)'}"
                       + (f"  [subagent: {r.get('agent') or '?'}]" if r.get("parent_session") else ""))
            out.append(f"         {(r.get('first_ts') or '?')[:19]} .. {(r.get('last_ts') or '?')[:19]}"
                       + (f"  ({r['span_days']} day(s))" if r.get("span_days") else "")
                       + f"  on {r.get('git_branch') or '?'}")
            out.append(f"         {r.get('turns')} turn(s), {r.get('prompts')} prompt(s), "
                       f"{r.get('marks')} mark point(s), {r.get('compactions')} compaction(s)")
            if r.get("subagent_threads"):
                out.append(f"         plus {r['subagent_threads']} subagent thread(s), "
                           f"{r['subagent_turns']} turn(s) - indexed and searchable, listed with "
                           f'{{"query": "sessions", "parent": "{r.get("session_id")}"}}')
            if not r.get("fully_indexed"):
                out.append(f"         {r.get('bytes_behind')} byte(s) not yet indexed"
                           + (" - the session is still being written"
                              if r.get("still_being_written") else " - refresh to catch up"))

    elif which == "search_turns":
        out.append(f"  mark points whose text contains '{q.get('text')}'"
                   + (f" ({q.get('turns_matching')} turn(s) match in all)"
                      if q.get("turns_matching") is not None else ""))
        for r in rows:
            out.append(f"    [{r.get('kind')}] {(r.get('ts') or '')[:19]}  "
                       f"{r.get('session_id')} seq {r.get('seq')}"
                       + ("  (subagent)" if r.get("sidechain") else ""))
            if r.get("agent"):
                out.append(f"         (subagent thread: {r['agent']})")
            out.append(f"         {r.get('label') or ''}")
            out.append("         " + ("..." if r.get("before") else "")
                       + str(r.get("snippet") or "") + ("..." if r.get("after") else ""))
            out.append(f"         fetch: {{\"query\": \"window\", \"session\": "
                       f"\"{r.get('session_id')}\", \"seq\": {r.get('seq')}}}")

    elif which == "window":
        out.append(f"  {q.get('session')} {q.get('session_title') or ''} - turns "
                   f"{q.get('range', [0, 0])[0]}..{q.get('range', [0, 0])[1]} "
                   f"around seq {q.get('seq')} ({q.get('chars')} characters)")
        for r in rows:
            head = f"    {'>>' if r.get('is_anchor') else '  '} seq {r.get('seq')} {r.get('role')} " \
                   f"{(r.get('ts') or '')[:19]}" + ("  (subagent)" if r.get("sidechain") else "")
            out.append(head)
            for m in r.get("marks") or []:
                out.append(f"         [{m.get('kind')}] {m.get('label') or ''}")
            body = (r.get("text") or "").splitlines()
            for line in body:
                out.append("         " + line)
            if r.get("turn_truncated"):
                out.append(f"         ... TURN TRUNCATED at "
                           f"{q.get('caps', {}).get('per_turn_chars')} of {r.get('text_chars')} "
                           f"characters")
        if q.get("stopped_at_seq") is not None:
            out.append(f"    ... WINDOW TRUNCATED at seq {q['stopped_at_seq']}: the "
                       f"{q.get('caps', {}).get('total_chars')}-character cap was reached. Ask for "
                       f"a narrower window, or move the anchor.")

    else:  # compaction
        if not q.get("found"):
            out.append("  no compaction found")
        else:
            out.append(f"  most recent compaction: {q.get('session')} seq {q.get('seq')} at "
                       f"{(q.get('ts') or '')[:19]} (trigger: {q.get('trigger')})")
            out.append(f"  context: {q.get('tokens_before')} tokens before -> "
                       f"{q.get('tokens_after')} after; {q.get('tokens_dropped')} dropped")
            out.append(f"  what fell out of context: {q.get('turns_before')} turn(s) from "
                       f"{(q.get('first_ts') or '?')[:19]} to {(q.get('last_ts') or '?')[:19]}; "
                       f"{q.get('turns_after')} turn(s) came after it")
            kinds = q.get("marks_before_by_kind") or {}
            if kinds:
                out.append("  mark points in that region: "
                           + ", ".join(f"{v} {k}" for k, v in sorted(kinds.items())))
            out.append(f"  the spine of it ({len(rows)} prompt/recap mark(s), oldest first):")
            for r in rows:
                out.append(f"    [{r.get('kind')}] seq {r.get('seq')}  "
                           f"{(r.get('ts') or '')[:19]}  {r.get('label') or ''}")
            if q.get("truncated"):
                out.append(f"    ... TRUNCATED at {q.get('limit')} mark(s). Raise `limit` "
                           f"(max {_sessions.MAX_LOST_MARKS}), or search within the region.")
            out.append('  fetch any of it: {"query": "window", "session": "'
                       + str(q.get("session")) + '", "seq": <seq>}')

    if not rows and which != "window":
        out.append(f"  no rows. {q.get('empty_reason') or ''}".rstrip())
    if q.get("truncated") and which in ("sessions", "search_turns"):
        out.append(f"  ... TRUNCATED at {q.get('limit')} rows. Narrow the question or raise "
                   f"`limit` (max {_sessions.MAX_LIMIT}); this tool never returns an unbounded "
                   f"dump.")
    for caveat in q.get("caveats") or []:
        out.append(f"  - {caveat}")
    return "\n".join(out)


def _session_query(root: pathlib.Path, args: dict, which: str, _sessions) -> dict:
    """Ask the session index. Every failure is a result, never an exception."""
    if not _sessions.db_path(root).exists():
        return text_result(
            f"the session index does not exist yet in {root}. Run teamme_librarian_refresh "
            f'{{"librarian": "sessions"}} first - it reads the transcripts the harness already '
            f"wrote, incrementally, and writes {_sessions.db_path(root)}.",
            True,
        )
    conn = None
    try:
        conn, reset_note = _sessions.connect_or_reset(root)
        if reset_note:
            return text_result(
                f"{reset_note}. Nothing was queried. Run teamme_librarian_refresh "
                f'{{"librarian": "sessions"}} and ask again.', True)
        q = _sessions.query(conn, which, args, root)
    except Exception as exc:
        return text_result(f"teamme_librarian_query failed: {exc}", True)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    if not q.get("ok"):
        return text_result(f"teamme_librarian_query: {q.get('error')}", True)
    return text_result(f"sessions / {which}: {q.get('count')} row(s)\n"
                       + _render_session_rows(which, q, _sessions))


# --------------------------------------------------------------------------- #
# the cross-index spine
#
# Three stores, one answer, and the honesty that makes it usable: every row says
# which store it came from and carries its own address, and the payload says
# which store contributed NOTHING and why. A thin answer that looks complete is
# the failure this rendering exists to prevent.
# --------------------------------------------------------------------------- #

def _cross_row_lines(r: dict) -> list:
    """One interleaved row: when, which store, what, and how to go deeper."""
    # ts_utc, not the store's own string: git prints the committer's offset and
    # the transcripts print UTC, so a correctly ordered list of raw strings
    # still reads as out of order.
    when = (r.get("ts_utc") or r.get("ts") or "")[:19] or "(no timestamp)"
    store_name = r.get("store")
    out = []
    if store_name == "history":
        out.append(f"    {when}  [history] {r.get('short_hash') or ''}  {r.get('subject') or ''}")
        detail = r.get("relation") or ""
        if r.get("path"):
            detail += f"  ({r['path']} +{r.get('additions')}/-{r.get('deletions')})"
        out.append(f"         {detail}")
        out.append(f"         fetch: {{\"query\": \"commit_detail\", \"hash\": "
                   f"\"{r.get('short_hash') or r.get('hash')}\"}}")
    elif store_name == "sessions":
        who = f"  (subagent: {r.get('agent') or '?'})" if r.get("subagent") else ""
        out.append(f"    {when}  [sessions] {r.get('kind')}  {r.get('session')} "
                   f"seq {r.get('seq')}{who}")
        near = r.get("seconds_from_anchor")
        rel = r.get("relation") or ""
        if near is not None:
            rel = f"{rel} by {abs(int(near))}s" if near else rel
        out.append(f"         {rel}")
        body = r.get("label") or r.get("preview") or ""
        if body:
            out.append(f"         {body}")
        out.append(f"         fetch: {{\"query\": \"window\", \"session\": "
                   f"\"{r.get('session')}\", \"seq\": {r.get('seq')}}}")
    else:
        out.append(f"    {when}  [worklog] {r.get('id')} {r.get('priority') or ''} "
                   f"[{r.get('status')}]  {r.get('title') or ''}")
        bits = [r.get("relation") or ""]
        if r.get("lane"):
            bits.append(f"lane {r['lane']}")
        if r.get("matched_in"):
            bits.append("matched in " + ", ".join(r["matched_in"]))
        if r.get("inference"):
            bits.append(r["inference"])
        out.append("         " + "; ".join(b for b in bits if b))
        out.append(f"         timestamp shown is `{r.get('epoch_from')}` "
                   f"(created {(r.get('created') or '?')[:19]}, status_changed "
                   f"{(r.get('status_changed') or '?')[:19]})")
        out.append(f"         fetch: {r.get('fetch_with')}")
    return out


def _render_cross(which: str, q: dict) -> str:
    out = []

    if which == "around_path":
        out.append(f"  around {q.get('path')}"
                   + (f"  (given as {q.get('path_as_given')})"
                      if q.get("path_as_given") != q.get("path") else ""))
        if q.get("since") or q.get("until"):
            out.append(f"  restricted to {q.get('since') or 'the beginning'} .. "
                       f"{q.get('until') or 'now'}")
    elif which == "around_commit":
        a = q.get("anchor") or {}
        out.append(f"  around commit {a.get('short_hash')}  {a.get('subject')}")
        out.append(f"  by {a.get('author')} at {a.get('date')}")
        files = q.get("files_changed") or []
        out.append(f"  it changed {'at least ' if q.get('files_truncated') else ''}{len(files)} "
                   f"file(s)" + (":" if files else " - a merge records none, by design"))
        for f in files:
            out.append(f"      {f.get('path')}  +{f.get('additions')}/-{f.get('deletions')}")
        if q.get("files_truncated"):
            out.append(f"      ... TRUNCATED at the {q.get('limit')}-row cap - raise `limit` for "
                       f"the rest, or ask files_in_commit")
        out.append(f"  window: +/- {q.get('window_minutes')} minute(s) "
                   f"({(q.get('window') or ['?', '?'])[0]} .. {(q.get('window') or ['?', '?'])[1]})")
    elif which == "around_task":
        out.append(f"  around {q.get('task')} [{q.get('status')}]  {q.get('title')}")
        w = q.get("window")
        out.append(f"  INFERRED ACTIVE WINDOW: {w[0]} .. {w[1]}  ({q.get('window_hours')} hour(s))"
                   if w else "  no window could be inferred")
        out.append(f"      basis: {q.get('window_basis')}")
        if q.get("notes_total"):
            out.append(f"  the task carries {q['notes_total']} note(s)"
                       + (" (only the first few are previewed below)"
                          if q.get("notes_truncated") else "")
                       + f"; read them in full with: python3 .claude/hooks/worklog.py show "
                         f"{q.get('task')}")
        regions = q.get("session_regions") or []
        if regions:
            out.append(f"  session regions inside that window ({len(regions)}"
                       + (", truncated" if q.get("session_regions_truncated") else "") + "):")
            for r in regions:
                out.append(f"      {r.get('session')}"
                           + (f"  [subagent: {r.get('agent') or '?'}]" if r.get("subagent") else "")
                           + f"  {r.get('turns_in_window')} turn(s) "
                             f"{(r.get('first_ts') or '')[:19]} .. {(r.get('last_ts') or '')[:19]}"
                             f"  seq {r.get('seq_range')}")
    else:  # timeline
        out.append(f"  {q.get('since')} .. {q.get('until')}  ({q.get('range_hours')} hour(s))"
                   + (f"  - {q['defaulted']}" if q.get("defaulted") else ""))
        out.append(f"  session mark kinds included: {', '.join(q.get('mark_kinds') or [])}")

    stores = q.get("stores") or {}
    out.append("  all times below are UTC, normalized from each store's own format")
    out.append("  what each store contributed (row cap is PER STORE, now "
               f"{q.get('limit')}):")
    for name in ("history", "sessions", "worklog"):
        st = stores.get(name)
        if not st:
            continue
        line = f"      {name:<9} {st.get('state'):<9} {st.get('rows', 0)} row(s)"
        if st.get("truncated"):
            line += "  TRUNCATED at the cap"
        out.append(line)
        if st.get("detail"):
            out.append(f"                  {st['detail']}")

    rows = q.get("rows") or []
    if rows:
        out.append(f"  {len(rows)} row(s), interleaved by time, newest first:")
        for r in rows:
            out += _cross_row_lines(r)
    else:
        out.append("  no rows from any store. Read the per-store lines above before concluding "
                   "nothing happened - an empty index and an empty answer look identical here, "
                   "which is why every store reports its own state.")

    notes = None
    if which == "around_task":
        # The anchor row is somewhere in the time-ordered list, not necessarily
        # first - a task whose last status change predates its own commits sorts
        # below them.
        notes = next((r.get("notes_preview") for r in rows if r.get("notes_preview")), None)
    if notes:
        out.append("  the task's own notes (clipped):")
        for i, note in enumerate(notes, 1):
            out.append(f"      {i}. {note}")

    out.append("  reading this:")
    for caveat in q.get("caveats") or []:
        out.append(f"    - {caveat}")
    return "\n".join(out)


def _cross_query(root: pathlib.Path, args: dict, which: str, _store, _cross, _config) -> dict:
    """Ask all three stores. Every failure is a result, never an exception."""
    enabled = {}
    for name in LIBRARIANS:
        try:
            enabled[name] = bool(_config.enabled(root, name, LIBRARIANS))
        except Exception:
            enabled[name] = True  # a gate that cannot be read is not a gate
    try:
        q = _cross.query(which, args, root, enabled)
    except Exception as exc:  # cross.query catches its own; this is belt and braces
        return text_result(f"teamme_librarian_query failed: {exc}", True)
    if not q.get("ok"):
        return text_result(f"teamme_librarian_query: {q.get('error')}", True)
    empty = q.get("stores_with_nothing") or []
    head = (f"cross / {which}: {q.get('count')} row(s) from "
            f"{3 - len(empty)} of 3 store(s)"
            + (f"; nothing from: {', '.join(empty)}" if empty else ""))
    return text_result(head + "\n" + _render_cross(which, q))


def tool_librarian_query(root: pathlib.Path, args: dict) -> dict:
    lib = librarian()
    if lib is None:
        return librarian_missing()
    _store, _history, _config, _sessions, _cross = lib
    which = str(args.get("query") or "").strip()
    name, refusal = _route_query(args, which, _store, _sessions, _cross)
    if refusal:
        return refusal
    if name == "cross":
        # No librarian_disabled() call here on purpose: a cross query gates each
        # store separately inside, so switching one librarian off narrows the
        # answer and SAYS which store went quiet, rather than refusing the whole
        # question. Nothing disabled is read either way.
        return _cross_query(root, args, which, _store, _cross, _config)
    off = librarian_disabled(root, name, _config)
    if off:
        return off
    if name == "sessions":
        return _session_query(root, args, which, _sessions)
    if not _store.db_path(root).exists():
        return text_result(
            f"the {name} index does not exist yet in {root}. Run teamme_librarian_refresh first "
            f"- it builds the index from git, or rebuilds it from "
            f"{_store.commits_jsonl(root)} if that has been committed.",
            True,
        )
    conn = None
    try:
        conn, reset_note = _store.connect_or_reset(root)
        if reset_note:
            return text_result(
                f"{reset_note}. Nothing was queried. Run teamme_librarian_refresh and ask again.",
                True,
            )
        if not _store.counts(conn).get("commits"):
            return text_result(
                f"the {name} index is empty. Run teamme_librarian_refresh first.", True
            )
        q = _store.query(conn, which, args, root)
    except Exception as exc:
        return text_result(f"teamme_librarian_query failed: {exc}", True)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    if not q.get("ok"):
        return text_result(f"teamme_librarian_query: {q.get('error')}", True)
    if which == "commit_detail":
        return text_result(f"{name} / commit_detail\n" + _render_detail(q, _store))
    if which in ("changes_with", "coupling_between", "hotspots"):
        body = [f"{name} / {which}: {q['count']} row(s)", _render_cochange(which, q, _store)]
        body += _repo_signal_notes(_history, root)
        return text_result("\n".join(body))
    lines = [f"{name} / {which}: {q['count']} row(s)", _render_rows(q)]
    if q.get("truncated"):
        lines.append(
            f"  ... TRUNCATED at {q['limit']} rows. Narrow the question or raise `limit` "
            f"(max {_store.MAX_LIMIT}); this tool never returns an unbounded dump."
        )
    return text_result("\n".join(lines))


def tool_librarian_configure(root: pathlib.Path, args: dict) -> dict:
    """Report the librarian settings, and change them if asked.

    Never a crash and never a protocol error: a write that fails, a .gitignore
    somebody hand-edited, a librarian name this release does not ship - each
    comes back as a result that says what did NOT happen.
    """
    lib = librarian()
    if lib is None:
        return librarian_missing()
    _store, _history, _config, _sessions, _cross = lib
    enable = args.get("enabled")
    commit = args.get("commit_record")
    # A boolean sent as "true" or 1 is dropped rather than guessed at - and said
    # so, because a setting silently not applied is the failure worth avoiding.
    rejected = [
        f"`{key}` was {type(args[key]).__name__} {args[key]!r}, not true/false; it was ignored "
        f"and nothing about it changed"
        for key in ("enabled", "commit_record")
        if key in args and not isinstance(args[key], bool)
    ]
    try:
        cfg = _config.configure(
            root,
            librarian=args.get("librarian"),
            enable=enable if isinstance(enable, bool) else None,
            commit_record=commit if isinstance(commit, bool) else None,
            names=LIBRARIANS,
            rejected=rejected,
        )
    except Exception as exc:  # configure() handles its own errors; this is the net
        return text_result(f"teamme_librarian_configure failed: {exc}", True)

    read_only = not cfg.get("changes")
    lines = [
        "teamme_librarian_configure" + (" (reporting only, nothing was changed)" if read_only else ""),
        f"  config file:     {cfg['path']}"
        + ("" if cfg.get("exists") else " (absent - the documented defaults are in force)"),
        "",
    ]
    for lname in LIBRARIANS:
        on = cfg["librarians"].get(lname, {}).get("enabled", True)
        lines.append(f"  {lname}: " + ("enabled" if on else
                                       "DISABLED - refresh and query refuse for it; status still works"))
    lines.append(f"  commit_record: {str(bool(cfg.get('commit_record'))).lower()}"
                 + ("  - the record is committed with the code"
                    if cfg.get("commit_record") else
                    "  - the record is kept out of git"))

    if cfg.get("changes"):
        lines += ["", "changed:"] + [f"  {c}" for c in cfg["changes"]]
        lines.append("  config.json written" if cfg.get("written")
                     else "  config.json was NOT written - see problems below")

    gi = cfg.get("gitignore")
    if gi:
        lines += ["", f"gitignore: {gi['path']}"]
        if not gi.get("ok"):
            lines.append("  NOT changed - see problems below")
        elif gi.get("changed"):
            lines.append("  updated. teamme only ever rewrites the lines between its own markers; "
                         "an ignore rule you wrote yourself is never removed.")
            for entry in gi.get("wrote") or []:
                lines.append(f"    ignoring: {entry}")
            if not gi.get("wrote"):
                lines.append("    teamme's block removed: the record is no longer ignored")
        else:
            lines.append("  already correct, nothing to do")
        for note in gi.get("notes") or []:
            lines.append(f"  note: {note}")
        if cfg.get("commit_record"):
            lines.append("  the .db is still ignored - that is never a choice, a binary index "
                         "cannot be merged")

    for problem in cfg.get("problems") or []:
        lines.append(f"  problem: {problem}")

    if read_only:
        lines += ["", 'Change something by passing arguments, e.g. {"librarian": "history", '
                      '"enabled": false} or {"commit_record": true}.']
    # isError only when something the caller ASKED for did not happen. A note
    # about an ignored unknown key is information, not a failure.
    failed = bool(cfg.get("changes")) and not cfg.get("written")
    failed = failed or bool(gi and not gi.get("ok")) or bool(rejected)
    return text_result("\n".join(lines), failed)


def call_tool(name: str, args: dict) -> dict:
    args = args if isinstance(args, dict) else {}
    root = resolve_project(args)
    if name == "teamme_status":
        return tool_status(root, args)
    if name == "teamme_install":
        return install(root, bool(args.get("force")))
    if name == "teamme_worklog":
        return tool_worklog(root, args)
    if name == "teamme_intake_phase":
        return tool_intake_phase(root, args)
    if name == "teamme_librarian_status":
        return tool_librarian_status(root, args)
    if name == "teamme_librarian_refresh":
        return tool_librarian_refresh(root, args)
    if name == "teamme_librarian_query":
        return tool_librarian_query(root, args)
    if name == "teamme_librarian_configure":
        return tool_librarian_configure(root, args)
    return text_result(
        f"unknown tool '{name}'. Available: " + ", ".join(t["name"] for t in TOOLS), True
    )


# --------------------------------------------------------------------------- #
# JSON-RPC
# --------------------------------------------------------------------------- #

def handle(msg: dict):
    """Response dict, or None for a notification."""
    method = msg.get("method")
    is_request = "id" in msg
    mid = msg.get("id")

    if method == "initialize":
        asked = ((msg.get("params") or {}).get("protocolVersion")) or DEFAULT_PROTOCOL
        version = asked if asked in KNOWN_PROTOCOLS else DEFAULT_PROTOCOL
        result = {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        params = msg.get("params") or {}
        try:
            result = call_tool(params.get("name") or "", params.get("arguments") or {})
        except Exception as exc:
            result = text_result(f"teamme: {params.get('name')} failed: {exc}", True)
    elif method == "ping":
        result = {}
    elif isinstance(method, str) and method.startswith("notifications/"):
        return None
    else:
        if not is_request:
            return None
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}

    if not is_request:
        return None
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def serve(stdin=None, stdout=None) -> int:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    while True:
        try:
            line = stdin.readline()
        except Exception:
            return 0
        if not line:
            return 0  # EOF: the client went away
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            resp = {"jsonrpc": "2.0", "id": None,
                    "error": {"code": -32700, "message": "parse error"}}
        else:
            if not isinstance(msg, dict):
                resp = {"jsonrpc": "2.0", "id": None,
                        "error": {"code": -32600, "message": "invalid request"}}
            else:
                try:
                    resp = handle(msg)
                except Exception as exc:
                    resp = (
                        {"jsonrpc": "2.0", "id": msg.get("id"),
                         "error": {"code": -32603, "message": f"internal error: {exc}"}}
                        if "id" in msg else None
                    )
        if resp is None:
            continue
        try:
            stdout.write(json.dumps(resp) + "\n")
            stdout.flush()
        except Exception:
            return 0  # the client closed the pipe


if __name__ == "__main__":
    try:
        raise SystemExit(serve() or 0)
    except SystemExit:
        raise
    except KeyboardInterrupt:
        raise SystemExit(0)
    except Exception:
        raise SystemExit(0)
