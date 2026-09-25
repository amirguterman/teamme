#!/usr/bin/env python3
"""Is teamme actually working in this project? The single source of truth.

Four states, and telling them apart is the whole point:

  not-installed      nothing here was ever installed by teamme - no registered
                     teamme hooks, no generated intake command. This is the only
                     state where /teamme:init-team is the right advice
  installed-outdated teamme was installed here, but the scaffolding is no longer
                     complete - typically an install from an earlier release that
                     predates a hook script this version expects, or one whose
                     hook scripts are still the older release's. REPAIR it
                     (`teamme_install` / /teamme:team-doctor); never re-run the
                     installer over a working team
  installed-not-live the hooks are on disk and registered in settings.json, but
                     they have never fired here. Almost always means the harness
                     has not loaded them yet: approve them with /hooks, or start
                     a fresh session
  live               a session start has actually executed this script, so the
                     hook wiring is proven, not assumed

"Installed" is deliberately NOT derived from the hook-script list. REQUIRED_HOOKS
grows every release, so a complete install from an older release would otherwise
score 5/6 and be reported as never installed - and be told to run the installer
over a working team. Existence is evidence-based (registered teamme hooks, or the
generated intake command); a missing hook *script* is a repair condition.

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
          python3 preflight.py roster       a SEPARATE verdict: does the team
                                            roster still agree with itself in
                                            all four places it is written down?
                                            Never part of `check` - see the
                                            banner above roster() for why

  import  diagnose(project_dir=None) -> dict with keys ok, state, installed,
          project_dir, checks (a list of {id,label,ok,detail,fix}), summary.
          render(result) -> list[str] for display.
          roster(project_dir=None) -> dict with keys ok, project_dir, checks
          (a list of {id,state,ok,detail,fix}), summary.
          render_roster(result) -> list[str] for display.

Nothing here writes anything except the heartbeat, and nothing here can deny a
tool call or block a prompt.
"""

import json
import os
import pathlib
import re
import select
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
    "librarian-gate.py",
)
REQUIRED_EVENTS = ("UserPromptSubmit", "PreToolUse", "SessionStart", "Stop")
SETTINGS_FILES = ("settings.json", "settings.local.json")
HEARTBEAT_NAME = "heartbeat.json"

# A directory only counts as the plugin's shipped templates/hooks if this file
# sits beside the others AND the settings template is next door. That second
# probe is what stops a project's own .claude/hooks from being mistaken for the
# templates and compared against itself - which would report every install fresh.
TEMPLATE_MARKER = "settings.hooks.json"

# This plugin's own name, as it appears before the '@' in the harness's install
# record key ("<plugin>@<marketplace>"). teamme's own name, not the host
# project's - invariant #4 is about assumptions concerning the project this is
# installed into, and this makes none.
PLUGIN_NAME = "teamme"

INSTALL_FIX = (
    "run the `teamme_install` MCP tool, or /teamme:team-doctor, to repair the scaffolding - or "
    "/teamme:init-team if this project has never been set up"
)

# The same failure in a project that IS already installed. diagnose() swaps this
# in once the state is known, so the individual checks stay context-free and no
# existing install is ever told to run the installer over its own team.
REPAIR_FIX = (
    "run the `teamme_install` MCP tool, or /teamme:team-doctor, to repair this install - teamme is "
    "already set up in this project, so repair the scaffolding rather than re-running the installer "
    "over a working team"
)

# A hook that is present but does not match the shipped copy needs a *different*
# repair from a missing one: teamme_install deliberately refuses to overwrite it,
# because the difference may be a deliberate local edit rather than an old file.
FORCE_FIX = (
    "run the `teamme_install` MCP tool with force=true, or /teamme:team-doctor - a plain repair "
    "leaves a hook that differs in place on purpose (it could be a local edit), so replacing it "
    "with the version this release ships takes force=true"
)


def project_root(project_dir=None) -> pathlib.Path:
    if project_dir:
        return pathlib.Path(project_dir).expanduser().resolve()
    return pathlib.Path(os.environ.get("CLAUDE_PROJECT_DIR") or ".").resolve()


def heartbeat_path(project_dir=None) -> pathlib.Path:
    return project_root(project_dir) / ".claude" / "intake" / HEARTBEAT_NAME


# --------------------------------------------------------------------------- #
# freshness: one definition of "does the installed hook match the shipped one"
# --------------------------------------------------------------------------- #

def _is_template_hooks_dir(p) -> bool:
    """True only for the plugin's own templates/hooks directory. Never raises."""
    try:
        p = pathlib.Path(p)
        return bool(
            p.is_dir()
            and (p / "preflight.py").is_file()
            and (p.parent / TEMPLATE_MARKER).is_file()
        )
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# HARNESS-SHAPED, BEST-EFFORT. Everything between this banner and the next one
# encodes one assumption about Claude Code's own on-disk layout. It is the only
# such assumption in this file; if the harness changes, this is the single place
# to fix, and until it is fixed every function here returns nothing and freshness
# degrades to an honest "not verified" rather than to a wrong answer.
# --------------------------------------------------------------------------- #

