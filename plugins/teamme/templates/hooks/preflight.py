#!/usr/bin/env python3
"""Is teamme actually working in this project? The single source of truth.

Three states, and telling them apart is the whole point:

  not-installed      the scaffolding is missing - nothing can work yet
  installed-not-live the hooks are on disk and registered in settings.json, but
                     they have never fired here. Almost always means the harness
                     has not loaded them yet: approve them with /hooks, or start
                     a fresh session
  live               a session start has actually executed this script, so the
                     hook wiring is proven, not assumed

Liveness is proven by a heartbeat file that SessionStart writes. It is evidence
only: NOTHING reads it to decide whether an action is allowed. It is not a second
lock, it cannot expire into a deny, and deleting it costs nothing but a restart.

Used two ways, with one implementation so the two can never disagree:

  CLI     python3 preflight.py check        one line per item, exit 0 iff all pass
          python3 preflight.py check --json the same result as JSON
          python3 preflight.py heartbeat    SessionStart mode: writes the
                                            heartbeat, prints nothing, always
                                            exits 0 - it can never block a
                                            session from starting

  import  diagnose(project_dir=None) -> dict with keys ok, state, installed,
          project_dir, checks (a list of {id,label,ok,detail,fix}), summary.
          render(result) -> list[str] for display.

Nothing here writes anything except the heartbeat, and nothing here can deny a
tool call or block a prompt.
"""

import json
import os
import pathlib
import sys
import time

# The scaffolding this project should have. These are teamme's own filenames -
# no assumption about the project they are installed into.
REQUIRED_HOOKS = (
    "intake-state.py",
    "intake-guard.py",
    "route-to-intake.py",
    "worklog-enforce.py",
    "worklog.py",
    "preflight.py",
)
REQUIRED_EVENTS = ("UserPromptSubmit", "PreToolUse", "SessionStart", "Stop")
SETTINGS_FILES = ("settings.json", "settings.local.json")
HEARTBEAT_NAME = "heartbeat.json"

INSTALL_FIX = (
    "run the `teamme_install` MCP tool, or /teamme:team-doctor, to repair the scaffolding - or "
    "/teamme:init-team if this project has never been set up"
)


def project_root(project_dir=None) -> pathlib.Path:
    if project_dir:
        return pathlib.Path(project_dir).expanduser().resolve()
    return pathlib.Path(os.environ.get("CLAUDE_PROJECT_DIR") or ".").resolve()


def heartbeat_path(project_dir=None) -> pathlib.Path:
    return project_root(project_dir) / ".claude" / "intake" / HEARTBEAT_NAME


# --------------------------------------------------------------------------- #
# heartbeat: the SessionStart side. Must never fail loudly, ever.
# --------------------------------------------------------------------------- #

def write_heartbeat(project_dir=None) -> bool:
    """Stamp 'a session start ran here'. Returns success; never raises."""
    try:
        p = heartbeat_path(project_dir)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps(
                {
                    "at": time.time(),
                    "iso": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "source": "SessionStart",
                    "note": "evidence that teamme hooks fire here; nothing reads this to gate anything",
                },
                indent=2,
            )
            + "\n"
        )
        return True
    except Exception:
        return False  # a read-only checkout must still start a session


