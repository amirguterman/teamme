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
SERVER_VERSION = "0.1.0"
DEFAULT_PROTOCOL = "2025-06-18"
KNOWN_PROTOCOLS = {"2024-11-05", "2025-03-26", "2025-06-18"}
CALL_TIMEOUT = 30

PLUGIN_ROOT = pathlib.Path(__file__).resolve().parent.parent

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