def _install_record_files() -> list:
    """Where the harness records its plugin installs, most specific first."""
    out = []
    try:
        cfg = os.environ.get("CLAUDE_CONFIG_DIR")
        if cfg:
            out.append(pathlib.Path(cfg).expanduser() / "plugins" / "installed_plugins.json")
    except Exception:
        pass
    try:
        out.append(pathlib.Path.home() / ".claude" / "plugins" / "installed_plugins.json")
    except Exception:
        pass
    return out


def _same_dir(a, b) -> bool:
    try:
        return pathlib.Path(str(a)).expanduser().resolve() == pathlib.Path(str(b)).expanduser().resolve()
    except Exception:
        return False


def _install_record_template_dirs(root=None) -> list:
    """Candidate templates/hooks directories according to the harness's own
    install record, best first. [] for anything unexpected. Never raises.

    Today that record is $CLAUDE_CONFIG_DIR/plugins/installed_plugins.json
    (~/.claude/plugins/installed_plugins.json when CLAUDE_CONFIG_DIR is unset),
    shaped {"plugins": {"<plugin>@<marketplace>": [{scope, projectPath, version,
    installPath, ...}, ...]}}. Every field is treated as untrusted: the caller
    still marker-checks whatever comes back, so a stale or bogus installPath
    costs a `not verified`, never a wrong verdict.

    Read live, on purpose - NOT captured at install time. The plugin cache is
    version-pinned and old versions stay on disk, so a path remembered at install
    time keeps pointing at the version the user installed FROM: after an upgrade
    it would compare their stale hooks against equally stale templates and call
    them all fresh, precisely when they are not. The record is rewritten by the
    upgrade, so reading it is self-correcting.

    Entries installed for THIS project win. An entry with no projectPath is a
    user/global install, which applies everywhere, so it is the fallback (newest
    lastUpdated first). An entry belonging to some *other* project says nothing
    about this one and is ignored rather than guessed at.
    """
    for rec in _install_record_files():
        try:
            if not rec.is_file():
                continue
            data = json.loads(rec.read_text())
        except Exception:
            continue  # unreadable or not JSON: no evidence, not an error
        if not isinstance(data, dict):
            continue
        plugins = data.get("plugins")
        if not isinstance(plugins, dict):
            continue
        mine, global_ = [], []
        try:
            for key, entries in plugins.items():
                if str(key).split("@", 1)[0] != PLUGIN_NAME:
                    continue
                if not isinstance(entries, list):
                    continue
                for e in entries:
                    if not isinstance(e, dict):
                        continue
                    where = e.get("installPath")
                    if not isinstance(where, str) or not where:
                        continue
                    hooks = pathlib.Path(where).expanduser() / "templates" / "hooks"
                    proj = e.get("projectPath")
                    if isinstance(proj, str) and proj:
                        if root is not None and _same_dir(proj, root):
                            mine.append(hooks)
                    else:
                        global_.append((str(e.get("lastUpdated") or ""), str(hooks)))
        except Exception:
            continue  # a schema we do not recognise: say nothing
        found = mine + [pathlib.Path(p) for _, p in sorted(global_, reverse=True)]
        if found:
            return found
    return []


# --------------------------------------------------------------------------- #
# end of the harness-shaped section
# --------------------------------------------------------------------------- #

# How each resolution path is described in the verdict. A reader must be able to
# tell "checked, against this" from "could not check".
TEMPLATE_SOURCES = {
    "hint": "the templates directory the caller named",
    "env": "$CLAUDE_PLUGIN_ROOT",
    "self": "this script's own directory",
    "record": "the harness's plugin install record",
}


def template_hooks_source(hint=None, root=None):
    """(directory, source-label) for the plugin's shipped hook scripts, or
    (None, "") if they cannot be found from here. Never raises.

    This runs as a hook *inside a user's project*, where the plugin directory is
    often unreachable: CLAUDE_PLUGIN_ROOT is only set for hooks a plugin
    registers itself, and teamme's hooks are registered by the project. So
    (None, "") is an ordinary, expected answer, and every caller must treat it as
    "could not verify", never as "broken".

    The label exists so the two outcomes can never be confused in a report.
    """
    candidates = []
    try:
        if hint:
            h = pathlib.Path(str(hint)).expanduser()
            candidates += [(h, "hint"), (h / "hooks", "hint"), (h / "templates" / "hooks", "hint")]
    except Exception:
        pass
    try:
        env = os.environ.get("CLAUDE_PLUGIN_ROOT")
        if env:
            e = pathlib.Path(env).expanduser()
            candidates += [(e / "templates" / "hooks", "env"), (e / "hooks", "env")]
    except Exception:
        pass
    try:
        # The case that matters for the MCP server: this file IS the template.
        candidates.append((pathlib.Path(__file__).resolve().parent, "self"))
    except Exception:
        pass
    try:
        # Last, so nothing that already worked changes: the in-project copy, with
        # no plugin root in its environment, asking the harness where the plugin
        # it was installed from lives now.
        if root is None:
            root = project_root()
        candidates += [(p, "record") for p in _install_record_template_dirs(root)]
    except Exception:
        pass
    for c, why in candidates:
        if _is_template_hooks_dir(c):
            try:
                return pathlib.Path(c).resolve(), TEMPLATE_SOURCES.get(why, why)
            except Exception:
                return pathlib.Path(c), TEMPLATE_SOURCES.get(why, why)
    return None, ""