def read_heartbeat(project_dir=None) -> dict:
    """Heartbeat contents, or {} for missing/unreadable/garbage."""
    try:
        d = json.loads(heartbeat_path(project_dir).read_text())
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _age(seconds: float) -> str:
    seconds = max(0.0, seconds)
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{int(seconds // size)}{unit} ago"
    return "just now"


# --------------------------------------------------------------------------- #
# the checks - one function each, every one of them failure-tolerant
# --------------------------------------------------------------------------- #

def _item(cid, label, ok, detail, fix="") -> dict:
    return {"id": cid, "label": label, "ok": bool(ok), "detail": detail, "fix": "" if ok else fix}


def _check_python(root: pathlib.Path) -> dict:
    v = sys.version_info
    return _item(
        "python3",
        "python3",
        True,
        f"{v.major}.{v.minor}.{v.micro} ({sys.executable or 'python3'})",
    )


def _check_hooks(root: pathlib.Path) -> dict:
    d = root / ".claude" / "hooks"
    missing = [n for n in REQUIRED_HOOKS if not (d / n).is_file()]
    if missing:
        return _item(
            "hooks",
            "hook scripts",
            False,
            f"{len(REQUIRED_HOOKS) - len(missing)}/{len(REQUIRED_HOOKS)} in .claude/hooks - "
            f"missing: {', '.join(missing)}",
            INSTALL_FIX,
        )
    return _item("hooks", "hook scripts", True, f"{len(REQUIRED_HOOKS)}/{len(REQUIRED_HOOKS)} in .claude/hooks")


def _check_command(root: pathlib.Path) -> dict:
    p = root / ".claude" / "commands" / "intake.md"
    if p.is_file():
        return _item("command", "intake command", True, ".claude/commands/intake.md")
    return _item(
        "command",
        "intake command",
        False,
        ".claude/commands/intake.md is missing",
        "run /teamme:init-team - intake.md is written for this project, not copied verbatim",
    )


def _load_settings(root: pathlib.Path):
    """Every readable settings file, as (name, parsed dict). Unreadable ones are
    reported so 'my settings.json is invalid JSON' never looks like 'not installed'."""
    found, broken = [], []
    for name in SETTINGS_FILES:
        p = root / ".claude" / name
        if not p.is_file():
            continue
        try:
            data = json.loads(p.read_text())
            found.append((name, data if isinstance(data, dict) else {}))
        except Exception:
            broken.append(name)
    return found, broken


def _check_settings(root: pathlib.Path) -> dict:
    found, broken = _load_settings(root)
    if broken:
        return _item(
            "settings",
            "hook registration",
            False,
            f"could not parse .claude/{', .claude/'.join(broken)}",
            "fix the JSON syntax in that file - Claude Code ignores hooks it cannot parse",
        )
    if not found:
        return _item(
            "settings",
            "hook registration",
            False,
            "no .claude/settings.json",
            INSTALL_FIX,
        )
    present, where = set(), {}
    for name, data in found:
        hooks = data.get("hooks")
        if not isinstance(hooks, dict):
            continue
        for ev in REQUIRED_EVENTS:
            if hooks.get(ev):
                present.add(ev)
                where.setdefault(ev, name)
    missing = [e for e in REQUIRED_EVENTS if e not in present]
    if missing:
        return _item(
            "settings",
            "hook registration",
            False,
            f"{len(present)}/{len(REQUIRED_EVENTS)} events registered - missing: {', '.join(missing)}",
            "run the `teamme_install` MCP tool, or /teamme:team-doctor - it merges the hooks "
            "block into settings.json without touching anything else",
        )
    files = sorted({v for v in where.values()})
    return _item(
        "settings",
        "hook registration",
        True,
        f"all {len(REQUIRED_EVENTS)} events in .claude/{', .claude/'.join(files)}",
    )


def _check_intake_dir(root: pathlib.Path) -> dict:
    d = root / ".claude" / "intake"
    if not d.is_dir():
        return _item(
            "intake_dir",
            "intake state dir",
            False,
            ".claude/intake/ does not exist",
            INSTALL_FIX,
        )
    if not os.access(str(d), os.W_OK | os.X_OK):
        return _item(
            "intake_dir",
            "intake state dir",
            False,
            ".claude/intake/ is not writable",
            "make .claude/intake/ writable - the phase lock and the work log are written there",
        )
    return _item("intake_dir", "intake state dir", True, ".claude/intake/ exists and is writable")


def _check_liveness(root: pathlib.Path, scaffolded: bool) -> dict:
    hb = read_heartbeat(root)
    if hb:
        try:
            detail = f"SessionStart last fired {_age(time.time() - float(hb.get('at') or 0))}"
        except Exception:
            detail = "SessionStart has fired here"
        return _item("liveness", "hooks firing", True, detail)
    if not scaffolded:
        return _item(
            "liveness",
            "hooks firing",
            False,
            "never - the scaffolding is not installed yet",
            "clear the failures above, then start a new session - the next session start is what "
            "records the heartbeat",
        )
    return _item(
        "liveness",
        "hooks firing",
        False,
        "registered but no session start has run them here yet",
        "approve the hooks with /hooks, or restart Claude Code - the next session start proves it",
    )


# --------------------------------------------------------------------------- #
# the diagnosis
# --------------------------------------------------------------------------- #

def diagnose(project_dir=None) -> dict:
    """Full diagnosis as plain data. Never raises: a check that blows up is
    reported as a failed check, so a caller can always render something."""
    try:
        root = project_root(project_dir)
    except Exception as exc:  # pragma: no cover - only a pathological cwd
        return {
            "ok": False,
            "installed": False,
            "state": "not-installed",
            "project_dir": str(project_dir or ""),
            "checks": [_item("python3", "python3", False, f"cannot resolve project dir: {exc}", INSTALL_FIX)],
            "summary": "could not resolve the project directory",
        }

    checks = []
    for cid, fn in (
        ("python3", _check_python),
        ("hooks", _check_hooks),
        ("command", _check_command),
        ("settings", _check_settings),
        ("intake_dir", _check_intake_dir),
    ):
        try:
            checks.append(fn(root))
        except Exception as exc:
            checks.append(_item(cid, cid, False, f"check failed: {exc}", INSTALL_FIX))

    by_id = {c["id"]: c for c in checks}
    scaffolded = by_id.get("hooks", {}).get("ok") and by_id.get("settings", {}).get("ok")
    try:
        live = _check_liveness(root, bool(scaffolded))
    except Exception as exc:
        live = _item("liveness", "hooks firing", False, f"check failed: {exc}", INSTALL_FIX)
    checks.append(live)

    if not scaffolded:
        state = "not-installed"
        summary = "teamme is not installed in this project"
    elif live["ok"]:
        state = "live"
        summary = "teamme is installed and its hooks are firing"
    else:
        state = "installed-not-live"
        summary = "teamme is installed but its hooks have not fired here yet"

    return {
        "ok": all(c["ok"] for c in checks),
        "installed": bool(scaffolded),
        "state": state,
        "project_dir": str(root),
        "checks": checks,
        "summary": summary,
    }


def render(result: dict) -> list:
    """Human-readable lines for a diagnosis, used by the CLI and by the MCP server."""
    lines = [f"teamme preflight - {result.get('project_dir', '?')}", ""]
    width = max([len(c.get("label", "")) for c in result.get("checks") or []] or [8])
    for c in result.get("checks") or []:
        mark = "PASS" if c.get("ok") else "FAIL"
        lines.append(f"  {mark}  {c.get('label', ''):<{width}}  {c.get('detail', '')}")
        if c.get("fix"):
            lines.append(f"        {'':<{width}}  fix: {c['fix']}")
    lines.append("")
    lines.append(f"state: {result.get('state', '?')} - {result.get('summary', '')}")
    return lines


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    mode = argv[0] if argv and not argv[0].startswith("-") else "check"

    if mode == "heartbeat":
        try:
            sys.stdin.read()  # drain the SessionStart payload; nothing here needs it
        except Exception:
            pass
        write_heartbeat()
        return 0  # ALWAYS. A session start is never blocked by this script.

    if mode in ("check", "status"):
        want_json = "--json" in argv
        project_dir = None
        if "--project-dir" in argv:
            i = argv.index("--project-dir")
            if i + 1 < len(argv):
                project_dir = argv[i + 1]
        result = diagnose(project_dir)
        if want_json:
            print(json.dumps(result, indent=2))
        else:
            print("\n".join(render(result)))
        return 0 if result.get("ok") else 1

    print(f"usage: {pathlib.Path(__file__).name} check [--json] [--project-dir DIR] | heartbeat",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        # Unreachable by design, but a preflight check that crashes the process
        # it is meant to reassure would be the worst possible failure.
        raise SystemExit(1)