def template_hooks_dir(hint=None, root=None):
    """Where the plugin's shipped hook scripts are, or None. Never raises."""
    return template_hooks_source(hint, root)[0]


def hook_freshness(installed, template) -> str:
    """'same', 'differs', or 'unknown' - the ONE definition of whether an
    installed hook script matches the copy this release ships.

    teamme_install reads this to decide whether to refuse an overwrite without
    force=true, and _check_hooks reads it to decide whether an install is stale.
    A second implementation would drift, and the two disagreeing is exactly the
    bug this exists to prevent: the installer skipping a file it calls "differs"
    while the health check called the same install fresh.

    Byte-exact on purpose, and named "differs" rather than "outdated" for the
    same reason the installer is: a deliberate local edit lands here too, and it
    is divergence, which is all this claims. Never raises.
    """
    try:
        a = pathlib.Path(installed)
        b = pathlib.Path(template)
        if not a.is_file() or not b.is_file():
            return "unknown"
        return "same" if a.read_bytes() == b.read_bytes() else "differs"
    except Exception:
        return "unknown"


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


def drain_stdin(limit_seconds: float = 0.5, max_bytes: int = 1 << 20) -> None:
    """Consume a piped payload without ever waiting for one. Never raises.

    Nothing here needs the SessionStart payload, but leaving it unread hands the
    writer a broken pipe. A plain `sys.stdin.read()` waits for EOF, so any caller
    whose stdin is a terminal - or a pipe held open by something else - would
    hang a session start. So the drain is bounded twice: it stops at a deadline
    and at a byte cap, it skips a terminal outright, and a platform that cannot
    poll a pipe just reads nothing. Discarding a payload costs nothing; blocking
    on one costs the session.
    """
    try:
        stream = sys.stdin
        if stream is None or stream.closed or stream.isatty():
            return
        fd = stream.fileno()
        deadline = time.monotonic() + max(0.0, limit_seconds)
        seen = 0
        while seen < max_bytes:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return  # slow or silent writer: give up rather than wait
            if not select.select([fd], [], [], remaining)[0]:
                return  # nothing offered in time
            chunk = os.read(fd, 65536)
            if not chunk:
                return  # EOF: the normal path
            seen += len(chunk)
    except Exception:
        return  # no fileno, no select on this handle, closed mid-read - all fine


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


def _check_hooks(root: pathlib.Path, templates=None) -> dict:
    """Present AND current. Missing and stale are different problems with
    different repairs, so they are reported separately - "5/6 present" told a
    user nothing when the real trouble was three scripts from the last release.

    When the shipped templates cannot be found this degrades to the old
    existence-only check and says so *in those words*: an install that cannot see
    its own templates is not thereby broken, and this must never fail for that -
    but neither may "not checked" be reported in the wording of "checked and
    clean". Every branch below names which of the two happened.
    """
    d = root / ".claude" / "hooks"
    total = len(REQUIRED_HOOKS)
    missing = [n for n in REQUIRED_HOOKS if not (d / n).is_file()]
    present = [n for n in REQUIRED_HOOKS if n not in missing]

    tdir, tsource = template_hooks_source(templates, root)
    try:
        # Comparing a directory with itself would call every install fresh.
        if tdir is not None and tdir == d.resolve():
            tdir, tsource = None, ""
    except Exception:
        pass

    stale, unchecked = [], []
    if tdir is not None:
        for n in present:
            verdict = hook_freshness(d / n, tdir / n)
            if verdict == "differs":
                stale.append(n)
            elif verdict != "same":
                unchecked.append(n)

    parts, fixes = [], []
    if missing:
        parts.append(f"missing: {', '.join(missing)}")
        fixes.append(INSTALL_FIX)
    if stale:
        parts.append(f"differs from the copy the plugin ships ({tsource}): {', '.join(stale)}")
        fixes.append(FORCE_FIX)

    if parts:
        return _item(
            "hooks",
            "hook scripts",
            False,
            f"{len(present)}/{total} in .claude/hooks - " + "; ".join(parts),
            "; also: ".join(fixes),
        )

    if tdir is None:
        # NOT the same statement as "all matching", and must never read like it:
        # the scripts are present and nothing compared them. /teamme:team-doctor
        # runs inside the plugin, where the shipped copies are always reachable.
        detail = f"{total}/{total} present in .claude/hooks - NOT compared: the copies this release " \
                 f"ships are not reachable from here, so freshness was not verified (run " \
                 f"/teamme:team-doctor to check)"
    elif unchecked:
        detail = f"{total}/{total} in .claude/hooks, matching the copy the plugin ships ({tsource}) " \
                 f"except {len(unchecked)} it does not ship: {', '.join(unchecked)}"
    else:
        detail = f"{total}/{total} in .claude/hooks, all matching the copy the plugin ships ({tsource})"
    return _item("hooks", "hook scripts", True, detail)


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


def _install_evidence(root: pathlib.Path) -> list:
    """Why we believe teamme was installed here, as a list of reasons ([] = none).

    Both probes are deliberately independent of REQUIRED_HOOKS: neither changes
    when a release adds a hook script, so an install can never age out of being
    an install. Every probe is individually failure-tolerant - an unreadable
    settings file just contributes no evidence.

    Both probes also look for teamme's OWN filenames, never for a word a project
    might use for its own reasons: "intake" is not a teamme-only word, so the
    mere existence of .claude/commands/intake.md proves nothing - a project that
    wrote its own would otherwise be reported as installed and pointed at a
    repair that writes teamme scaffolding into a repo that never asked for it.
    A confidently wrong verdict driving a write is worse than no evidence.

    The two probes are independent, which is what makes the stricter test safe:
    a real install would have to lose BOTH its settings hooks block AND every
    reference to a teamme script in its intake.md before it read as
    not-installed.
    """
    reasons = []
    try:
        found, _ = _load_settings(root)
        for name, data in found:
            hooks = data.get("hooks")
            if not isinstance(hooks, dict):
                continue
            if any(_names_a_teamme_hook(g) for g in hooks.values()):
                reasons.append(f".claude/{name} registers teamme's hooks")
                break
    except Exception:
        pass
    try:
        cmd = root / ".claude" / "commands" / "intake.md"
        if cmd.is_file() and _text_names_a_teamme_hook(cmd):
            reasons.append(".claude/commands/intake.md names teamme's hooks")
    except Exception:
        pass
    return reasons


def _text_names_a_teamme_hook(path: pathlib.Path) -> bool:
    """True if this file's text mentions one of teamme's own hook scripts.

    `any`, not `all`, for the same reason _names_a_teamme_hook uses it: a
    release that adds a hook only ever adds another way to match, so an older
    install can never age out of matching. Read with `all` this would be the
    exact trap that made `installed` derivation a P0.

    Safe because a generated /intake command names these scripts by
    construction - its own steps invoke them. Measured across every version of
    the plugin's intake.md template in this repo's history rather than assumed:
    22 to 27 references, at least two distinct scripts, never zero.
    """
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            # A command file is a few KB. Cap the read so an unrelated giant
            # file at that path costs a page, not a whole disk.
            text = fh.read(1 << 20)
    except Exception:
        return False
    return any(n in text for n in REQUIRED_HOOKS)


def _names_a_teamme_hook(groups) -> bool:
    """True if any hook command in this event mentions one of teamme's scripts.
    `any`, not `all`: an install predating the newest hook still matches."""
    try:
        if not isinstance(groups, list):
            return False
        for g in groups:
            if not isinstance(g, dict):
                continue
            for h in g.get("hooks") or []:
                cmd = h.get("command") if isinstance(h, dict) else None
                if isinstance(cmd, str) and any(n in cmd for n in REQUIRED_HOOKS):
                    return True
    except Exception:
        return False
    return False


def _check_liveness(root: pathlib.Path, stage: str = "complete") -> dict:
    if stage is True:            # tolerate the old boolean argument
        stage = "complete"
    elif stage is False:
        stage = "not-installed"
    hb = read_heartbeat(root)
    if hb:
        try:
            detail = f"SessionStart last fired {_age(time.time() - float(hb.get('at') or 0))}"
        except Exception:
            detail = "SessionStart has fired here"
        return _item("liveness", "hooks firing", True, detail)
    if stage == "outdated":
        # Restarting alone can never produce a heartbeat here: the missing piece
        # is the wiring itself, so the repair has to come first.
        return _item(
            "liveness",
            "hooks firing",
            False,
            "never here - part of this install predates the current scaffolding",
            "repair the install first (`teamme_install`, or /teamme:team-doctor), then start a new "
            "session - until the scaffolding is complete a restart cannot record a heartbeat",
        )
    if stage == "not-installed":
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

def diagnose(project_dir=None, templates=None) -> dict:
    """Full diagnosis as plain data. Never raises: a check that blows up is
    reported as a failed check, so a caller can always render something.

    `templates` is an optional hint at the plugin's templates directory, for the
    one caller that knows where it is (the MCP server). Without it the hook
    self-locates - including, for a copy living in a project, by asking the
    harness's own install record where the plugin it came from lives now - and
    where it cannot, the freshness comparison degrades to the existence check,
    reported as "not verified", rather than failing."""
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
        ("hooks", lambda r: _check_hooks(r, templates)),
        ("command", _check_command),
        ("settings", _check_settings),
        ("intake_dir", _check_intake_dir),
    ):
        try:
            checks.append(fn(root))
        except Exception as exc:
            checks.append(_item(cid, cid, False, f"check failed: {exc}", INSTALL_FIX))

    by_id = {c["id"]: c for c in checks}
    # Does the scaffolding still have every piece this release expects?
    complete = bool(by_id.get("hooks", {}).get("ok") and by_id.get("settings", {}).get("ok"))
    # Was teamme ever installed here at all? Evidence that does not grow with
    # REQUIRED_HOOKS, so `complete` failing is a repair, not a non-install.
    try:
        evidence = _install_evidence(root)
    except Exception:
        evidence = [] if not complete else ["the scaffolding is present"]
    installed = bool(evidence) or complete

    if not installed:
        stage = "not-installed"
    elif not complete:
        stage = "outdated"
    else:
        stage = "complete"

    try:
        live = _check_liveness(root, stage)
    except Exception as exc:
        live = _item("liveness", "hooks firing", False, f"check failed: {exc}",
                     REPAIR_FIX if installed else INSTALL_FIX)
    checks.append(live)

    if stage == "not-installed":
        state = "not-installed"
        summary = "teamme is not installed in this project"
    elif stage == "outdated":
        state = "installed-outdated"
        summary = (
            "teamme is installed here (" + "; ".join(evidence or ["scaffolding present"]) + ") but "
            "part of its scaffolding is missing or no longer matches this release - repair it, do "
            "not reinstall"
        )
    elif live["ok"]:
        state = "live"
        summary = "teamme is installed and its hooks are firing"
    else:
        state = "installed-not-live"
        summary = "teamme is installed but its hooks have not fired here yet"

    if state == "installed-outdated":
        # Every fix line written for a fresh project offers /teamme:init-team as
        # an alternative. In an existing install that advice is actively harmful,
        # so it is rewritten here - once, where the state is actually known.
        for c in checks:
            fix = c.get("fix") or ""
            # Substring, not equality: a check can now carry INSTALL_FIX joined
            # to another repair (missing hooks AND stale ones), and that combined
            # line must still lose its init-team advice.
            if INSTALL_FIX in fix:
                c["fix"] = fix.replace(INSTALL_FIX, REPAIR_FIX)
            elif c.get("id") == "command" and c.get("fix"):
                # intake.md really is generated, not copied, so that one check
                # has to keep pointing at the installer - scoped to itself, so it
                # cannot be read as "reinstall the whole project".
                c["fix"] += (
                    " - only that file; the rest of this install is already here, so repair the "
                    "other failures above with `teamme_install` instead"
                )

    return {
        "ok": all(c["ok"] for c in checks),
        "installed": installed,
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


# --------------------------------------------------------------------------- #
# ROSTER CONSISTENCY - a separate verdict, deliberately NOT part of check().
#
# A team's roster is one fact written down in four places: the agent files in
# .claude/agents/, the roster table in .claude/agents/README.md, the lane table
# in the generated .claude/commands/intake.md, and the `lane` field on every
# task in .claude/intake/worklog.json. A second copy of a fact drifting from its
# source is this project's most expensive recurring bug, so something has to
# compare the four.
#
# WHY IT IS NOT A check() ITEM, AND MUST NOT BECOME ONE: check()'s exit code is
# what halts /intake in a generated install - templates/intake.md reads it and
# stops on a failure. A README row that no longer matches the agent files costs
# a reader a stale document; it does not stop one agent from working. Halting
# somebody's /intake over a drifted doc would be the same over-reach that
# deriving `installed` from a growing hook list already cost a P0 to unlearn.
# So the two are kept visibly apart: different functions, different result
# shapes, different renderers, different subcommands. Nothing below is called
# from diagnose(), nothing below appears in its `checks` list, and nothing below
# can move its exit code or its `state:` line.
#
# Every item here degrades to "not verified" (SKIP) rather than guessing. The
# one answer that must never be produced from a file this could not read is
# "consistent".
# --------------------------------------------------------------------------- #

ROSTER_FIX = "/teamme:modify-team"

# Concluded statuses. THE list lives in worklog.py; this reads it out of the
# sibling script's source rather than keeping a second copy, because a second
# copy is precisely the drift this subcommand exists to catch. Parsed, never
# imported - a health check must not execute a script it found on disk. The
# literal below is reached only when the sibling cannot be read, and the check
# that falls back says so in its own detail line rather than staying quiet.
CLOSED_STATUSES_FALLBACK = ("done", "declined", "dropped")


def _roster_item(cid, state, detail, fix="") -> dict:
    """One roster finding. `state` is 'pass', 'fail' or 'skip'; only 'pass'
    counts towards exit 0, so a 'not verified' is never read as agreement."""
    state = state if state in ("pass", "fail", "skip") else "skip"
    return {
        "id": cid,
        "state": state,
        "ok": state == "pass",
        "detail": detail,
        "fix": fix if state == "fail" else "",
    }


def _q(items) -> str:
    return ", ".join("`%s`" % i for i in items)


def _frontmatter_name(path) -> str:
    """The `name:` an agent file declares, or "" for anything else. Never raises.

    This is what makes "which files in .claude/agents/ are agents" a property of
    the files themselves rather than a filename convention: README.md and any
    other prose dropped in that directory simply have no frontmatter name.
    """
    try:
        lines = pathlib.Path(path).read_text(errors="replace").splitlines()
    except Exception:
        return ""
    if not lines or lines[0].strip() != "---":
        return ""
    for line in lines[1:]:
        if line.strip() in ("---", "..."):
            break
        m = re.match(r"\s*name\s*:\s*(.+?)\s*$", line)
        if m:
            return m.group(1).strip().strip("'\"").strip()
    return ""


def _agent_roster(root: pathlib.Path):
    """(names, ignored_files, problem). `problem` non-empty means the roster
    could not be established at all, and every check degrades to not-verified.

    No assumption about how the agents are named: they are whatever the files in
    this project's .claude/agents/ declare themselves to be.
    """
    d = root / ".claude" / "agents"
    try:
        if not d.is_dir():
            return [], [], "there is no .claude/agents/ directory in this project"
        files = sorted(p for p in d.iterdir() if p.is_file() and p.suffix.lower() == ".md")
    except Exception as exc:
        return [], [], f"could not read .claude/agents/ ({exc})"
    names, ignored = [], []
    for p in files:
        if p.name.lower() == "readme.md":
            continue  # the roster document, not a roster member
        n = _frontmatter_name(p)
        if not n:
            ignored.append(p.name)  # no frontmatter name: not an agent this can name
        elif n not in names:
            names.append(n)
    if not names:
        return [], ignored, "no agent files were found in .claude/agents/"
    return sorted(names), ignored, ""


# --- the smallest markdown table reader that can answer "which rows" ---------

def _md_cells(line) -> list:
    s = str(line).strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def _is_delimiter_row(line) -> bool:
    s = str(line).strip()
    if not s.startswith("|"):
        return False
    cells = _md_cells(line)
    return bool(cells) and all(re.fullmatch(r":?-+:?", c or "") for c in cells)


def _md_tables(text) -> list:
    """Every pipe table's body rows, as lists of cells. Header and delimiter
    rows are dropped, so a header cell can never be mistaken for a roster row."""
    lines = str(text).splitlines()
    out, i = [], 0
    while i < len(lines):
        if _is_delimiter_row(lines[i]) and i > 0 and lines[i - 1].strip().startswith("|"):
            body, j = [], i + 1
            while j < len(lines) and lines[j].strip().startswith("|"):
                if not _is_delimiter_row(lines[j]):
                    body.append(_md_cells(lines[j]))
                j += 1
            out.append(body)
            i = j
        else:
            i += 1
    return out


def _plain(cell) -> str:
    """A cell as bare text: `x`, **x** and [x](y) all reduce to x."""
    s = str(cell or "").strip()
    m = re.match(r"^\[(.+?)\]\([^)]*\)$", s)
    if m:
        s = m.group(1)
    for _ in range(3):
        s = s.strip().strip("`").strip("*")
    return s.strip()


def _names_in_text(text, name) -> bool:
    """Is this agent named in this text, as a whole word? Substring matching
    would let `foo` be satisfied by a mention of `foo-bar`, so the match is
    fenced by characters that can appear in an agent name."""
    try:
        return bool(re.search(r"(?<![A-Za-z0-9_-])" + re.escape(name) + r"(?![A-Za-z0-9_-])", text))
    except Exception:
        return False


# --- the three checks -------------------------------------------------------

def _roster_check_readme(root, names, ignored) -> dict:
    """agent files <-> the roster table, BOTH directions."""
    path = root / ".claude" / "agents" / "README.md"
    try:
        if not path.is_file():
            return _roster_item(
                "roster_readme", "skip",
                "not verified: .claude/agents/README.md is missing, so there is no roster table "
                "to compare the agent files against",
            )
        text = path.read_text(errors="replace")
    except Exception as exc:
        return _roster_item(
            "roster_readme", "skip",
            f"not verified: could not read .claude/agents/README.md ({exc})",
        )

    known = set(names)
    # The roster table is the one whose first column names the most agents. A
    # README may hold several tables, and guessing by position would be a
    # confident wrong answer the first time somebody adds one above it.
    best, hits = None, 0
    for body in _md_tables(text):
        firsts = [_plain(r[0]) for r in body if r]
        n = sum(1 for f in firsts if f in known)
        if n > hits:
            best, hits = firsts, n
    if not best:
        return _roster_item(
            "roster_readme", "skip",
            f"not verified: no table in .claude/agents/README.md names any of the {len(names)} "
            "agent files, so its roster rows could not be identified",
        )

    rows, seen = [], set()
    for f in best:
        if f and f not in seen:
            seen.add(f)
            rows.append(f)
    missing = [n for n in names if n not in seen]
    extra = [f for f in rows if f not in known]

    note = f" (ignored: {_q(ignored)} - no `name:` in the frontmatter)" if ignored else ""
    if missing or extra:
        parts = []
        if missing:
            parts.append("no row for " + _q(missing))
        if extra:
            parts.append("rows naming no agent file: " + _q(extra))
        return _roster_item(
            "roster_readme", "fail",
            ".claude/agents/README.md roster table: " + "; ".join(parts) + note,
            f"{ROSTER_FIX}, or bring the table in .claude/agents/README.md back in line by hand",
        )
    return _roster_item("roster_readme", "pass",
                        f"{len(names)} agents, {len(rows)} README rows" + note)


def _roster_check_command(root, names, ignored) -> dict:
    """agent files -> the generated intake command.

    Deliberately loose, and said out loud rather than dressed up: intake.md is
    prose written for one project, so the only claim worth making about it is
    whether the agent is named in the file AT ALL. A name that appears nowhere
    cannot be being dispatched by it; a name that appears somewhere is only
    evidence that it has not been forgotten, not proof the lane table is right.
    """
    path = root / ".claude" / "commands" / "intake.md"
    try:
        if not path.is_file():
            return _roster_item(
                "roster_command", "skip",
                "not verified: .claude/commands/intake.md is missing, so there is no lane table "
                "to compare the agent files against",
            )
        text = path.read_text(errors="replace")
    except Exception as exc:
        return _roster_item(
            "roster_command", "skip",
            f"not verified: could not read .claude/commands/intake.md ({exc})",
        )
    missing = [n for n in names if not _names_in_text(text, n)]
    if missing:
        return _roster_item(
            "roster_command", "fail",
            "intake.md lane table: no row for " + _q(missing)
            + " (the name appears nowhere in .claude/commands/intake.md)",
            f"{ROSTER_FIX}, or add the row by hand",
        )
    return _roster_item("roster_command", "pass", f"intake.md names all {len(names)} agents")


def _closed_statuses(root):
    """(statuses, source) from worklog.py's own source. source "" = fell back."""
    for p in (
        root / ".claude" / "hooks" / "worklog.py",
        pathlib.Path(__file__).resolve().parent / "worklog.py",
    ):
        try:
            if not p.is_file():
                continue
            m = re.search(r"^CLOSED_STATUSES\s*=\s*\(([^)]*)\)",
                          p.read_text(errors="replace"), re.M)
            if not m:
                continue
            found = [s.strip().strip("'\"").strip() for s in m.group(1).split(",")]
            found = [s for s in found if s]
            if found:
                return tuple(found), str(p)
        except Exception:
            continue
    return CLOSED_STATUSES_FALLBACK, ""


def _roster_check_tasks(root, names, ignored) -> dict:
    """open tasks -> live agents.

    A CLOSED task is history and is never a failure here: `teamme-foo` really
    did do that work, and an append-only ledger is editable about current scope,
    not about what happened. A task with no lane is a PASS too - `lane` is
    optional, and an unassigned task is not a drifted one.
    """
    path = root / ".claude" / "intake" / "worklog.json"
    try:
        if not path.is_file():
            return _roster_item(
                "roster_tasks", "skip",
                "not verified: .claude/intake/worklog.json is missing, so no task lanes could "
                "be read",
            )
        data = json.loads(path.read_text(errors="replace"))
    except Exception as exc:
        return _roster_item(
            "roster_tasks", "skip",
            f"not verified: could not read .claude/intake/worklog.json ({exc}) - its lanes were "
            "not compared against the agent files",
        )
    tasks = data.get("tasks") if isinstance(data, dict) else None
    if not isinstance(tasks, list):
        return _roster_item(
            "roster_tasks", "skip",
            "not verified: .claude/intake/worklog.json has no `tasks` list, so no task lanes "
            "could be read",
        )

    closed, source = _closed_statuses(root)
    fallback_note = "" if source else (
        " (worklog.py's own closed-status list was not readable from here, so the built-in "
        f"default was used: {', '.join(CLOSED_STATUSES_FALLBACK)})"
    )

    open_laned, laneless, bad = [], 0, []
    for t in tasks:
        if not isinstance(t, dict):
            continue
        if str(t.get("status") or "") in closed:
            continue  # history, and exempt on purpose
        lane = str(t.get("lane") or "").strip()
        if not lane:
            laneless += 1
            continue
        open_laned.append(t)
        if lane not in names:
            bad.append(f"{t.get('id') or '?'} `{lane}`")

    total = len(open_laned)
    tail = f", {laneless} with no lane" if laneless else ""
    if bad:
        return _roster_item(
            "roster_tasks", "fail",
            f"open tasks: {total - len(bad)}/{total} name a live lane{tail} - no agent file for "
            + ", ".join(bad) + fallback_note,
            f"{ROSTER_FIX}, or `python3 .claude/hooks/worklog.py lane <id> <agent>` to repoint "
            "each one - closed tasks are history and are left alone",
        )
    return _roster_item("roster_tasks", "pass",
                        f"open tasks: {total}/{total} name a live lane{tail}" + fallback_note)


def roster(project_dir=None) -> dict:
    """The roster verdict as plain data. Never raises, and never reports
    agreement it did not actually observe."""
    try:
        root = project_root(project_dir)
    except Exception as exc:  # pragma: no cover - only a pathological cwd
        return {
            "ok": False,
            "project_dir": str(project_dir or ""),
            "checks": [_roster_item("roster", "skip",
                                    f"not verified: cannot resolve the project directory ({exc})")],
            "summary": "could not resolve the project directory",
        }
    try:
        names, ignored, problem = _agent_roster(root)
    except Exception as exc:
        names, ignored, problem = [], [], f"could not read .claude/agents/ ({exc})"

    checks = []
    for cid, fn in (
        ("roster_readme", _roster_check_readme),
        ("roster_command", _roster_check_command),
        ("roster_tasks", _roster_check_tasks),
    ):
        if problem:
            checks.append(_roster_item(cid, "skip", f"not verified: {problem}"))
            continue
        try:
            checks.append(fn(root, names, ignored))
        except Exception as exc:
            checks.append(_roster_item(cid, "skip", f"not verified: the check itself failed ({exc})"))

    failed = [c for c in checks if c["state"] == "fail"]
    skipped = [c for c in checks if c["state"] == "skip"]
    if failed:
        summary = f"the roster has drifted - {len(failed)} of {len(checks)} checks failed"
    elif skipped:
        summary = f"the roster could not be fully verified - {len(skipped)} of {len(checks)} checks were not run"
    else:
        summary = "the agent files, the roster README, the intake command and the work log agree"
    return {
        "ok": not failed and not skipped,
        "project_dir": str(root),
        "checks": checks,
        "summary": summary,
    }


def render_roster(result: dict) -> list:
    """Human-readable lines for a roster verdict. Deliberately not render():
    the two verdicts must never be mistaken for one another on screen."""
    lines = ["roster consistency"]
    for c in result.get("checks") or []:
        mark = {"pass": "PASS", "fail": "FAIL"}.get(c.get("state"), "SKIP")
        lines.append(f"  {mark}  {c.get('detail', '')}")
        if c.get("fix"):
            lines.append(f"        fix: {c['fix']}")
    return lines


def _arg_value(argv, flag):
    if flag in argv:
        i = argv.index(flag)
        if i + 1 < len(argv):
            return argv[i + 1]
    return None


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    mode = argv[0] if argv and not argv[0].startswith("-") else "check"

    if mode == "heartbeat":
        drain_stdin()  # bounded: this mode can never wait on its caller's stdin
        write_heartbeat()
        return 0  # ALWAYS. A session start is never blocked by this script.

    if mode in ("check", "status"):
        want_json = "--json" in argv
        result = diagnose(_arg_value(argv, "--project-dir"))
        if want_json:
            print(json.dumps(result, indent=2))
        else:
            print("\n".join(render(result)))
        return 0 if result.get("ok") else 1

    if mode == "roster":
        # A SEPARATE code path on purpose: this never touches diagnose(), its
        # exit code or its `state:` line. A drifted roster is a stale document,
        # not a broken install, and must not halt anybody's /intake.
        want_json = "--json" in argv
        result = roster(_arg_value(argv, "--project-dir"))
        if want_json:
            print(json.dumps(result, indent=2))
        else:
            print("\n".join(render_roster(result)))
        return 0 if result.get("ok") else 1

    print(f"usage: {pathlib.Path(__file__).name} check [--json] [--project-dir DIR] | "
          f"roster [--json] [--project-dir DIR] | heartbeat", file=sys.stderr)
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
