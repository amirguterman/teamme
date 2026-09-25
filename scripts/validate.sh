#!/usr/bin/env bash
# Validate the teamme plugin: manifests, hook syntax, and an end-to-end smoke test
# of the scaffolding in a throwaway project. Run it locally the same way CI does.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$PWD"
fail() { echo "FAIL: $*" >&2; exit 1; }

echo "== manifests =="
python3 - <<'PY'
import json, pathlib, sys
root = pathlib.Path.cwd()

# A plugin-shipped agent must NOT set these: the installed CLI silently
# ignores them for plugin agents, so setting one is a bug that would
# otherwise ship invisibly - this check exists to catch that, not just to
# confirm the block parses.
FORBIDDEN_AGENT_KEYS = ("permissionMode", "hooks", "mcpServers")


def parse_frontmatter(text: str):
    """A minimal frontmatter parser for this repo's shape: a CLOSED '---'
    block of flat 'key: scalar' lines, where a scalar may be a double-quoted
    YAML string (decoded like JSON, so \\n etc. resolve) or a bare/single-
    quoted one. Not a general YAML parser - there is no YAML library here by
    design (stdlib only, see CLAUDE.md) - but real enough to catch an
    unclosed block or an unparseable value, which "startswith('---')" alone
    never could. Returns (dict, None) or (None, "why").
    """
    if not (text.startswith("---\n") or text.startswith("---\r\n")):
        return None, "does not start with a '---' frontmatter marker"
    lines = text.splitlines()
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return None, "frontmatter block is never closed with a second '---' line"
    out, key = {}, None
    for raw in lines[1:end]:
        if not raw.strip():
            continue
        if raw[:1] not in (" ", "\t") and ":" in raw:
            k, _, v = raw.partition(":")
            key = k.strip()
            v = v.strip()
            if v.startswith('"'):
                try:
                    v = json.loads(v)
                except Exception as exc:
                    return None, f"key '{key}': could not parse quoted value ({exc})"
            elif v.startswith("'") and v.endswith("'") and len(v) >= 2:
                v = v[1:-1].replace("''", "'")
            out[key] = v
        elif key is not None:
            out[key] = (out[key] + " " + raw.strip()).strip()
        else:
            return None, f"line outside any key: {raw!r}"
    return out, None


m = json.loads((root / ".claude-plugin/marketplace.json").read_text())
entries = m.get("plugins") or []
if not entries:
    sys.exit("marketplace.json lists no plugins")
for e in entries:
    src = e.get("source")
    if not isinstance(src, str) or not src.startswith("./"):
        sys.exit(f"{e.get('name')}: source must be a relative path in this repo")
    pdir = root / src
    manifest = pdir / ".claude-plugin/plugin.json"
    if not manifest.is_file():
        sys.exit(f"{e.get('name')}: missing {manifest.relative_to(root)}")
    p = json.loads(manifest.read_text())
    if p.get("name") != e.get("name"):
        sys.exit(f"name mismatch: marketplace '{e.get('name')}' vs plugin '{p.get('name')}'")
    for key in ("name", "description", "author"):
        if not p.get(key):
            sys.exit(f"{p.get('name')}: plugin.json is missing '{key}'")

    cmds = sorted((pdir / "commands").glob("*.md"))
    if not cmds:
        sys.exit(f"{p['name']}: no commands/*.md")
    for c in cmds:
        fm, err = parse_frontmatter(c.read_text())
        if err:
            sys.exit(f"{c.relative_to(root)}: {err}")
        if not fm.get("description"):
            sys.exit(f"{c.relative_to(root)}: frontmatter has no 'description'")

    agent_dir = pdir / "agents"
    agents = sorted(agent_dir.glob("*.md")) if agent_dir.is_dir() else []

    # `plugins/*/server/*.py` (the old glob) only ever reached the top-level
    # server file and silently never checked server/librarian/*.py - the same
    # shape as REQUIRED_HOOKS quietly covering less than its author assumed.
    # commands/*.md-only frontmatter checking repeated that exact mistake:
    # agents/ shipped and nothing validated it. Rather than add a second,
    # equally narrow glob for agents/ next to it, walk the WHOLE plugin tree
    # for *.md and require every file found to be accounted for by a
    # directory this check already knows how to validate (or explicitly
    # excluded). A third prompt directory added later fails this loudly
    # instead of silently passing unchecked.
    prompt_files = sorted(
        f for f in pdir.rglob("*.md")
        if f.name != "README.md" and "templates" not in f.relative_to(pdir).parts
    )
    accounted = set(cmds) | set(agents)
    unaccounted = sorted(str(f.relative_to(root)) for f in set(prompt_files) - accounted)
    if unaccounted:
        sys.exit(
            f"{p['name']}: found *.md file(s) this manifests check does not know how to "
            f"validate: {unaccounted} - extend the check, don't let it pass silently"
        )

    for a in agents:
        fm, err = parse_frontmatter(a.read_text())
        if err:
            sys.exit(f"{a.relative_to(root)}: {err}")
        for req in ("name", "description"):
            if not fm.get(req):
                sys.exit(f"{a.relative_to(root)}: frontmatter is missing '{req}'")
        present = [k for k in FORBIDDEN_AGENT_KEYS if k in fm]
        if present:
            sys.exit(
                f"{a.relative_to(root)}: frontmatter sets {present} - the installed CLI "
                f"drops these for plugin-shipped agents (it warns, but nothing here catches that), so this must never ship"
            )

    print(f"  ok: {p['name']} v{p.get('version','?')} - {len(cmds)} command(s), {len(agents)} agent(s)")
PY

echo "== hook syntax =="
python3 -m py_compile plugins/*/templates/hooks/*.py
echo "  ok: all hooks compile"

echo "== mcp server syntax =="
# `plugins/*/server/*.py` (the old form) only ever expands to the top-level
# server file - it silently never reaches plugins/*/server/librarian/*.py or any
# other subpackage. That is the same failure shape as T23's REQUIRED_HOOKS: a
# list/glob that quietly covers less than its author assumed. `find -path` walks
# the whole server/ tree regardless of depth, so a new subdirectory cannot be
# missed the same way again.
mapfile -t SERVER_PYFILES < <(find plugins -path '*/server/*' -name '*.py' | sort)
[ "${#SERVER_PYFILES[@]}" -gt 0 ] || fail "no plugins/*/server/**/*.py files found to compile - has the server/ layout moved?"
python3 -m py_compile "${SERVER_PYFILES[@]}"
echo "  ok: the mcp server and its submodules compile (${#SERVER_PYFILES[@]} file(s), including server/librarian/*.py)"

echo "== mcp manifest =="
python3 -c "
import json, glob, sys
found = glob.glob('plugins/*/.mcp.json')
if not found:
    sys.exit('no plugins/*/.mcp.json found')
for f in found:
    d = json.load(open(f))
    servers = d.get('mcpServers') or {}
    if not servers:
        sys.exit(f'{f}: no mcpServers entries')
    for name, spec in servers.items():
        if not isinstance(spec.get('command'), str) or not spec['command'].strip():
            sys.exit(f'{f}: {name} has no command')
        args = spec.get('args')
        if not isinstance(args, list) or not args:
            sys.exit(f'{f}: {name} has no args')
    print(f'  ok: {f} -', ', '.join(sorted(servers)))
"

echo "== settings template =="
python3 -c "
import json,glob,sys
for f in glob.glob('plugins/*/templates/settings.hooks.json'):
    h=json.load(open(f))['hooks']
    need={'UserPromptSubmit','PreToolUse','SessionStart','Stop'}
    missing=need-set(h)
    if missing: sys.exit(f'{f}: missing hook events {sorted(missing)}')
    print('  ok: events', sorted(h))
"

echo "== smoke test in a throwaway project =="
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
mkdir -p "$T/.claude/hooks" "$T/src"
cp plugins/*/templates/hooks/*.py "$T/.claude/hooks/"
cd "$T"; export CLAUDE_PROJECT_DIR="$T"
H=.claude/hooks
denied() {
  echo '{"tool_name":"Edit","tool_input":{"file_path":"'"$T"'/src/a.txt"}}' \
    | python3 $H/intake-guard.py | grep -q deny
}
denied && fail "guard denied with no intake active (must fail open)"
test -f "$H/preflight.py" || fail "preflight.py was not part of the copied scaffolding"
denied && fail "guard denied with the new preflight.py scaffolding present and no intake active"
python3 $H/worklog.py add "smoke task" --priority P1 >/dev/null
python3 $H/intake-state.py begin T1 >/dev/null
denied || fail "guard did not deny during grounding"
python3 $H/intake-state.py approve >/dev/null
denied && fail "guard still denied after approval"
python3 $H/worklog.py start T1 >/dev/null
echo '{}' | python3 $H/worklog-enforce.py stop | grep -q '"block"' || fail "Stop did not block on an active task"
echo '{}' | python3 $H/worklog-enforce.py stop | grep -q '"block"' && fail "Stop nagged twice (loop risk)"
python3 $H/worklog.py done T1 >/dev/null
python3 $H/intake-state.py release >/dev/null
echo 'garbage' | python3 $H/intake-guard.py | grep -q deny && fail "guard denied on malformed input"
echo "  ok: fail-open, deny-during-grounding, lift-on-approve, Stop fires once"

WLJSON="$PWD/.claude/intake/worklog.json"

echo "== worklog: N concurrent notes on one task all survive (the lockfile earns its keep) =="
LOCK_TASK=$(python3 $H/worklog.py add "lock stress task" --priority P2 | grep -oE 'T[0-9]+' | head -1)
[ -n "$LOCK_TASK" ] || fail "could not create the lock stress task"
for i in $(seq 1 10); do
  python3 $H/worklog.py note "$LOCK_TASK" "note-$i" >/dev/null &
done
wait
NOTE_COUNT=$(python3 -c "
import json
d = json.load(open('$WLJSON'))
t = next(x for x in d['tasks'] if x['id'] == '$LOCK_TASK')
print(len(t['notes']))
")
[ "$NOTE_COUNT" = "10" ] || fail "expected 10 notes after 10 concurrent writers, got $NOTE_COUNT (lost notes mean the lock regressed)"
echo "  ok: 10/10 concurrent notes survived"

echo "== worklog: a stale lockfile is broken rather than wedging the write, and leaves nothing behind =="
LOCKFILE="$(dirname "$WLJSON")/worklog.lock"
echo "999999 0" > "$LOCKFILE"
touch -d "-60 seconds" "$LOCKFILE"
STALE_TASK=$(python3 $H/worklog.py add "stale lock task" --priority P2 | grep -oE 'T[0-9]+' | head -1)
[ -n "$STALE_TASK" ] || fail "add did not succeed with a stale (crashed-process) lockfile present"
test -f "$LOCKFILE" && fail "the stale lockfile from a crashed process was never cleaned up (a live lock would still be here, but this one is 60s old)"
LEFTOVER=$(find "$(dirname "$WLJSON")" -maxdepth 1 \( -name '*.lock' -o -name '.*.tmp' \) 2>/dev/null)
[ -z "$LEFTOVER" ] || fail "leftover lock/tmp file(s) after a write: $LEFTOVER"
echo "  ok: stale lock broken, write landed, no lock/tmp debris"

echo "== worklog: status_changed - a note answers the Stop nag without re-arming it, a real transition does =="
SC_TASK=$(python3 $H/worklog.py add "status changed task" --priority P1 | grep -oE 'T[0-9]+' | head -1)
python3 $H/worklog.py start "$SC_TASK" >/dev/null
echo '{}' | python3 $H/worklog-enforce.py stop | grep -q '"block"' \
  || fail "Stop did not block on a freshly-active task"
echo '{}' | python3 $H/worklog-enforce.py stop | grep -q '"block"' \
  && fail "Stop nagged twice in a row with nothing changed (loop risk)"
sleep 1  # force `updated` (bumped by note) to differ, at second resolution, from status_changed/nagged_at
python3 $H/worklog.py note "$SC_TASK" "still working on it" >/dev/null
NOTE_STOP=$(echo '{}' | python3 $H/worklog-enforce.py stop)
[ -z "$NOTE_STOP" ] || fail "a note re-armed the Stop nag - this is the exact bug T10 fixed: $NOTE_STOP"
sleep 1  # force the next status_changed stamp (second-resolution) to differ from the armed one
python3 $H/worklog.py start "$SC_TASK" >/dev/null   # a real status transition, even to the same status
echo '{}' | python3 $H/worklog-enforce.py stop | grep -q '"block"' \
  || fail "a real status transition after the note did not re-arm the Stop nag (overcorrection: nag stopped enforcing)"
echo "  ok: note leaves the nag armed, a real transition re-arms it"

echo "== worklog: a pre-migration ledger with no status_changed loads, lists and does not spuriously re-nag =="
mkdir -p compat-proj/.claude/hooks compat-proj/.claude/intake
cp "$H"/*.py compat-proj/.claude/hooks/
python3 -c "
import json, pathlib
d = {
    'version': 1, 'next_id': 2,
    'tasks': [{
        'id': 'T1', 'title': 'pre-migration task', 'status': 'active',
        'priority': 'P1', 'lane': '', 'notes': [], 'blocked_on': '', 'dispatched_to': '',
        'created': '2024-01-01T00:00:00+00:00', 'updated': '2024-01-01T00:00:00+00:00',
        'nagged_at': '2024-01-01T00:00:00+00:00',
    }],
}
pathlib.Path('compat-proj/.claude/intake/worklog.json').write_text(json.dumps(d, indent=2) + chr(10))
"
COMPAT_LIST=$(CLAUDE_PROJECT_DIR="$PWD/compat-proj" python3 compat-proj/.claude/hooks/worklog.py list) \
  || fail "worklog.py list crashed on a pre-migration ledger with no status_changed"
echo "$COMPAT_LIST" | grep -q "T1" || fail "pre-migration task did not appear in list: $COMPAT_LIST"
COMPAT_STOP=$(echo '{}' | CLAUDE_PROJECT_DIR="$PWD/compat-proj" python3 compat-proj/.claude/hooks/worklog-enforce.py stop)
[ -z "$COMPAT_STOP" ] || fail "a pre-migration task already nagged (nagged_at == updated) spuriously re-fired after status_changed defaulted to updated: $COMPAT_STOP"
echo "  ok: pre-migration ledger loads cleanly, status_changed defaults to updated, no spurious re-nag"

echo "== worklog: dispatched tasks are unfinished-but-unnagged, and get their own SessionStart wording =="
DISPATCH_TASK=$(python3 $H/worklog.py add "dispatch test task" --priority P1 | grep -oE 'T[0-9]+' | head -1)
python3 $H/worklog.py dispatch "$DISPATCH_TASK" "teamme-hook-engineer" >/dev/null
DISPATCH_STOP=$(echo '{}' | python3 $H/worklog-enforce.py stop)
[ -z "$DISPATCH_STOP" ] || fail "Stop produced output while only a dispatched task was pending: $DISPATCH_STOP"
python3 $H/worklog.py list | grep -q "\[@\] $DISPATCH_TASK" || fail "dispatched task missing its [@] mark in list"
python3 $H/worklog.py stats | grep -qE "unfinished: [1-9]" || fail "dispatched task was not counted as unfinished in stats"
SESSION_OUT=$(echo '{}' | python3 $H/worklog-enforce.py session)
echo "$SESSION_OUT" | grep -q "$DISPATCH_TASK" || fail "SessionStart did not mention the dispatched task"
echo "$SESSION_OUT" | grep -q "in flight with another agent" || fail "SessionStart did not use the dispatched-specific wording, distinct from blocked"
echo "$SESSION_OUT" | grep -q "blocked and need an input" && fail "SessionStart used blocked wording for a dispatched-only task"
echo "  ok: dispatched is unfinished, unnagged, listed, and worded distinctly at SessionStart"

echo "== mcp server: teamme_worklog/teamme_intake_phase are gated on teamme_install =="
mkdir -p gate-proj
python3 - "$ROOT" "$PWD/gate-proj" <<'PY'
import json, pathlib, subprocess, sys

root = pathlib.Path(sys.argv[1])
gate_proj = pathlib.Path(sys.argv[2])
server = root / "plugins/teamme/server/teamme_mcp.py"


def send(proc, obj):
    proc.stdin.write(json.dumps(obj) + "\n")
    proc.stdin.flush()


def recv(proc):
    line = proc.stdout.readline()
    if not line:
        sys.exit(f"server closed the pipe unexpectedly; stderr: {proc.stderr.read()}")
    return json.loads(line)


def call_text(resp):
    result = resp.get("result") or {}
    return result, "".join(c.get("text", "") for c in result.get("content") or [])


proc = subprocess.Popen(
    ["python3", str(server)],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    text=True, bufsize=1,
)
try:
    send(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18"}})
    resp = recv(proc)
    if resp.get("result", {}).get("serverInfo", {}).get("name") != "teamme":
        sys.exit(f"initialize did not report the teamme server: {resp}")

    # gated tool call against an unscaffolded project must refuse, naming teamme_install
    send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "teamme_worklog",
                           "arguments": {"action": "list", "project_dir": str(gate_proj)}}})
    result, text = call_text(recv(proc))
    if not result.get("isError"):
        sys.exit(f"teamme_worklog was not gated against an unscaffolded project: {result}")
    if "teamme_install" not in text:
        sys.exit(f"the refusal did not name teamme_install: {text!r}")

    # teamme_install scaffolds the project
    send(proc, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                "params": {"name": "teamme_install",
                           "arguments": {"project_dir": str(gate_proj)}}})
    result, text = call_text(recv(proc))
    if result.get("isError"):
        sys.exit(f"teamme_install reported an error: {text}")
    if not (gate_proj / ".claude" / "hooks" / "worklog.py").is_file():
        sys.exit("teamme_install did not scaffold .claude/hooks/worklog.py")

    # the same gated call now succeeds
    send(proc, {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                "params": {"name": "teamme_worklog",
                           "arguments": {"action": "list", "project_dir": str(gate_proj)}}})
    result, text = call_text(recv(proc))
    if result.get("isError"):
        sys.exit(f"teamme_worklog is still gated after teamme_install: {text}")
finally:
    try:
        proc.stdin.close()
    except Exception:
        pass
    proc.wait(timeout=5)

print("  ok: teamme_worklog refuses (naming teamme_install) before install, succeeds after")
PY

echo "== mcp server: serverInfo.version always matches plugin.json, read live - the T22 drift guard =="
python3 - "$ROOT" <<'PY'
import json, pathlib, subprocess, sys

root = pathlib.Path(sys.argv[1])
server = root / "plugins/teamme/server/teamme_mcp.py"
manifest_path = root / "plugins/teamme/.claude-plugin/plugin.json"
manifest_version = json.loads(manifest_path.read_text())["version"]


def send(proc, obj):
    proc.stdin.write(json.dumps(obj) + "\n")
    proc.stdin.flush()


def recv(proc):
    line = proc.stdout.readline()
    if not line:
        sys.exit(f"server closed the pipe unexpectedly; stderr: {proc.stderr.read()}")
    return json.loads(line)


proc = subprocess.Popen(
    ["python3", str(server)],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    text=True, bufsize=1,
)
try:
    send(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18"}})
    resp = recv(proc)
    reported = resp.get("result", {}).get("serverInfo", {}).get("version")
    # Deliberately NOT a literal like "0.3.0" here: a literal is the second copy
    # T22 deleted, and would drift (and fail spuriously) on the next version bump.
    # Parsed from plugin.json at test time, this assertion needs no maintenance.
    if reported != manifest_version:
        sys.exit(
            f"serverInfo.version ({reported!r}) does not match plugins/teamme/.claude-plugin/"
            f"plugin.json's version ({manifest_version!r}) - SERVER_VERSION has drifted from the "
            f"manifest, exactly what T22 removed the hardcoded literal to prevent"
        )
finally:
    try:
        proc.stdin.close()
    except Exception:
        pass
    proc.wait(timeout=5)

print(f"  ok: serverInfo.version ({manifest_version}) matches plugin.json live, not a second hardcoded copy")
PY

echo "== mcp server: a damaged manifest (throwaway copy only) degrades to 0.0.0+unknown, never crashes =="
# cp -r the whole plugin into a scratch fixture and damage the COPY's manifest.
# Touching this repo's own plugin.json would be a trap for anyone running
# validate.sh on a dirty tree, and mutating the repo under test is a bug on its
# own regardless of cleanup - so the fixture lives under $T (already trapped).
FALLBACK_COPY="$PWD/mcp-version-fallback"
rm -rf "$FALLBACK_COPY"
mkdir -p "$FALLBACK_COPY"
cp -r "$ROOT/plugins/teamme" "$FALLBACK_COPY/teamme"
rm -f "$FALLBACK_COPY/teamme/.claude-plugin/plugin.json"
python3 - "$FALLBACK_COPY/teamme" <<'PY'
import json, pathlib, subprocess, sys

plugin_dir = pathlib.Path(sys.argv[1])
server = plugin_dir / "server" / "teamme_mcp.py"


def send(proc, obj):
    proc.stdin.write(json.dumps(obj) + "\n")
    proc.stdin.flush()


def recv(proc):
    line = proc.stdout.readline()
    if not line:
        sys.exit(f"server closed the pipe unexpectedly; stderr: {proc.stderr.read()}")
    return json.loads(line)


proc = subprocess.Popen(
    ["python3", str(server)],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    text=True, bufsize=1,
)
try:
    send(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18"}})
    resp = recv(proc)
finally:
    try:
        proc.stdin.close()
    except Exception:
        pass
    rc = proc.wait(timeout=5)

stderr = proc.stderr.read()
server_info = (resp.get("result") or {}).get("serverInfo") or {}
version = server_info.get("version")
if version != "0.0.0+unknown":
    sys.exit(f"a missing plugin.json did not degrade to the unknown-version fallback: got {version!r}")
if server_info.get("name") != "teamme":
    sys.exit(f"initialize with a damaged manifest lost serverInfo.name entirely: {server_info}")
if rc != 0:
    sys.exit(f"the server did not exit 0 with a damaged (deleted) manifest: exit={rc}")
if stderr.strip():
    sys.exit(f"the server wrote to stderr with a damaged manifest: {stderr!r}")

print("  ok: a deleted plugin.json on a throwaway copy degrades to 0.0.0+unknown, exit 0, no stderr - "
      "the real plugin.json was never touched")
PY

echo "== preflight: not-installed, a live baseline, and each check fails independently =="
PF="$ROOT/plugins/teamme/templates/hooks/preflight.py"
mkdir -p pf-empty pf-full/.claude/hooks pf-full/.claude/intake pf-full/.claude/commands
cp "$H"/*.py pf-full/.claude/hooks/
python3 -c "
import json, pathlib
tpl = json.loads(pathlib.Path('$ROOT/plugins/teamme/templates/settings.hooks.json').read_text())
pathlib.Path('pf-full/.claude/settings.json').write_text(json.dumps(tpl, indent=2) + chr(10))
pathlib.Path('pf-full/.claude/commands/intake.md').write_text('---\ndescription: x\n---\nbody\n')
"
NOTLIVE_JSON_FILE="$PWD/pf-full-notlive.json"
set +e
python3 "$PF" check --json --project-dir "$PWD/pf-full" > "$NOTLIVE_JSON_FILE"
set -e
NOTLIVE_STATE=$(python3 -c "import json; print(json.load(open('$NOTLIVE_JSON_FILE'))['state'])")
[ "$NOTLIVE_STATE" = "installed-not-live" ] || fail "full scaffolding with no heartbeat reported state '$NOTLIVE_STATE', expected installed-not-live"
CLAUDE_PROJECT_DIR="$PWD/pf-full" python3 "$PF" heartbeat </dev/null >/dev/null
python3 - "$PF" "$PWD/pf-empty" "$PWD/pf-full" <<'PY'
import json, pathlib, shutil, subprocess, sys

pf, empty_dir, full_dir = sys.argv[1], pathlib.Path(sys.argv[2]), pathlib.Path(sys.argv[3])


def run_check(project_dir):
    proc = subprocess.run(
        ["python3", pf, "check", "--json", "--project-dir", str(project_dir)],
        capture_output=True, text=True, timeout=10,
    )
    try:
        data = json.loads(proc.stdout)
    except Exception as exc:
        sys.exit(f"preflight check --json produced unparseable output for {project_dir}: {exc}\n{proc.stdout}")
    return proc.returncode, data


def by_id(data, cid):
    for c in data.get("checks") or []:
        if c.get("id") == cid:
            return c
    sys.exit(f"no check with id {cid!r} in {data}")


# a completely empty project reports not-installed and a non-zero exit
rc, data = run_check(empty_dir)
if rc == 0:
    sys.exit("preflight check exited 0 for a completely unscaffolded project")
if data.get("state") != "not-installed":
    sys.exit(f"expected state not-installed for an empty project, got {data.get('state')!r}")
if data.get("ok"):
    sys.exit("preflight reported ok=true for an unscaffolded project")

# the fully-scaffolded, heartbeated baseline passes everything
rc, data = run_check(full_dir)
if rc != 0 or not data.get("ok"):
    sys.exit(f"the fully-scaffolded baseline did not pass: {json.dumps(data, indent=2)}")
if data.get("state") != "live":
    sys.exit(f"expected state live for the scaffolded+heartbeated baseline, got {data.get('state')!r}")


def broken_copy(name, mutate):
    dst = full_dir.parent / f"pf-{name}"
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(full_dir, dst)
    mutate(dst)
    rc, data = run_check(dst)
    if rc == 0:
        sys.exit(f"preflight exited 0 with {name} broken")
    if data.get("ok"):
        sys.exit(f"preflight reported ok=true with {name} broken")
    check = by_id(data, name)
    if check.get("ok"):
        sys.exit(f"check {name!r} still reports ok=true after breaking it: {check}")
    if not check.get("fix"):
        sys.exit(f"check {name!r} failed with no fix: line: {check}")
    return dst


def remove_hook(d):
    (d / ".claude" / "hooks" / "worklog.py").unlink()


def corrupt_settings(d):
    (d / ".claude" / "settings.json").write_text("{ not valid json")


def lock_intake_dir(d):
    (d / ".claude" / "intake").chmod(0o500)


broken_copy("hooks", remove_hook)
broken_copy("settings", corrupt_settings)
intake_dst = broken_copy("intake_dir", lock_intake_dir)
(intake_dst / ".claude" / "intake").chmod(0o700)  # so the temp-dir cleanup can remove it

print("  ok: not-installed on an empty project; hooks/settings/intake_dir each fail independently with a fix")
PY

echo "== preflight: a present-but-modified hook drives installed-outdated and names the file =="
STALE_FIXTURE="$PWD/pf-stale"
cp -r pf-full "$STALE_FIXTURE"
python3 -c "
import pathlib
p = pathlib.Path('$STALE_FIXTURE/.claude/hooks/worklog.py')
p.write_bytes(p.read_bytes() + b'\n# locally modified\n')
"
STALE_JSON_FILE="$PWD/pf-stale.json"
set +e
python3 "$PF" check --json --project-dir "$STALE_FIXTURE" > "$STALE_JSON_FILE"
STALE_RC=$?
set -e
[ "$STALE_RC" -ne 0 ] || fail "preflight exited 0 with a stale (present-but-modified) hook in place"
python3 -c "
import json
d = json.load(open('$STALE_JSON_FILE'))
if d.get('state') != 'installed-outdated':
    raise SystemExit(f\"expected installed-outdated for a stale hook, got {d.get('state')!r}: {d}\")
if d.get('ok'):
    raise SystemExit('preflight reported ok=true with a stale hook in place')
hooks_check = next(c for c in d['checks'] if c['id'] == 'hooks')
if hooks_check.get('ok'):
    raise SystemExit(f'the hooks check itself still reported ok=true: {hooks_check}')
if 'worklog.py' not in hooks_check.get('detail', ''):
    raise SystemExit(f'the stale filename was not named in the hooks check detail: {hooks_check}')
if not hooks_check.get('fix'):
    raise SystemExit(f'the stale hooks check failed with no fix line: {hooks_check}')
"
echo "  ok: a stale hook (not missing, just modified) drives installed-outdated, exit non-zero, names the file"

echo "== preflight: unreachable templates degrade freshness to existence-only and PASS, never fail =="
ISOLATED_DIR="$PWD/isolated-preflight"
mkdir -p "$ISOLATED_DIR"
cp "$PF" "$ISOLATED_DIR/preflight.py"
ISO_PF="$ISOLATED_DIR/preflight.py"
# preflight.py also resolves templates from the harness's own install record at
# $CLAUDE_CONFIG_DIR/plugins/installed_plugins.json (falling back to
# ~/.claude/...). Whether that record exists, and what it points at, depends on
# whoever's machine this runs on - a developer with a user-scope teamme install
# would legitimately resolve it, freshness WOULD be verified, and this "degraded"
# assertion would fail for reasons that have nothing to do with their change.
# Point both HOME and CLAUDE_CONFIG_DIR at an empty directory so degradation is
# forced by the fixture, not by an accident of whoever happens to run this.
ISOLATED_HOME="$PWD/isolated-home"
mkdir -p "$ISOLATED_HOME"
isolated_env() { env -u CLAUDE_PLUGIN_ROOT HOME="$ISOLATED_HOME" CLAUDE_CONFIG_DIR="$ISOLATED_HOME/.claude-config" "$@"; }
set +e
isolated_env python3 "$ISO_PF" check --project-dir "$STALE_FIXTURE" >/dev/null
ISO_RC=$?
set -e
[ "$ISO_RC" -eq 0 ] || fail "preflight exited $ISO_RC on a stale-but-otherwise-healthy install once its templates were unreachable (must degrade to existence-only and PASS)"
ISO_JSON_FILE="$PWD/pf-degraded.json"
isolated_env python3 "$ISO_PF" check --json --project-dir "$STALE_FIXTURE" > "$ISO_JSON_FILE"
python3 -c "
import json
d = json.load(open('$ISO_JSON_FILE'))
if not d.get('ok'):
    raise SystemExit(f'degraded run reported ok=false: {d}')
if d.get('state') != 'live':
    raise SystemExit(f\"expected state live once freshness could not be verified, got {d.get('state')!r}: {d}\")
hooks_check = next(c for c in d['checks'] if c['id'] == 'hooks')
if not hooks_check.get('ok'):
    raise SystemExit(f'the hooks check failed in degraded mode instead of degrading: {hooks_check}')
if 'not verified' not in hooks_check.get('detail', ''):
    raise SystemExit(f'the degraded hooks check did not say freshness was unverified: {hooks_check}')
if '/teamme:team-doctor' not in hooks_check.get('detail', ''):
    raise SystemExit(f'the degraded hooks check did not point at /teamme:team-doctor: {hooks_check}')
"
echo "  ok: templates unreachable (no install record either) -> freshness not verified, hooks check still PASSES, names /teamme:team-doctor"

echo "== preflight: a resolvable install record is what freshness checks against, and catches a stale hook through it =="
# A synthetic harness install record (user scope, no projectPath) pointing at
# this repo's own real templates/hooks - which DOES have a hook that differs
# from the fixture's deliberately-modified worklog.py. If this resolves through
# the record, the stale hook must be caught and named, and the detail must say
# so came from the record - not from env/self/hint, which are all unset/broken
# here on purpose (isolated copy, no CLAUDE_PLUGIN_ROOT).
RECORD_HOME="$PWD/record-home"
mkdir -p "$RECORD_HOME/.claude-config/plugins"
python3 -c "
import json, pathlib
data = {'plugins': {'teamme@teamme': [{
    'scope': 'user', 'installPath': '$ROOT/plugins/teamme',
    'version': '9.9.9', 'lastUpdated': '2026-01-01T00:00:00.000Z',
}]}}
pathlib.Path('$RECORD_HOME/.claude-config/plugins/installed_plugins.json').write_text(json.dumps(data))
"
record_env() { env -u CLAUDE_PLUGIN_ROOT HOME="$RECORD_HOME" CLAUDE_CONFIG_DIR="$RECORD_HOME/.claude-config" "$@"; }
RECORD_JSON_FILE="$PWD/pf-record.json"
set +e
record_env python3 "$ISO_PF" check --project-dir "$STALE_FIXTURE" >/dev/null
RECORD_RC=$?
record_env python3 "$ISO_PF" check --json --project-dir "$STALE_FIXTURE" > "$RECORD_JSON_FILE"
set -e
[ "$RECORD_RC" -ne 0 ] || fail "preflight exited 0 with a stale hook, even though a resolvable install record should have caught it"
python3 -c "
import json
d = json.load(open('$RECORD_JSON_FILE'))
if d.get('ok'):
    raise SystemExit(f'reported ok=true despite a stale hook visible through the install record: {d}')
hooks_check = next(c for c in d['checks'] if c['id'] == 'hooks')
if hooks_check.get('ok'):
    raise SystemExit(f'the hooks check reported ok=true despite the stale hook: {hooks_check}')
if 'worklog.py' not in hooks_check.get('detail', ''):
    raise SystemExit(f'the stale filename was not named: {hooks_check}')
if 'install record' not in hooks_check.get('detail', ''):
    raise SystemExit(f'the detail did not name the install-record source - this may have resolved some other way: {hooks_check}')
"
echo "  ok: a synthetic install record resolves the templates and catches the stale hook, naming the record as its source"

echo "== preflight: a corrupt or missing install record degrades rather than erroring =="
# missing: an empty home directory, no installed_plugins.json at all - already
# exercised above (ISOLATED_HOME), re-asserted here for the /teamme:team-doctor
# wording. corrupt: a record that exists but fails to parse as JSON - a
# different code path (json.loads raising) than a simply-absent file.
CORRUPT_HOME="$PWD/corrupt-home"
mkdir -p "$CORRUPT_HOME/.claude-config/plugins"
printf '{ not valid json' > "$CORRUPT_HOME/.claude-config/plugins/installed_plugins.json"
corrupt_env() { env -u CLAUDE_PLUGIN_ROOT HOME="$CORRUPT_HOME" CLAUDE_CONFIG_DIR="$CORRUPT_HOME/.claude-config" "$@"; }
CORRUPT_JSON_FILE="$PWD/pf-corrupt-record.json"
set +e
corrupt_env python3 "$ISO_PF" check --project-dir "$STALE_FIXTURE" >/dev/null
CORRUPT_RC=$?
set -e
[ "$CORRUPT_RC" -eq 0 ] || fail "preflight exited $CORRUPT_RC with an unparseable install record (must degrade, not error)"
corrupt_env python3 "$ISO_PF" check --json --project-dir "$STALE_FIXTURE" > "$CORRUPT_JSON_FILE"
python3 -c "
import json
d = json.load(open('$CORRUPT_JSON_FILE'))
if not d.get('ok'):
    raise SystemExit(f'a corrupt install record failed the check instead of degrading: {d}')
hooks_check = next(c for c in d['checks'] if c['id'] == 'hooks')
if not hooks_check.get('ok'):
    raise SystemExit(f'the hooks check failed on a corrupt record instead of degrading: {hooks_check}')
if 'not verified' not in hooks_check.get('detail', ''):
    raise SystemExit(f'a corrupt install record did not degrade to existence-only: {hooks_check}')
if '/teamme:team-doctor' not in hooks_check.get('detail', ''):
    raise SystemExit(f'the degraded detail did not point at /teamme:team-doctor: {hooks_check}')
"
echo "  ok: an unparseable install record degrades to existence-only (PASS), naming /teamme:team-doctor - same as a missing one"

echo "== preflight: a record whose installPath fails the marker check is rejected, never compared against =="
# The false positive that matters most: the resolver picking a path it should
# have rejected and reporting 'all matching' against the wrong templates. Point
# installPath at a real, existing directory that is NOT shaped like the
# plugin's templates/hooks (no settings.hooks.json marker, no preflight.py) -
# this repo's own root - and confirm it is rejected rather than trusted.
BADPATH_HOME="$PWD/badpath-home"
mkdir -p "$BADPATH_HOME/.claude-config/plugins"
python3 -c "
import json, pathlib
data = {'plugins': {'teamme@teamme': [{
    'scope': 'user', 'installPath': '$ROOT',
    'version': '9.9.9', 'lastUpdated': '2026-01-01T00:00:00.000Z',
}]}}
pathlib.Path('$BADPATH_HOME/.claude-config/plugins/installed_plugins.json').write_text(json.dumps(data))
"
badpath_env() { env -u CLAUDE_PLUGIN_ROOT HOME="$BADPATH_HOME" CLAUDE_CONFIG_DIR="$BADPATH_HOME/.claude-config" "$@"; }
BADPATH_JSON_FILE="$PWD/pf-badpath.json"
set +e
badpath_env python3 "$ISO_PF" check --project-dir "$STALE_FIXTURE" >/dev/null
BADPATH_RC=$?
set -e
[ "$BADPATH_RC" -eq 0 ] || fail "preflight exited $BADPATH_RC on a record whose installPath fails the marker check (must degrade, never be trusted blindly)"
badpath_env python3 "$ISO_PF" check --json --project-dir "$STALE_FIXTURE" > "$BADPATH_JSON_FILE"
python3 -c "
import json
d = json.load(open('$BADPATH_JSON_FILE'))
hooks_check = next(c for c in d['checks'] if c['id'] == 'hooks')
if not hooks_check.get('ok'):
    raise SystemExit(f'a record pointing at a path with no shipped templates was trusted instead of rejected: {hooks_check}')
if 'not verified' not in hooks_check.get('detail', ''):
    raise SystemExit(f'a bad-path record was not rejected down to existence-only: {hooks_check}')
"
echo "  ok: a record whose installPath fails the marker check is rejected, not compared against"

echo "== mcp server: teamme_install leaves a stale hook alone without force, replaces it with force=true =="
FORCE_PROJ="$PWD/force-repair-proj"
cp -r pf-full "$FORCE_PROJ"
python3 -c "
import pathlib
p = pathlib.Path('$FORCE_PROJ/.claude/hooks/worklog.py')
p.write_bytes(p.read_bytes() + b'\n# locally modified\n')
"
python3 - "$ROOT" "$FORCE_PROJ" <<'PY'
import json, pathlib, subprocess, sys

root = pathlib.Path(sys.argv[1])
proj = pathlib.Path(sys.argv[2])
server = root / "plugins/teamme/server/teamme_mcp.py"
hook_path = proj / ".claude" / "hooks" / "worklog.py"
template_path = root / "plugins/teamme/templates/hooks/worklog.py"
stale_bytes = hook_path.read_bytes()


def send(proc, obj):
    proc.stdin.write(json.dumps(obj) + "\n")
    proc.stdin.flush()


def recv(proc):
    line = proc.stdout.readline()
    if not line:
        sys.exit(f"server closed the pipe unexpectedly; stderr: {proc.stderr.read()}")
    return json.loads(line)


def call_text(resp):
    result = resp.get("result") or {}
    return result, "".join(c.get("text", "") for c in result.get("content") or [])


proc = subprocess.Popen(
    ["python3", str(server)],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    text=True, bufsize=1,
)
try:
    send(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18"}})
    recv(proc)

    send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "teamme_status", "arguments": {"project_dir": str(proj)}}})
    result, text = call_text(recv(proc))
    if "state: installed-outdated" not in text:
        sys.exit(f"teamme_status did not report installed-outdated for a stale hook: {text!r}")

    # without force: the stale file must be left exactly alone
    send(proc, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                "params": {"name": "teamme_install", "arguments": {"project_dir": str(proj)}}})
    result, text = call_text(recv(proc))
    if result.get("isError"):
        sys.exit(f"teamme_install (no force) reported an error: {text}")
    if hook_path.read_bytes() != stale_bytes:
        sys.exit("teamme_install without force modified a stale hook - it must refuse without force=true")
    if "differs" not in text:
        sys.exit(f"teamme_install without force did not report the hook as differing from the plugin's copy: {text}")

    send(proc, {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                "params": {"name": "teamme_status", "arguments": {"project_dir": str(proj)}}})
    result, text = call_text(recv(proc))
    if "state: installed-outdated" not in text:
        sys.exit(f"status changed even though the stale hook was correctly left alone: {text!r}")

    # with force=true: replaced with the plugin's copy
    send(proc, {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                "params": {"name": "teamme_install",
                           "arguments": {"project_dir": str(proj), "force": True}}})
    result, text = call_text(recv(proc))
    if result.get("isError"):
        sys.exit(f"teamme_install (force=true) reported an error: {text}")
    if hook_path.read_bytes() != template_path.read_bytes():
        sys.exit("teamme_install force=true did not restore the hook to match the plugin's copy")
    if "replaced" not in text:
        sys.exit(f"teamme_install force=true did not report the hook as replaced: {text}")

    send(proc, {"jsonrpc": "2.0", "id": 6, "method": "tools/call",
                "params": {"name": "teamme_status", "arguments": {"project_dir": str(proj)}}})
    result, text = call_text(recv(proc))
    if "state: installed-outdated" in text:
        sys.exit(f"status still reports installed-outdated after a forced repair: {text!r}")
finally:
    try:
        proc.stdin.close()
    except Exception:
        pass
    proc.wait(timeout=5)

print("  ok: teamme_install refuses a stale hook without force=true, and force=true replaces it and clears the state")
PY

# A 0.1.0-shaped install: the five original hooks, no preflight.py, an
# already-generated intake.md, and a settings.json missing the preflight.py
# heartbeat entry this release added. Building this from real template pieces
# (not hand-authored JSON) is what makes the fixture honest.
make_outdated_fixture() {
  local dir="$1"
  mkdir -p "$dir/.claude/hooks" "$dir/.claude/intake" "$dir/.claude/commands"
  cp "$H/intake-state.py" "$H/intake-guard.py" "$H/route-to-intake.py" \
     "$H/worklog-enforce.py" "$H/worklog.py" "$dir/.claude/hooks/"
  python3 -c "
import json, pathlib
tpl = json.loads(pathlib.Path('$ROOT/plugins/teamme/templates/settings.hooks.json').read_text())
tpl['hooks']['SessionStart'] = [tpl['hooks']['SessionStart'][0]]  # drop the preflight.py heartbeat entry - 0.1.0 predates it
pathlib.Path('$dir/.claude/settings.json').write_text(json.dumps(tpl, indent=2) + chr(10))
pathlib.Path('$dir/.claude/commands/intake.md').write_text(
    '---\ndescription: project-specific intake\n---\nMARKER-DO-NOT-CLOBBER project-specific body\n'
)
"
}

echo "== preflight: installed-outdated (a 0.1.0-shaped install) is never told to run init-team =="
make_outdated_fixture "$PWD/pf-outdated"
test -f "$PWD/pf-outdated/.claude/hooks/preflight.py" \
  && fail "the 0.1.0-shaped fixture accidentally shipped preflight.py - it must predate it"
python3 - "$PF" "$PWD/pf-outdated" <<'PY'
import json, pathlib, subprocess, sys

pf, outdated_dir = sys.argv[1], sys.argv[2]
proc = subprocess.run(
    ["python3", pf, "check", "--json", "--project-dir", outdated_dir],
    capture_output=True, text=True, timeout=10,
)
if proc.returncode == 0:
    sys.exit("preflight check exited 0 for a 0.1.0-shaped installed-outdated project")
try:
    data = json.loads(proc.stdout)
except Exception as exc:
    sys.exit(f"installed-outdated check produced unparseable output: {exc}\n{proc.stdout}")
if data.get("state") != "installed-outdated":
    sys.exit(f"expected state installed-outdated for the 0.1.0-shaped fixture, got {data.get('state')!r}: {data}")
blob = json.dumps(data)
if "init-team" in blob:
    sys.exit(
        "an installed-outdated project was told to run /teamme:init-team - this is exactly the T23 "
        f"regression (an existing install told to reinstall over itself): {blob}"
    )
print("  ok: installed-outdated is reported (not not-installed), exit is non-zero, and no fix mentions init-team")
PY

echo "== preflight: the install-evidence probe has no false positives on a stranger's repo =="
mkdir -p stranger-proj/.claude
python3 -c "
import json, pathlib
pathlib.Path('stranger-proj/.claude/settings.json').write_text(json.dumps({
    'hooks': {'SessionStart': [{'hooks': [{'type': 'command', 'command': 'python3 .claude/hooks/some-other-tool.py'}]}]}
}, indent=2) + chr(10))
"
python3 - "$PF" "$PWD/stranger-proj" <<'PY'
import json, subprocess, sys

pf, stranger_dir = sys.argv[1], sys.argv[2]
proc = subprocess.run(
    ["python3", pf, "check", "--json", "--project-dir", stranger_dir],
    capture_output=True, text=True, timeout=10,
)
data = json.loads(proc.stdout)
if data.get("state") != "not-installed":
    sys.exit(
        f"a project with its own unrelated SessionStart hook and no teamme reported state "
        f"{data.get('state')!r} instead of not-installed: {data}"
    )
print("  ok: an unrelated SessionStart hook does not count as teamme install evidence")
PY

echo "== mcp server: teamme_install repairs an installed-outdated project without touching intake.md =="
make_outdated_fixture "$PWD/repair-proj"
python3 - "$ROOT" "$PWD/repair-proj" <<'PY'
import json, pathlib, subprocess, sys

root = pathlib.Path(sys.argv[1])
repair_proj = pathlib.Path(sys.argv[2])
orig_intake_md = (repair_proj / ".claude" / "commands" / "intake.md").read_text()
server = root / "plugins/teamme/server/teamme_mcp.py"


def send(proc, obj):
    proc.stdin.write(json.dumps(obj) + "\n")
    proc.stdin.flush()


def recv(proc):
    line = proc.stdout.readline()
    if not line:
        sys.exit(f"server closed the pipe unexpectedly; stderr: {proc.stderr.read()}")
    return json.loads(line)


def call_text(resp):
    result = resp.get("result") or {}
    return result, "".join(c.get("text", "") for c in result.get("content") or [])


proc = subprocess.Popen(
    ["python3", str(server)],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    text=True, bufsize=1,
)
try:
    send(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18"}})
    recv(proc)

    send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "teamme_status",
                           "arguments": {"project_dir": str(repair_proj)}}})
    result, text = call_text(recv(proc))
    if "installed-outdated" not in text:
        sys.exit(f"teamme_status did not report installed-outdated before repair: {text!r}")

    send(proc, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                "params": {"name": "teamme_install",
                           "arguments": {"project_dir": str(repair_proj)}}})
    result, text = call_text(recv(proc))
    if result.get("isError"):
        sys.exit(f"teamme_install reported an error repairing an installed-outdated project: {text}")
    if "state: installed-outdated" in text:
        sys.exit(f"teamme_install did not repair the installed-outdated project: {text}")
    if "state: installed-not-live" not in text:
        sys.exit(f"expected teamme_install to leave the project installed-not-live (repaired, not yet heartbeated): {text}")
finally:
    try:
        proc.stdin.close()
    except Exception:
        pass
    proc.wait(timeout=5)

new_intake_md = (repair_proj / ".claude" / "commands" / "intake.md").read_text()
if new_intake_md != orig_intake_md:
    sys.exit(
        "teamme_install clobbered the project-specific .claude/commands/intake.md while repairing an "
        f"installed-outdated project.\nbefore: {orig_intake_md!r}\nafter:  {new_intake_md!r}"
    )
print("  ok: teamme_install repairs an installed-outdated project and leaves intake.md untouched")
PY

echo "== preflight heartbeat: silent, always exits 0, even unscaffolded with stdin closed =="
mkdir -p hb-empty
set +e
HB_OUT="$(CLAUDE_PROJECT_DIR="$PWD/hb-empty" python3 "$PF" heartbeat </dev/null 2>&1)"
HB_RC=$?
set -e
[ -z "$HB_OUT" ] || fail "heartbeat printed output: $HB_OUT"
[ "$HB_RC" -eq 0 ] || fail "heartbeat exited $HB_RC"
test -f "hb-empty/.claude/intake/heartbeat.json" || fail "heartbeat did not stamp its file"
echo "  ok: heartbeat is silent and always exits 0"

echo "== librarian: git fixtures live under a fresh subdirectory of the throwaway project; never touch this repos own .claude/librarians/ =="
LIBPATH="$ROOT/plugins/teamme/server"
gitc() { git -c user.name=teamme-fixture -c user.email=teamme-fixture@example.invalid -c commit.gpgsign=false "$@"; }

echo "== librarian: rebuild-from-text is row-for-row identical to ground truth, with git genuinely unavailable =="
LIB_REBUILD="$PWD/lib-repo-rebuild"
mkdir -p "$LIB_REBUILD"
gitc -C "$LIB_REBUILD" init -q
for i in 1 2 3 4 5 6; do
  echo "content-$i" > "$LIB_REBUILD/f$i.txt"
  gitc -C "$LIB_REBUILD" add "f$i.txt"
  gitc -C "$LIB_REBUILD" commit -qm "commit $i"
done
# Ground truth computed with plain git, entirely independent of the librarian
# module, so a bug shared between the initial index and the later rebuild (both
# of which call the same insert_commit/rebuild code) cannot hide from this check.
GT_HASHES="$(git -C "$LIB_REBUILD" log --format=%H | sort)"
GT_HEAD="$(git -C "$LIB_REBUILD" rev-parse HEAD)"
GT_COUNT="$(git -C "$LIB_REBUILD" rev-list --count HEAD)"
python3 - "$LIBPATH" "$LIB_REBUILD" <<'PY'
import sys
libpath, repo = sys.argv[1], sys.argv[2]
sys.path.insert(0, libpath)
from librarian import history
r = history.index(repo, full=True)
if not r.get("ok"):
    sys.exit(f"initial full index of the rebuild fixture failed: {r}")
PY

for suf in "" "-wal" "-shm"; do
  rm -f "$LIB_REBUILD/.claude/librarians/index.db$suf"
done

python3 - "$LIBPATH" "$LIB_REBUILD" "$GT_HASHES" "$GT_HEAD" "$GT_COUNT" <<'PY'
import sys, os
libpath, repo, gt_hashes, gt_head, gt_count = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5]
sys.path.insert(0, libpath)
from librarian import store

os.environ["PATH"] = "/nonexistent-so-git-cannot-be-found"
rb = store.rebuild(repo)
if not rb.get("ok"):
    sys.exit(f"rebuild-from-text failed with git unavailable: {rb}")

conn = store.connect(repo)
rows = {dict(r)["hash"]: dict(r) for r in conn.execute("SELECT * FROM commits").fetchall()}
files = [dict(r) for r in conn.execute("SELECT * FROM files_changed").fetchall()]
parents = [dict(r) for r in conn.execute("SELECT * FROM commit_parents").fetchall()]
marker = store.get_meta(conn, store.META_LAST_INDEXED)
conn.close()

expected_hashes = set(gt_hashes.split())
got_hashes = set(rows.keys())
if got_hashes != expected_hashes:
    sys.exit(f"rebuilt commit hashes do not match git's own log: missing {expected_hashes - got_hashes}, "
              f"extra {got_hashes - expected_hashes}")
if len(rows) != int(gt_count):
    sys.exit(f"expected {gt_count} commits (git rev-list --count), rebuild produced {len(rows)}")
if len(files) != int(gt_count):
    sys.exit(f"expected {gt_count} file rows (one distinct file per commit), rebuild produced {len(files)}")
expected_paths = {f"f{i}.txt" for i in range(1, int(gt_count) + 1)}
got_paths = {f["path"] for f in files}
if got_paths != expected_paths:
    sys.exit(f"rebuilt file paths do not match: expected {expected_paths}, got {got_paths}")
if len(parents) != int(gt_count) - 1:
    sys.exit(f"expected {int(gt_count) - 1} parent edges (a linear chain), rebuild produced {len(parents)}")
if marker != gt_head:
    sys.exit(f"the last-indexed marker did not match git's own HEAD: marker={marker!r}, HEAD={gt_head!r}")

print(f"  ok: rebuild-from-text reproduced all {len(rows)} commit(s) (hashes, file paths, parent edges) "
      f"and the marker, matched against git's own log directly - with git genuinely unavailable")
PY

echo "== librarian: empty repo, not-a-git-repo, a missing directory, and git-missing are distinct, clean errors =="
LIB_EMPTY="$PWD/lib-repo-empty"
LIB_NOTREPO="$PWD/lib-repo-notrepo"
mkdir -p "$LIB_EMPTY" "$LIB_NOTREPO"
gitc -C "$LIB_EMPTY" init -q
python3 - "$LIBPATH" "$LIB_EMPTY" "$LIB_NOTREPO" "$PWD/lib-repo-missing-does-not-exist" <<'PY'
import sys
libpath, empty_dir, notrepo_dir, missing_dir = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
sys.path.insert(0, libpath)
from librarian import history
import os

r = history.index(empty_dir, full=True)
if not r.get("ok"):
    sys.exit(f"an empty repository was not indexed as a success: {r}")
if r.get("commits_added") != 0 or r.get("commits") != 0:
    sys.exit(f"an empty repository should index zero rows, got: {r}")
if r.get("error"):
    sys.exit(f"an empty repository reported an error instead of success: {r}")

not_repo = history.index(notrepo_dir)
if not_repo.get("ok"):
    sys.exit(f"a plain (non-git) directory was reported as successfully indexed: {not_repo}")
err_notrepo = not_repo.get("error") or ""
if "not a git repository" not in err_notrepo:
    sys.exit(f"a non-git directory did not report 'not a git repository': {err_notrepo!r}")
if "no such directory" in err_notrepo:
    sys.exit(f"a non-git directory was misreported as a missing directory: {err_notrepo!r}")

missing = history.index(missing_dir)
if missing.get("ok"):
    sys.exit(f"a directory that does not exist was reported as successfully indexed: {missing}")
err_missing = missing.get("error") or ""
if "no such directory" not in err_missing:
    sys.exit(f"a missing directory did not report 'no such directory': {err_missing!r}")
if "not a git repository" in err_missing:
    sys.exit(f"a missing directory was misreported as an existing non-git repository: {err_missing!r}")
if "git is not installed" in err_missing:
    sys.exit(f"a missing directory was misdiagnosed as git being uninstalled - this is exactly the "
              f"confusion the hook lane says it fixed: {err_missing!r}")

old_path = os.environ.get("PATH")
os.environ["PATH"] = "/nonexistent-so-git-cannot-be-found"
try:
    git_missing = history.index(empty_dir)
finally:
    os.environ["PATH"] = old_path
if git_missing.get("ok"):
    sys.exit(f"git genuinely missing was reported as a successful index: {git_missing}")
err_git = git_missing.get("error") or ""
if "git is not installed" not in err_git:
    sys.exit(f"git genuinely missing did not report 'git is not installed': {err_git!r}")
if "no such directory" in err_git or "not a git repository" in err_git:
    sys.exit(f"git-missing was conflated with a directory/repo problem: {err_git!r}")

print("  ok: empty repo succeeds with zero rows; not-a-repo, missing-directory and git-missing "
      "each report a distinct, non-overlapping error")
PY

echo "== librarian: an unreachable marker (rebase/force-push) falls back to a full reindex and SAYS SO =="
LIB_REBASE="$PWD/lib-repo-rebase"
mkdir -p "$LIB_REBASE"
gitc -C "$LIB_REBASE" init -q
echo one > "$LIB_REBASE/f.txt"; gitc -C "$LIB_REBASE" add f.txt; gitc -C "$LIB_REBASE" commit -qm "c1"
echo two >> "$LIB_REBASE/f.txt"; gitc -C "$LIB_REBASE" add f.txt; gitc -C "$LIB_REBASE" commit -qm "c2"
python3 - "$LIBPATH" "$LIB_REBASE" <<'PY'
import sys
libpath, repo = sys.argv[1], sys.argv[2]
sys.path.insert(0, libpath)
from librarian import history

r = history.index(repo, full=True)
if not r.get("ok") or r.get("commits") != 2:
    sys.exit(f"initial index of the rebase fixture failed: {r}")
PY
gitc -C "$LIB_REBASE" reset --hard HEAD~1 -q
echo three > "$LIB_REBASE/g.txt"; gitc -C "$LIB_REBASE" add g.txt; gitc -C "$LIB_REBASE" commit -qm "c2-alt (history rewritten)"
python3 - "$LIBPATH" "$LIB_REBASE" <<'PY'
import sys
libpath, repo = sys.argv[1], sys.argv[2]
sys.path.insert(0, libpath)
from librarian import history

r = history.index(repo)
if not r.get("ok"):
    sys.exit(f"the refresh after a rewritten history did not complete: {r}")
fallback = r.get("fallback") or ""
if not fallback:
    sys.exit(f"an unreachable marker did not report a fallback at all (silent fallback is the dangerous "
              f"version of this bug): {r}")
if "rebase" not in fallback or "force-push" not in fallback:
    sys.exit(f"the fallback message did not name rebase/force-push: {fallback!r}")
if r.get("mode") != "full":
    sys.exit(f"an unreachable marker did not fall back to a full reindex, mode was {r.get('mode')!r}: {r}")
if r.get("commits") != 2:
    sys.exit(f"the fallback full reindex did not recover the 2 reachable commits: {r}")
print(f"  ok: an unreachable marker falls back to a full reindex and says so: {fallback}")
PY

echo "== librarian: a corrupt index.db is discarded (not raised) through the MCP query tool, and an instructed refresh rebuilds it =="
LIB_CORRUPT="$PWD/lib-repo-corrupt"
mkdir -p "$LIB_CORRUPT"
gitc -C "$LIB_CORRUPT" init -q
echo one > "$LIB_CORRUPT/f.txt"; gitc -C "$LIB_CORRUPT" add f.txt; gitc -C "$LIB_CORRUPT" commit -qm "c1"
echo two >> "$LIB_CORRUPT/f.txt"; gitc -C "$LIB_CORRUPT" add f.txt; gitc -C "$LIB_CORRUPT" commit -qm "c2"
python3 - "$LIBPATH" "$LIB_CORRUPT" <<'PY'
import sys
libpath, repo = sys.argv[1], sys.argv[2]
sys.path.insert(0, libpath)
from librarian import history
r = history.index(repo, full=True)
if not r.get("ok") or r.get("commits") != 2:
    sys.exit(f"initial index of the corrupt-db fixture failed: {r}")
PY
printf 'this is not a sqlite database, just garbage bytes' > "$LIB_CORRUPT/.claude/librarians/index.db"
python3 - "$ROOT" "$LIB_CORRUPT" <<'PY'
import json, pathlib, subprocess, sys

root = pathlib.Path(sys.argv[1])
proj = sys.argv[2]
server = root / "plugins/teamme/server/teamme_mcp.py"


def send(proc, obj):
    proc.stdin.write(json.dumps(obj) + "\n")
    proc.stdin.flush()


def recv(proc):
    line = proc.stdout.readline()
    if not line:
        sys.exit(f"server closed the pipe unexpectedly; stderr: {proc.stderr.read()}")
    return json.loads(line)


def call_text(resp):
    result = resp.get("result") or {}
    return result, "".join(c.get("text", "") for c in result.get("content") or [])


proc = subprocess.Popen(
    ["python3", str(server)],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    text=True, bufsize=1,
)
try:
    send(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18"}})
    recv(proc)

    # the corrupt db must be discarded cleanly - no crash, a controlled error result
    send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "teamme_librarian_query",
                           "arguments": {"project_dir": proj, "query": "recent"}}})
    result, text = call_text(recv(proc))
    if not result.get("isError"):
        sys.exit(f"a corrupt index.db did not surface as an error through the query tool: {text}")
    if "discarded" not in text:
        sys.exit(f"the corrupt-db query result did not say the file was discarded: {text!r}")
    if "teamme_librarian_refresh" not in text:
        sys.exit(f"the corrupt-db query result did not point at teamme_librarian_refresh: {text!r}")

    # following that instruction rebuilds the data from commits.jsonl, not raising either
    send(proc, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                "params": {"name": "teamme_librarian_refresh",
                           "arguments": {"project_dir": proj}}})
    result, text = call_text(recv(proc))
    if result.get("isError"):
        sys.exit(f"the refresh that repairs a discarded index.db reported an error: {text}")
    if "rebuilt" not in text or "commits.jsonl" not in text:
        sys.exit(f"the refresh did not report rebuilding from commits.jsonl: {text!r}")

    # and the data is really back - a real query succeeds with the original rows
    send(proc, {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                "params": {"name": "teamme_librarian_query",
                           "arguments": {"project_dir": proj, "query": "recent"}}})
    result, text = call_text(recv(proc))
    if result.get("isError"):
        sys.exit(f"the query after rebuild still failed: {text}")
    if "2 row(s)" not in text:
        sys.exit(f"the rebuilt index did not hold the original 2 commits: {text!r}")
finally:
    try:
        proc.stdin.close()
    except Exception:
        pass
    proc.wait(timeout=5)

print("  ok: a corrupt index.db is discarded through the query tool without raising, and the "
      "instructed refresh rebuilds it from commits.jsonl")
PY

echo "== librarian: N concurrent refreshes on one repo leave no duplicate rows and no lock/tmp debris =="
LIB_CONCURRENT="$PWD/lib-repo-concurrent"
mkdir -p "$LIB_CONCURRENT"
gitc -C "$LIB_CONCURRENT" init -q
for i in 1 2 3 4 5 6 7 8; do
  echo "c$i" >> "$LIB_CONCURRENT/f.txt"
  gitc -C "$LIB_CONCURRENT" add f.txt
  gitc -C "$LIB_CONCURRENT" commit -qm "commit $i"
done
for i in $(seq 1 10); do
  PYTHONPATH="$LIBPATH" python3 -c "
from librarian import history
history.index('$LIB_CONCURRENT')
" >/dev/null 2>&1 &
done
wait
python3 - "$LIBPATH" "$LIB_CONCURRENT" <<'PY'
import sys, pathlib
libpath, repo = sys.argv[1], sys.argv[2]
sys.path.insert(0, libpath)
from librarian import store

conn = store.connect(repo)
n = store.counts(conn)
conn.close()
if n.get("commits") != 8:
    sys.exit(f"expected exactly 8 commits after 10 concurrent refreshes, got {n.get('commits')} "
             f"(a number above 8 means duplicates, below means lost writes): {n}")

history_dir = store.history_dir(repo)
librarians_dir = store.librarians_dir(repo)
leftover = list(history_dir.glob("*.lock")) + list(history_dir.glob(".*.tmp")) + \
    list(librarians_dir.glob("*.lock")) + list(librarians_dir.glob(".*.tmp"))
if leftover:
    sys.exit(f"leftover lock/tmp file(s) after concurrent refreshes: {leftover}")
print("  ok: 10 concurrent refreshes on one repo produced exactly 8 commits, no duplicates, no debris")
PY

echo "== librarian: commits_behind_head reaches zero after a refresh (over the MCP pipe) =="
LIB_STALE="$PWD/lib-repo-stale"
mkdir -p "$LIB_STALE"
gitc -C "$LIB_STALE" init -q
echo one > "$LIB_STALE/f.txt"; gitc -C "$LIB_STALE" add f.txt; gitc -C "$LIB_STALE" commit -qm "c1"
python3 - "$LIBPATH" "$LIB_STALE" <<'PY'
import sys
libpath, repo = sys.argv[1], sys.argv[2]
sys.path.insert(0, libpath)
from librarian import history
r = history.index(repo, full=True)
if not r.get("ok"):
    sys.exit(f"initial index of the staleness fixture failed: {r}")
PY
echo two >> "$LIB_STALE/f.txt"; gitc -C "$LIB_STALE" add f.txt; gitc -C "$LIB_STALE" commit -qm "c2"
echo three >> "$LIB_STALE/f.txt"; gitc -C "$LIB_STALE" add f.txt; gitc -C "$LIB_STALE" commit -qm "c3"
python3 - "$ROOT" "$LIB_STALE" <<'PY'
import json, pathlib, subprocess, sys

root = pathlib.Path(sys.argv[1])
proj = sys.argv[2]
server = root / "plugins/teamme/server/teamme_mcp.py"


def send(proc, obj):
    proc.stdin.write(json.dumps(obj) + "\n")
    proc.stdin.flush()


def recv(proc):
    line = proc.stdout.readline()
    if not line:
        sys.exit(f"server closed the pipe unexpectedly; stderr: {proc.stderr.read()}")
    return json.loads(line)


def call_text(resp):
    result = resp.get("result") or {}
    return result, "".join(c.get("text", "") for c in result.get("content") or [])


proc = subprocess.Popen(
    ["python3", str(server)],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    text=True, bufsize=1,
)
try:
    send(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18"}})
    recv(proc)

    send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "teamme_librarian_status", "arguments": {"project_dir": proj}}})
    _, text = call_text(recv(proc))
    if "behind HEAD:     2 commit(s)" not in text:
        sys.exit(f"status before refresh did not report 2 commits behind HEAD: {text!r}")

    send(proc, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                "params": {"name": "teamme_librarian_refresh", "arguments": {"project_dir": proj}}})
    result, text = call_text(recv(proc))
    if result.get("isError"):
        sys.exit(f"the refresh that should clear staleness reported an error: {text}")

    send(proc, {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                "params": {"name": "teamme_librarian_status", "arguments": {"project_dir": proj}}})
    _, text = call_text(recv(proc))
    if "behind HEAD:     0 commit(s) - up to date" not in text:
        sys.exit(f"status after refresh did not report caught up to HEAD: {text!r}")
finally:
    try:
        proc.stdin.close()
    except Exception:
        pass
    proc.wait(timeout=5)

print("  ok: commits_behind_head goes from 2 to 0 after a refresh")
PY

echo "== librarian: hostile content - a newline in a path, a non-UTF-8 subject, a merge commit =="
LIB_HOSTILE="$PWD/lib-repo-hostile"
mkdir -p "$LIB_HOSTILE/weird"
gitc -C "$LIB_HOSTILE" init -q
printf 'content\n' > "$LIB_HOSTILE/weird/name"$'\n'"with-newline.txt"
gitc -C "$LIB_HOSTILE" add -A
gitc -C "$LIB_HOSTILE" commit -qm "path with newline"
echo more > "$LIB_HOSTILE/b.txt"
gitc -C "$LIB_HOSTILE" add b.txt
python3 -c "open('$PWD/lib-hostile-msg', 'wb').write(b'bad subject: \xff\xfe not utf8\n')"
gitc -C "$LIB_HOSTILE" commit -q -F "$PWD/lib-hostile-msg" >/dev/null 2>&1
gitc -C "$LIB_HOSTILE" checkout -q -b feature
echo feat > "$LIB_HOSTILE/c.txt"; gitc -C "$LIB_HOSTILE" add c.txt; gitc -C "$LIB_HOSTILE" commit -qm "feature commit"
gitc -C "$LIB_HOSTILE" checkout -q -
echo mainf > "$LIB_HOSTILE/d.txt"; gitc -C "$LIB_HOSTILE" add d.txt; gitc -C "$LIB_HOSTILE" commit -qm "main commit"
gitc -C "$LIB_HOSTILE" merge --no-ff -q -m "merge feature" feature
python3 - "$LIBPATH" "$LIB_HOSTILE" <<'PY'
import sys
libpath, repo = sys.argv[1], sys.argv[2]
sys.path.insert(0, libpath)
from librarian import store, history

r = history.index(repo, full=True)
if not r.get("ok"):
    sys.exit(f"indexing hostile content raised/failed instead of tolerating it: {r}")
if r.get("commits") != 5:
    sys.exit(f"expected 5 commits (including the merge) in the hostile-content fixture, got {r}")

conn = store.connect(repo)
paths = [row["path"] for row in conn.execute("SELECT path FROM files_changed").fetchall()]
if not any("\n" in p for p in paths):
    sys.exit(f"a path containing a literal newline did not survive indexing: {paths}")

subjects = [row["subject"] for row in conn.execute("SELECT subject FROM commits").fetchall()]
if not any(s.startswith("bad subject:") for s in subjects):
    sys.exit(f"the non-UTF-8 commit subject did not survive indexing: {subjects}")

merge_row = conn.execute("SELECT hash FROM commits WHERE subject = 'merge feature'").fetchone()
if merge_row is None:
    sys.exit(f"the merge commit was not indexed at all: {subjects}")
merge_hash = merge_row["hash"]
n_parents = conn.execute(
    "SELECT COUNT(*) AS n FROM commit_parents WHERE hash = ?", (merge_hash,)
).fetchone()["n"]
n_files = conn.execute(
    "SELECT COUNT(*) AS n FROM files_changed WHERE hash = ?", (merge_hash,)
).fetchone()["n"]
conn.close()
if n_parents != 2:
    sys.exit(f"the merge commit should have exactly 2 parent rows, got {n_parents}")
if n_files != 0:
    sys.exit(f"the merge commit should have no file rows (no numstat for a plain merge), got {n_files}")

print("  ok: a newline in a path, a non-UTF-8 subject, and a merge commit (2 parent rows, 0 file "
      "rows) all parse without raising")
PY

echo "== librarian: over the real JSON-RPC pipe - all three tools, bad input, limit clamp, garbage line =="
LIB_PIPE="$PWD/lib-repo-pipe"
mkdir -p "$LIB_PIPE"
gitc -C "$LIB_PIPE" init -q
for i in 1 2 3; do
  echo "p$i" >> "$LIB_PIPE/f.txt"
  gitc -C "$LIB_PIPE" add f.txt
  gitc -C "$LIB_PIPE" commit -qm "pipe commit $i"
done
python3 - "$ROOT" "$LIB_PIPE" <<'PY'
import json, pathlib, subprocess, sys

root = pathlib.Path(sys.argv[1])
proj = sys.argv[2]
server = root / "plugins/teamme/server/teamme_mcp.py"


def send(proc, obj):
    proc.stdin.write(json.dumps(obj) + "\n")
    proc.stdin.flush()


def recv(proc):
    line = proc.stdout.readline()
    if not line:
        sys.exit(f"server closed the pipe unexpectedly; stderr: {proc.stderr.read()}")
    return json.loads(line)


def call_text(resp):
    result = resp.get("result") or {}
    return result, "".join(c.get("text", "") for c in result.get("content") or [])


proc = subprocess.Popen(
    ["python3", str(server)],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    text=True, bufsize=1,
)
try:
    send(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18"}})
    recv(proc)

    # all three librarian tools respond over the real pipe
    send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "teamme_librarian_refresh", "arguments": {"project_dir": proj}}})
    result, text = call_text(recv(proc))
    if result.get("isError"):
        sys.exit(f"teamme_librarian_refresh over the pipe reported an error: {text}")

    send(proc, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                "params": {"name": "teamme_librarian_status", "arguments": {"project_dir": proj}}})
    result, text = call_text(recv(proc))
    if result.get("isError") or "3 commit(s)" not in text:
        sys.exit(f"teamme_librarian_status over the pipe did not report 3 commits: {text!r}")

    send(proc, {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                "params": {"name": "teamme_librarian_query",
                           "arguments": {"project_dir": proj, "query": "recent"}}})
    result, text = call_text(recv(proc))
    if result.get("isError") or "3 row(s)" not in text:
        sys.exit(f"teamme_librarian_query over the pipe did not return 3 rows: {text!r}")

    # an unknown query name is a clean error, not a crash
    send(proc, {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                "params": {"name": "teamme_librarian_query",
                           "arguments": {"project_dir": proj, "query": "not_a_real_query"}}})
    result, text = call_text(recv(proc))
    if not result.get("isError"):
        sys.exit(f"an unknown query name was not reported as an error: {text}")

    # a bad hash is a clean error, not a crash
    send(proc, {"jsonrpc": "2.0", "id": 6, "method": "tools/call",
                "params": {"name": "teamme_librarian_query",
                           "arguments": {"project_dir": proj, "query": "files_in_commit",
                                         "hash": "not-hex!!"}}})
    result, text = call_text(recv(proc))
    if not result.get("isError"):
        sys.exit(f"a malformed commit hash was not reported as an error: {text}")

    # limit is clamped and truncation is reported
    send(proc, {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                "params": {"name": "teamme_librarian_query",
                           "arguments": {"project_dir": proj, "query": "recent", "limit": 1}}})
    result, text = call_text(recv(proc))
    if result.get("isError") or "TRUNCATED at 1" not in text:
        sys.exit(f"a limit of 1 with 3 rows available did not report truncation: {text!r}")

    send(proc, {"jsonrpc": "2.0", "id": 8, "method": "tools/call",
                "params": {"name": "teamme_librarian_query",
                           "arguments": {"project_dir": proj, "query": "recent", "limit": 999999}}})
    result, text = call_text(recv(proc))
    if result.get("isError") or "3 row(s)" not in text or "TRUNCATED" in text:
        sys.exit(f"an oversized limit was not clamped to the real row count: {text!r}")

    # a garbage (non-JSON) line still leaves the server exiting 0 with empty stderr
    proc.stdin.write("this is not json\n")
    proc.stdin.flush()
    garbage_resp = recv(proc)
    if garbage_resp.get("error", {}).get("code") != -32700:
        sys.exit(f"a garbage line did not produce a parse-error response: {garbage_resp}")
finally:
    try:
        proc.stdin.close()
    except Exception:
        pass
    rc = proc.wait(timeout=5)

stderr = proc.stderr.read()
if rc != 0:
    sys.exit(f"the server did not exit 0 after a garbage line and EOF: exit={rc}")
if stderr.strip():
    sys.exit(f"the server wrote to stderr: {stderr!r}")

print("  ok: all three librarian tools respond over the pipe; unknown query / bad hash are clean "
      "errors; limit is clamped with a truncation notice; a garbage line still exits 0, no stderr")
PY

echo "== librarian config: defaults with no config file, and asking for it never creates it =="
LIB_CFG_DEF="$PWD/lib-cfg-defaults"
mkdir -p "$LIB_CFG_DEF"
gitc -C "$LIB_CFG_DEF" init -q
echo one > "$LIB_CFG_DEF/f.txt"; gitc -C "$LIB_CFG_DEF" add f.txt; gitc -C "$LIB_CFG_DEF" commit -qm "c1"
python3 - "$ROOT" "$LIB_CFG_DEF" <<'PY'
import json, pathlib, subprocess, sys

root = pathlib.Path(sys.argv[1])
proj = pathlib.Path(sys.argv[2])
server = root / "plugins/teamme/server/teamme_mcp.py"
cfg_path = proj / ".claude" / "librarians" / "config.json"


def send(proc, obj):
    proc.stdin.write(json.dumps(obj) + "\n")
    proc.stdin.flush()


def recv(proc):
    line = proc.stdout.readline()
    if not line:
        sys.exit(f"server closed the pipe unexpectedly; stderr: {proc.stderr.read()}")
    return json.loads(line)


def call_text(resp):
    result = resp.get("result") or {}
    return result, "".join(c.get("text", "") for c in result.get("content") or [])


if cfg_path.exists():
    sys.exit(f"fixture is dirty: {cfg_path} already exists before any configure call")

proc = subprocess.Popen(
    ["python3", str(server)],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    text=True, bufsize=1,
)
try:
    send(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18"}})
    recv(proc)

    # no enabled/commit_record argument at all - a pure read
    send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "teamme_librarian_configure",
                           "arguments": {"project_dir": str(proj)}}})
    result, text = call_text(recv(proc))
    if result.get("isError"):
        sys.exit(f"a read-only configure call reported an error: {text}")
    if "reporting only, nothing was changed" not in text:
        sys.exit(f"a no-argument call was not reported as read-only: {text!r}")
    if "history: enabled" not in text:
        sys.exit(f"the default history entry was not reported as enabled: {text!r}")
    if "commit_record: false" not in text:
        sys.exit(f"commit_record did not default to false: {text!r}")
    if "(absent - the documented defaults are in force)" not in text:
        sys.exit(f"an absent config file was not reported as absent: {text!r}")
finally:
    try:
        proc.stdin.close()
    except Exception:
        pass
    proc.wait(timeout=5)

if cfg_path.exists():
    sys.exit(f"merely reporting the config created {cfg_path} - asking must never be the thing that writes state")

print("  ok: defaults with no config file; asking what the config is never creates config.json")
PY

echo "== librarian config: [watch-fail] disabled gate - refresh/query refuse, status still answers =="
LIB_CFG_GATE="$PWD/lib-cfg-gate"
mkdir -p "$LIB_CFG_GATE"
gitc -C "$LIB_CFG_GATE" init -q
echo one > "$LIB_CFG_GATE/f.txt"; gitc -C "$LIB_CFG_GATE" add f.txt; gitc -C "$LIB_CFG_GATE" commit -qm "c1"
echo two >> "$LIB_CFG_GATE/f.txt"; gitc -C "$LIB_CFG_GATE" add f.txt; gitc -C "$LIB_CFG_GATE" commit -qm "c2"
python3 - "$ROOT" "$LIB_CFG_GATE" <<'PY'
import json, pathlib, subprocess, sys

root = pathlib.Path(sys.argv[1])
proj = sys.argv[2]
server = root / "plugins/teamme/server/teamme_mcp.py"


def send(proc, obj):
    proc.stdin.write(json.dumps(obj) + "\n")
    proc.stdin.flush()


def recv(proc):
    line = proc.stdout.readline()
    if not line:
        sys.exit(f"server closed the pipe unexpectedly; stderr: {proc.stderr.read()}")
    return json.loads(line)


def call_text(resp):
    result = resp.get("result") or {}
    return result, "".join(c.get("text", "") for c in result.get("content") or [])


proc = subprocess.Popen(
    ["python3", str(server)],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    text=True, bufsize=1,
)
try:
    send(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18"}})
    recv(proc)

    # build the index while the librarian is still enabled (the default)
    send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "teamme_librarian_refresh", "arguments": {"project_dir": proj}}})
    result, text = call_text(recv(proc))
    if result.get("isError"):
        sys.exit(f"the initial refresh (still enabled) reported an error: {text}")

    # disable it
    send(proc, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                "params": {"name": "teamme_librarian_configure",
                           "arguments": {"project_dir": proj, "librarian": "history",
                                         "enabled": False}}})
    result, text = call_text(recv(proc))
    if result.get("isError"):
        sys.exit(f"disabling history reported an error: {text}")
    if "DISABLED" not in text:
        sys.exit(f"configure did not report history as disabled: {text!r}")

    # refresh must now refuse
    send(proc, {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                "params": {"name": "teamme_librarian_refresh", "arguments": {"project_dir": proj}}})
    result, text = call_text(recv(proc))
    if not result.get("isError"):
        sys.exit(f"teamme_librarian_refresh did NOT refuse for a disabled librarian: {text}")
    if "disabled" not in text.lower() or "teamme_librarian_configure" not in text:
        sys.exit(f"the refresh refusal did not explain how to re-enable it: {text!r}")

    # query must now refuse
    send(proc, {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                "params": {"name": "teamme_librarian_query",
                           "arguments": {"project_dir": proj, "query": "recent"}}})
    result, text = call_text(recv(proc))
    if not result.get("isError"):
        sys.exit(f"teamme_librarian_query did NOT refuse for a disabled librarian: {text}")
    if "disabled" not in text.lower():
        sys.exit(f"the query refusal did not say the librarian is disabled: {text!r}")

    # status must NOT go dark - it is the one tool a disabled librarian must not silence
    send(proc, {"jsonrpc": "2.0", "id": 6, "method": "tools/call",
                "params": {"name": "teamme_librarian_status", "arguments": {"project_dir": proj}}})
    result, text = call_text(recv(proc))
    if result.get("isError"):
        sys.exit(f"teamme_librarian_status refused for a disabled librarian - it must always answer: {text}")
    if "NO - refresh and query refuse for it" not in text:
        sys.exit(f"status did not report the disabled setting: {text!r}")
    if "data:            yes" not in text:
        sys.exit(f"status lost the data it indexed before being disabled: {text!r}")
finally:
    try:
        proc.stdin.close()
    except Exception:
        pass
    proc.wait(timeout=5)

print("  ok: a disabled librarian makes refresh and query refuse, while status keeps answering")
PY

echo "== librarian config: [watch-fail] commit_record is ENACTED (git check-ignore is ground truth), and idempotent =="
LIB_CFG_GI="$PWD/lib-cfg-gitignore"
mkdir -p "$LIB_CFG_GI"
gitc -C "$LIB_CFG_GI" init -q
echo one > "$LIB_CFG_GI/f.txt"; gitc -C "$LIB_CFG_GI" add f.txt; gitc -C "$LIB_CFG_GI" commit -qm "c1"
python3 - "$ROOT" "$LIB_CFG_GI" <<'PY'
import json, pathlib, subprocess, sys

root = pathlib.Path(sys.argv[1])
proj = sys.argv[2]
server = root / "plugins/teamme/server/teamme_mcp.py"
gitignore = pathlib.Path(proj) / ".gitignore"
DB = ".claude/librarians/index.db"
RECORD = ".claude/librarians/history/commits.jsonl"


def send(proc, obj):
    proc.stdin.write(json.dumps(obj) + "\n")
    proc.stdin.flush()


def recv(proc):
    line = proc.stdout.readline()
    if not line:
        sys.exit(f"server closed the pipe unexpectedly; stderr: {proc.stderr.read()}")
    return json.loads(line)


def call_text(resp):
    result = resp.get("result") or {}
    return result, "".join(c.get("text", "") for c in result.get("content") or [])


def ignored(path: str) -> bool:
    """Ground truth from git itself, never from reading .gitignore's text."""
    r = subprocess.run(["git", "check-ignore", "-q", path], cwd=proj)
    if r.returncode not in (0, 1):
        sys.exit(f"git check-ignore errored (rc={r.returncode}) on {path}")
    return r.returncode == 0


proc = subprocess.Popen(
    ["python3", str(server)],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    text=True, bufsize=1,
)
try:
    send(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18"}})
    recv(proc)

    # commit_record=false (explicit): the record IS ignored, the .db always is
    send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "teamme_librarian_configure",
                           "arguments": {"project_dir": proj, "commit_record": False}}})
    result, text = call_text(recv(proc))
    if result.get("isError"):
        sys.exit(f"commit_record=false reported an error: {text}")
    if not ignored(DB):
        sys.exit("git check-ignore says the .db is NOT ignored - it must always be")
    if not ignored(RECORD):
        sys.exit("git check-ignore says the record is NOT ignored with commit_record=false")

    # commit_record=true: the record is UN-ignored, the .db is still ignored
    send(proc, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                "params": {"name": "teamme_librarian_configure",
                           "arguments": {"project_dir": proj, "commit_record": True}}})
    result, text = call_text(recv(proc))
    if result.get("isError"):
        sys.exit(f"commit_record=true reported an error: {text}")
    if not ignored(DB):
        sys.exit("git check-ignore says the .db is NOT ignored after commit_record=true - it must always be")
    if ignored(RECORD):
        sys.exit("git check-ignore still says the record is ignored after commit_record=true was enacted")

    text_after_first_true = gitignore.read_text()

    # idempotence: the same setting applied again must not change the file at all
    send(proc, {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                "params": {"name": "teamme_librarian_configure",
                           "arguments": {"project_dir": proj, "commit_record": True}}})
    result, text = call_text(recv(proc))
    if result.get("isError"):
        sys.exit(f"re-applying commit_record=true reported an error: {text}")
    if "already correct, nothing to do" not in text:
        sys.exit(f"re-applying an unchanged setting was not reported as a no-op: {text!r}")
    text_after_second_true = gitignore.read_text()
    if text_after_second_true != text_after_first_true:
        sys.exit(
            "applying commit_record=true twice produced a byte-different .gitignore:\n"
            f"--- first ---\n{text_after_first_true!r}\n--- second ---\n{text_after_second_true!r}"
        )
finally:
    try:
        proc.stdin.close()
    except Exception:
        pass
    proc.wait(timeout=5)

print("  ok: commit_record is enacted (git check-ignore agrees both ways) and re-applying it is byte-idempotent")
PY

echo "== librarian config: [watch-fail] a foreign ignore rule outside teamme's block is never deleted =="
LIB_CFG_FOREIGN="$PWD/lib-cfg-foreign"
mkdir -p "$LIB_CFG_FOREIGN"
gitc -C "$LIB_CFG_FOREIGN" init -q
echo one > "$LIB_CFG_FOREIGN/f.txt"; gitc -C "$LIB_CFG_FOREIGN" add f.txt; gitc -C "$LIB_CFG_FOREIGN" commit -qm "c1"
# The exact text of teamme's own RECORD_IGNORE constant (config.py) - "a
# matching ignore rule" means textually identical, since the code's foreign-
# line check is an exact string comparison against that constant, not a
# git-ignore glob evaluation.
cat > "$LIB_CFG_FOREIGN/.gitignore" <<'EOF'
# a rule I wrote myself, long before teamme existed
.claude/librarians/*/commits.jsonl
EOF
gitc -C "$LIB_CFG_FOREIGN" add .gitignore
gitc -C "$LIB_CFG_FOREIGN" commit -qm "my own gitignore"
python3 - "$ROOT" "$LIB_CFG_FOREIGN" <<'PY'
import json, pathlib, subprocess, sys

root = pathlib.Path(sys.argv[1])
proj = sys.argv[2]
server = root / "plugins/teamme/server/teamme_mcp.py"
gitignore = pathlib.Path(proj) / ".gitignore"
RECORD_GLOB = ".claude/librarians/*/commits.jsonl"   # teamme's own RECORD_IGNORE, verbatim
RECORD_PATH = ".claude/librarians/history/commits.jsonl"  # a real path, for git check-ignore
FOREIGN_COMMENT = "# a rule I wrote myself, long before teamme existed"
before = gitignore.read_text()
if RECORD_GLOB not in before or FOREIGN_COMMENT not in before:
    sys.exit(f"fixture is wrong: the foreign line/comment is not in the starting .gitignore: {before!r}")


def send(proc, obj):
    proc.stdin.write(json.dumps(obj) + "\n")
    proc.stdin.flush()


def recv(proc):
    line = proc.stdout.readline()
    if not line:
        sys.exit(f"server closed the pipe unexpectedly; stderr: {proc.stderr.read()}")
    return json.loads(line)


def call_text(resp):
    result = resp.get("result") or {}
    return result, "".join(c.get("text", "") for c in result.get("content") or [])


def ignored(path: str) -> bool:
    r = subprocess.run(["git", "check-ignore", "-q", path], cwd=proj)
    if r.returncode not in (0, 1):
        sys.exit(f"git check-ignore errored (rc={r.returncode}) on {path}")
    return r.returncode == 0


if not ignored(RECORD_PATH):
    sys.exit(f"fixture is wrong: {RECORD_PATH} is not actually ignored by the foreign rule yet")

proc = subprocess.Popen(
    ["python3", str(server)],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    text=True, bufsize=1,
)
try:
    send(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18"}})
    recv(proc)

    # ask for the record to be committed - it is already (foreign-)ignored
    send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "teamme_librarian_configure",
                           "arguments": {"project_dir": proj, "commit_record": True}}})
    result, text = call_text(recv(proc))
    if result.get("isError"):
        sys.exit(f"commit_record=true over a foreign rule reported an error: {text}")
    if "will not take effect" not in text:
        sys.exit(f"the result did not say the setting will not take effect: {text!r}")
finally:
    try:
        proc.stdin.close()
    except Exception:
        pass
    proc.wait(timeout=5)

after = gitignore.read_text()
if RECORD_GLOB not in after or FOREIGN_COMMENT not in after:
    sys.exit(
        "teamme deleted a line it did not write: the foreign ignore rule/comment is gone after "
        f"commit_record=true:\n--- before ---\n{before!r}\n--- after ---\n{after!r}"
    )
if not ignored(RECORD_PATH):
    sys.exit(
        "the record is no longer ignored by git even though the foreign rule was left in place - "
        "the foreign line survived textually but stopped being enforced, which is worse"
    )

print("  ok: a foreign ignore rule outside teamme's block survives commit_record=true untouched, "
      "and the result says the setting will not take effect")
PY

echo "== librarian config: a corrupt config.json degrades to defaults with a named problem, never an error, never disabled =="
LIB_CFG_CORRUPT="$PWD/lib-cfg-corrupt"
mkdir -p "$LIB_CFG_CORRUPT/.claude/librarians"
gitc -C "$LIB_CFG_CORRUPT" init -q
echo one > "$LIB_CFG_CORRUPT/f.txt"; gitc -C "$LIB_CFG_CORRUPT" add f.txt; gitc -C "$LIB_CFG_CORRUPT" commit -qm "c1"
printf '{ this is not valid json' > "$LIB_CFG_CORRUPT/.claude/librarians/config.json"
python3 - "$ROOT" "$LIB_CFG_CORRUPT" <<'PY'
import json, pathlib, subprocess, sys

root = pathlib.Path(sys.argv[1])
proj = sys.argv[2]
server = root / "plugins/teamme/server/teamme_mcp.py"


def send(proc, obj):
    proc.stdin.write(json.dumps(obj) + "\n")
    proc.stdin.flush()


def recv(proc):
    line = proc.stdout.readline()
    if not line:
        sys.exit(f"server closed the pipe unexpectedly; stderr: {proc.stderr.read()}")
    return json.loads(line)


def call_text(resp):
    result = resp.get("result") or {}
    return result, "".join(c.get("text", "") for c in result.get("content") or [])


proc = subprocess.Popen(
    ["python3", str(server)],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    text=True, bufsize=1,
)
try:
    send(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18"}})
    recv(proc)

    send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "teamme_librarian_status", "arguments": {"project_dir": proj}}})
    result, text = call_text(recv(proc))
    if result.get("isError"):
        sys.exit(f"a corrupt config.json made teamme_librarian_status ERROR instead of degrading: {text}")
    if "config problem:" not in text or "using defaults" not in text:
        sys.exit(f"a corrupt config.json was not named as a problem with the defaults used: {text!r}")
    if "NO - refresh and query refuse for it" in text:
        sys.exit(f"a corrupt config.json was read as DISABLING the librarian - it must default to enabled: {text!r}")
    if "commit_record:   false" not in text:
        sys.exit(f"a corrupt config.json did not fall back to commit_record=false: {text!r}")

    # "never disabled" proven, not just claimed: refresh and query must actually work
    send(proc, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                "params": {"name": "teamme_librarian_refresh", "arguments": {"project_dir": proj}}})
    result, text = call_text(recv(proc))
    if result.get("isError"):
        sys.exit(f"refresh refused over a corrupt config.json - it must degrade to enabled, not refuse: {text}")

    send(proc, {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                "params": {"name": "teamme_librarian_query",
                           "arguments": {"project_dir": proj, "query": "recent"}}})
    result, text = call_text(recv(proc))
    if result.get("isError"):
        sys.exit(f"query refused over a corrupt config.json - it must degrade to enabled, not refuse: {text}")
finally:
    try:
        proc.stdin.close()
    except Exception:
        pass
    proc.wait(timeout=5)

print("  ok: a corrupt config.json degrades to defaults with a named problem - never an error, never disabled")
PY

echo "== librarian query: commit_detail - body cap+truncation, an ambiguous short hash, a merge commit's zero file rows =="
LIB_CFG_DETAIL="$PWD/lib-cfg-detail"
mkdir -p "$LIB_CFG_DETAIL"
gitc -C "$LIB_CFG_DETAIL" init -q
gitc -C "$LIB_CFG_DETAIL" commit -q --allow-empty -m "base"
gitc -C "$LIB_CFG_DETAIL" commit -q --allow-empty -m "long body commit" -m "$(python3 -c 'print("z" * 5000, end="")')"
MAIN_BRANCH="$(git -C "$LIB_CFG_DETAIL" symbolic-ref --short HEAD)"
gitc -C "$LIB_CFG_DETAIL" checkout -q -b feature
gitc -C "$LIB_CFG_DETAIL" commit -q --allow-empty -m "feature work"
gitc -C "$LIB_CFG_DETAIL" checkout -q "$MAIN_BRANCH"
gitc -C "$LIB_CFG_DETAIL" commit -q --allow-empty -m "main work"
gitc -C "$LIB_CFG_DETAIL" merge --no-ff -q -m "merge feature" feature
python3 - "$ROOT" "$LIB_CFG_DETAIL" <<'PY'
import json, pathlib, subprocess, sys

root = pathlib.Path(sys.argv[1])
proj = sys.argv[2]
server = root / "plugins/teamme/server/teamme_mcp.py"

log = subprocess.run(
    ["git", "log", "--all", "--format=%H\t%s"], cwd=proj, capture_output=True, text=True, check=True,
).stdout.splitlines()
by_subject = {}
for line in log:
    h, _, s = line.partition("\t")
    by_subject[s] = h
long_hash = by_subject.get("long body commit")
merge_hash = by_subject.get("merge feature")
if not long_hash or not merge_hash:
    sys.exit(f"fixture is wrong: could not find the long-body or merge commit by exact subject "
              f"(long={long_hash!r}, merge={merge_hash!r}, subjects seen={sorted(by_subject)})")


def send(proc, obj):
    proc.stdin.write(json.dumps(obj) + "\n")
    proc.stdin.flush()


def recv(proc):
    line = proc.stdout.readline()
    if not line:
        sys.exit(f"server closed the pipe unexpectedly; stderr: {proc.stderr.read()}")
    return json.loads(line)


def call_text(resp):
    result = resp.get("result") or {}
    return result, "".join(c.get("text", "") for c in result.get("content") or [])


proc = subprocess.Popen(
    ["python3", str(server)],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    text=True, bufsize=1,
)
try:
    send(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18"}})
    recv(proc)

    send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "teamme_librarian_refresh", "arguments": {"project_dir": proj}}})
    result, text = call_text(recv(proc))
    if result.get("isError"):
        sys.exit(f"refresh of the commit_detail fixture reported an error: {text}")

    # the body cap, with its truncation notice
    send(proc, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                "params": {"name": "teamme_librarian_query",
                           "arguments": {"project_dir": proj, "query": "commit_detail",
                                         "hash": long_hash}}})
    result, text = call_text(recv(proc))
    if result.get("isError"):
        sys.exit(f"commit_detail on the long-body commit reported an error: {text}")
    if "BODY TRUNCATED at 4000 of 5000 characters" not in text:
        sys.exit(f"a 5000-character body was not reported as truncated at 4000: {text!r}")

    # a merge commit: two parents, zero file rows, by design
    send(proc, {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                "params": {"name": "teamme_librarian_query",
                           "arguments": {"project_dir": proj, "query": "commit_detail",
                                         "hash": merge_hash}}})
    result, text = call_text(recv(proc))
    if result.get("isError"):
        sys.exit(f"commit_detail on the merge commit reported an error: {text}")
    if "files:    0" not in text or "(no file rows - a merge commit records none, by design)" not in text:
        sys.exit(f"the merge commit was not reported with zero file rows, by design: {text!r}")
finally:
    try:
        proc.stdin.close()
    except Exception:
        pass
    proc.wait(timeout=5)

print("  ok: commit_detail caps and labels a truncated body, and reports a merge commit's zero "
      "file rows as by-design")
PY

echo "== librarian query: commit_detail - an ambiguous short hash lists the candidates rather than guessing =="
# A real SHA1 collision on a 7-character prefix (28 bits) cannot be produced by
# committing in a loop - the odds are astronomically against it. Ambiguity is
# tested directly against store.query()/commit_detail with two synthetic rows
# inserted through the store's own insert_commit() (the same function refresh
# and rebuild use), on a throwaway index that is never read by git at all.
LIB_CFG_AMBIG="$PWD/lib-cfg-ambiguous"
mkdir -p "$LIB_CFG_AMBIG"
python3 - "$LIBPATH" "$LIB_CFG_AMBIG" <<'PY'
import sys
libpath, repo = sys.argv[1], sys.argv[2]
sys.path.insert(0, libpath)
from librarian import store

conn = store.connect(repo)
for suffix, short in (("a" * 33, "1234567aaa"), ("b" * 33, "1234567bbb")):
    store.insert_commit(conn, {
        "hash": "1234567" + suffix, "short_hash": short, "author": "a", "author_email": "a@x",
        "date": "2026-01-01T00:00:00+00:00", "epoch": 1767225600, "subject": f"synthetic {short}",
        "body": "", "parents": [], "indexed_at": "2026-01-01T00:00:00+00:00",
    })
conn.commit()

q = store.query(conn, "commit_detail", {"hash": "1234567"}, repo)
conn.close()
if q.get("ok"):
    sys.exit(f"a 7-character prefix matching two synthetic commits was not rejected as ambiguous: {q}")
if "ambiguous" not in q.get("error", "").lower():
    sys.exit(f"the ambiguous-hash error did not say so: {q}")
missing = [s for s in ("1234567aaa", "1234567bbb") if s not in q["error"]]
if missing:
    sys.exit(f"the ambiguous-hash error did not name candidate(s) {missing}: {q['error']!r}")
print("  ok: an ambiguous short hash is rejected and names both colliding candidates, never guesses")
PY

echo "== librarian query: changes_with matches ground truth read straight from git log, module out of the loop =="
LIB_COCHANGE="$PWD/lib-cochange-truth"
mkdir -p "$LIB_COCHANGE"
gitc -C "$LIB_COCHANGE" init -q
echo 1 > "$LIB_COCHANGE/a.py"; gitc -C "$LIB_COCHANGE" add -A; gitc -C "$LIB_COCHANGE" commit -qm "add a"
echo 1 > "$LIB_COCHANGE/b.py"; gitc -C "$LIB_COCHANGE" add -A; gitc -C "$LIB_COCHANGE" commit -qm "add b"
printf '1\n2\n' > "$LIB_COCHANGE/a.py"; printf '1\n2\n' > "$LIB_COCHANGE/b.py"
gitc -C "$LIB_COCHANGE" add -A; gitc -C "$LIB_COCHANGE" commit -qm "a+b together 1"
printf '1\n2\n3\n' > "$LIB_COCHANGE/a.py"; printf '1\n2\n3\n' > "$LIB_COCHANGE/b.py"
gitc -C "$LIB_COCHANGE" add -A; gitc -C "$LIB_COCHANGE" commit -qm "a+b together 2"
printf '1\n2\n3\n4\n' > "$LIB_COCHANGE/a.py"; echo 1 > "$LIB_COCHANGE/c.py"
gitc -C "$LIB_COCHANGE" add -A; gitc -C "$LIB_COCHANGE" commit -qm "a+c together"
echo 1 > "$LIB_COCHANGE/unrelated.py"; gitc -C "$LIB_COCHANGE" add -A; gitc -C "$LIB_COCHANGE" commit -qm "unrelated 1"
echo 2 >> "$LIB_COCHANGE/unrelated.py"; gitc -C "$LIB_COCHANGE" add -A; gitc -C "$LIB_COCHANGE" commit -qm "unrelated 2"
echo 3 >> "$LIB_COCHANGE/unrelated.py"; gitc -C "$LIB_COCHANGE" add -A; gitc -C "$LIB_COCHANGE" commit -qm "unrelated 3"
printf '1\n2\n3\n4\n5\n' > "$LIB_COCHANGE/a.py"; gitc -C "$LIB_COCHANGE" add -A; gitc -C "$LIB_COCHANGE" commit -qm "a alone"
python3 - "$LIBPATH" "$LIB_COCHANGE" <<'PY'
# Ground truth is computed directly from `git log --name-only`, entirely
# independent of the librarian module - the only comparison worth making, per
# this repo's own doctrine (a module agreeing with itself proves nothing).
import collections, subprocess, sys

libpath, repo = sys.argv[1], sys.argv[2]
sys.path.insert(0, libpath)

log = subprocess.run(
    ["git", "-C", repo, "log", "--no-renames", "--name-only", "--format=COMMIT\t%H"],
    capture_output=True, text=True, check=True,
).stdout
by_hash = {}
cur = None
for line in log.splitlines():
    if line.startswith("COMMIT\t"):
        cur = line.split("\t", 1)[1]
        by_hash[cur] = set()
    elif line.strip():
        by_hash[cur].add(line.strip())

anchor_hashes = {h for h, files in by_hash.items() if "a.py" in files}
partner_shared = collections.Counter()
for h in anchor_hashes:
    for f in by_hash[h]:
        if f != "a.py":
            partner_shared[f] += 1
partner_total = collections.Counter()
for files in by_hash.values():
    for f in files:
        partner_total[f] += 1
expected_rows = sorted(partner_shared.items(), key=lambda kv: (-kv[1], kv[0]))

from librarian import history, store

r = history.index(repo, full=True)
if not r.get("ok"):
    sys.exit(f"initial index of the co-change ground-truth fixture failed: {r}")
conn = store.connect(repo)
q = store.query(conn, "changes_with", {"path": "a.py"}, repo)
conn.close()

got_rows = [(row["path"], row["shared_commits"]) for row in q["rows"]]
if got_rows != expected_rows:
    sys.exit(f"changes_with rows do not match ground truth from git log: module={got_rows}, "
              f"git={expected_rows}")
if q["anchor_commits"] != len(anchor_hashes):
    sys.exit(f"anchor_commits does not match git's own count: module={q['anchor_commits']}, "
              f"git={len(anchor_hashes)}")
for row in q["rows"]:
    exp_total = partner_total[row["path"]]
    if row["partner_commits"] != exp_total:
        sys.exit(f"partner_commits for {row['path']} does not match git's own count: "
                  f"module={row['partner_commits']}, git={exp_total}")

print(f"  ok: changes_with({{'path': 'a.py'}}) matches git log directly: {got_rows}")
PY

echo "== librarian query: co-change damping excludes a sweep at the default cap, and raising max_files brings it back =="
LIB_DAMP="$PWD/lib-cochange-damping"
mkdir -p "$LIB_DAMP"
gitc -C "$LIB_DAMP" init -q
echo 0 > "$LIB_DAMP/a.py"; gitc -C "$LIB_DAMP" add -A; gitc -C "$LIB_DAMP" commit -qm "add a"
echo 0 > "$LIB_DAMP/a_test.py"; gitc -C "$LIB_DAMP" add -A; gitc -C "$LIB_DAMP" commit -qm "add a_test"
# Push the considered-commit count comfortably above YOUNG_HISTORY (20) so the
# young-index caveat cannot contaminate this fixture's damping assertions.
for i in $(seq 1 18); do
  printf 'line %s\n' $(seq 1 "$((i + 1))") > "$LIB_DAMP/a.py"
  printf 'line %s\n' $(seq 1 "$((i + 1))") > "$LIB_DAMP/a_test.py"
  gitc -C "$LIB_DAMP" add -A
  gitc -C "$LIB_DAMP" commit -qm "a+a_test together $i"
done
echo 0 > "$LIB_DAMP/lonely.py"; gitc -C "$LIB_DAMP" add -A; gitc -C "$LIB_DAMP" commit -qm "lonely alone"
# the sweep: a.py plus 29 junk files in ONE commit - 30 files, over
# DEFAULT_MAX_COMMIT_FILES=25
echo sweep > "$LIB_DAMP/a.py"
for i in $(seq 1 29); do echo sweep > "$LIB_DAMP/junk$i.py"; done
gitc -C "$LIB_DAMP" add -A
gitc -C "$LIB_DAMP" commit -qm "mass reformat sweep"
python3 - "$LIBPATH" "$LIB_DAMP" <<'PY'
import sys
libpath, repo = sys.argv[1], sys.argv[2]
sys.path.insert(0, libpath)
from librarian import history, store

r = history.index(repo, full=True)
if not r.get("ok"):
    sys.exit(f"initial index of the damping fixture failed: {r}")
conn = store.connect(repo)

# direction 1: at the default cap, the sweep is excluded entirely
q_default = store.query(conn, "changes_with", {"path": "a.py"}, repo)
partners_default = {row["path"] for row in q_default["rows"]}
if any(p.startswith("junk") for p in partners_default):
    sys.exit(f"a junk file from the sweep appeared as a partner at the default cap: {partners_default}")
if "a_test.py" not in partners_default:
    sys.exit(f"a_test.py (the real co-change partner) is missing at the default cap: {partners_default}")
if q_default["damping"]["commits_skipped_too_broad"] != 1:
    sys.exit(f"expected exactly 1 commit skipped as too broad, got {q_default['damping']}")

# direction 2: raising max_files past the sweep size brings its edges back
q_raised = store.query(conn, "changes_with", {"path": "a.py", "max_files": 40}, repo)
partners_raised = {row["path"] for row in q_raised["rows"]}
junk_seen = {p for p in partners_raised if p.startswith("junk")}
if not junk_seen:
    sys.exit(f"raising max_files past the sweep size did not surface any junk partner: {partners_raised}")
if q_raised["damping"]["commits_skipped_too_broad"] != 0:
    sys.exit(f"expected 0 commits skipped once max_files covers the sweep, got {q_raised['damping']}")

conn.close()
print(f"  ok: default cap excludes the sweep ({len(partners_default)} partner(s), 1 skipped); "
      f"max_files=40 surfaces {len(junk_seen)} junk partner(s), 0 skipped")
PY

echo "== librarian query: coupling_between shows a too-broad shared commit rather than hiding it =="
python3 - "$LIBPATH" "$LIB_DAMP" <<'PY'
import sys
libpath, repo = sys.argv[1], sys.argv[2]
sys.path.insert(0, libpath)
from librarian import store

conn = store.connect(repo)
q = store.query(conn, "coupling_between", {"path": "a.py", "other_path": "junk1.py"}, repo)
conn.close()
if not q.get("ok"):
    sys.exit(f"coupling_between(a.py, junk1.py) reported an error: {q}")
if q["shared_commits"] != 1:
    sys.exit(f"expected exactly 1 shared commit between a.py and junk1.py, got {q['shared_commits']}")
if q["shared_counted"] != 0 or q["shared_too_broad"] != 1:
    sys.exit(f"expected shared_counted=0 shared_too_broad=1, got shared_counted={q['shared_counted']} "
              f"shared_too_broad={q['shared_too_broad']}")
if not q["rows"] or not q["rows"][0]["too_broad"]:
    sys.exit(f"the sweep commit was listed but not marked too_broad: {q['rows']}")
if not any("no edge at all" in c for c in q["caveats"]):
    sys.exit(f"missing the 'no edge at all' caveat when every shared commit is too broad: {q['caveats']}")
print("  ok: coupling_between lists the sweep commit and marks it too_broad, rather than hiding it")
PY

echo "== librarian query: changes_with's empty/annotated payloads stay distinguishable (unknown, damped-away, alone, young) =="
LIB_YOUNG="$PWD/lib-cochange-young"
mkdir -p "$LIB_YOUNG"
gitc -C "$LIB_YOUNG" init -q
echo 0 > "$LIB_YOUNG/x.py"; gitc -C "$LIB_YOUNG" add -A; gitc -C "$LIB_YOUNG" commit -qm "add x"
echo 0 > "$LIB_YOUNG/y.py"; gitc -C "$LIB_YOUNG" add -A; gitc -C "$LIB_YOUNG" commit -qm "add y"
echo 1 > "$LIB_YOUNG/x.py"; echo 1 > "$LIB_YOUNG/y.py"
gitc -C "$LIB_YOUNG" add -A; gitc -C "$LIB_YOUNG" commit -qm "x+y together"
python3 - "$LIBPATH" "$LIB_DAMP" "$LIB_YOUNG" <<'PY'
import sys
libpath, damp_repo, young_repo = sys.argv[1], sys.argv[2], sys.argv[3]
sys.path.insert(0, libpath)
from librarian import history, store

conn = store.connect(damp_repo)
q_unknown = store.query(conn, "changes_with", {"path": "does-not-exist.py"}, damp_repo)
q_damped = store.query(conn, "changes_with", {"path": "junk1.py"}, damp_repo)
q_alone = store.query(conn, "changes_with", {"path": "lonely.py"}, damp_repo)
conn.close()

texts = {
    "unknown": q_unknown.get("empty_reason") or "",
    "damped": q_damped.get("empty_reason") or "",
    "alone": q_alone.get("empty_reason") or "",
}
if q_unknown["rows"] or q_damped["rows"] or q_alone["rows"]:
    sys.exit(f"one of the three empty cases unexpectedly had rows: "
              f"unknown={q_unknown['rows']}, damped={q_damped['rows']}, alone={q_alone['rows']}")
if "no commit in the index touches" not in texts["unknown"]:
    sys.exit(f"unknown-path text wrong: {texts['unknown']!r}")
if "skipped as too broad" not in texts["damped"]:
    sys.exit(f"damped-away text wrong: {texts['damped']!r}")
if q_damped["anchor_commits_all"] != 1 or q_damped["anchor_commits"] != 0:
    sys.exit(f"junk1.py anchor counts wrong: all={q_damped['anchor_commits_all']} "
              f"considered={q_damped['anchor_commits']}")
if "nothing else changed in any of them" not in texts["alone"]:
    sys.exit(f"genuinely-alone text wrong: {texts['alone']!r}")
if len(set(texts.values())) != 3:
    sys.exit(f"the three empty_reason texts are not pairwise distinct: {texts}")
for name, q in (("unknown", q_unknown), ("damped", q_damped), ("alone", q_alone)):
    if any("little history" in c for c in q.get("caveats") or []):
        sys.exit(f"the young-index caveat leaked into the non-young '{name}' result: {q['caveats']}")

# the fourth, separately-triggered annotation: a genuinely young index
r2 = history.index(young_repo, full=True)
if not r2.get("ok"):
    sys.exit(f"initial index of the young fixture failed: {r2}")
conn2 = store.connect(young_repo)
q_young = store.query(conn2, "changes_with", {"path": "x.py"}, young_repo)
conn2.close()
if not q_young["rows"]:
    sys.exit(f"expected x.py/y.py to co-change at least once in the young fixture: {q_young}")
young_caveats = [c for c in q_young["caveats"] if "little history" in c]
if not young_caveats:
    sys.exit(f"a 3-commit index did not carry the young-history caveat: {q_young['caveats']}")
if young_caveats[0] in texts.values():
    sys.exit("the young-index caveat collided with one of the three empty_reason texts")

print("  ok: unknown path, damped-away path, genuinely-alone path and a young index are four "
      "distinct, non-colliding answers")
PY

echo "== librarian renderer: changes_with's caveats and evidence base survive into RENDERED text, and a weak row visibly differs from a strong one (over the real pipe) [watch-fail] =="
# Everything up to here compares store.query()'s row data directly - correct
# for ground truth, but it never once looks at the prose _render_cochange
# produces, which is the text a librarian agent actually reads. Reuses the
# LIB_DAMP fixture (a.py/a_test.py share 19 commits - strong; a.py/junk1.py
# share exactly 1 once max_files is raised - weak) so no new git history is
# needed. Caught failing first by commenting out _render_caveats/_render_damping's
# call sites in _render_cochange and confirming this block reported the exact
# missing strings, then restoring them.
python3 - "$LIBPATH" "$LIB_DAMP" <<'PY'
import json, subprocess, sys

libpath, proj = sys.argv[1], sys.argv[2]
server = libpath + "/teamme_mcp.py"


def send(proc, obj):
    proc.stdin.write(json.dumps(obj) + "\n")
    proc.stdin.flush()


def recv(proc):
    line = proc.stdout.readline()
    if not line:
        sys.exit(f"server closed the pipe unexpectedly; stderr: {proc.stderr.read()}")
    return json.loads(line)


def call_text(resp):
    result = resp.get("result") or {}
    return result, "".join(c.get("text", "") for c in result.get("content") or [])


def row_line(text, needle):
    return next((ln for ln in text.splitlines() if needle in ln), None)


proc = subprocess.Popen(
    ["python3", server],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    text=True, bufsize=1,
)
try:
    send(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18"}})
    recv(proc)

    send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "teamme_librarian_refresh", "arguments": {"project_dir": proj}}})
    result, text = call_text(recv(proc))
    if result.get("isError"):
        sys.exit(f"refresh of the shared damping fixture over the pipe reported an error: {text}")

    # the strong row, at the default cap: a.py / a_test.py, 19 shared commits
    send(proc, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                "params": {"name": "teamme_librarian_query",
                           "arguments": {"project_dir": proj, "query": "changes_with", "path": "a.py"}}})
    result, text_default = call_text(recv(proc))
    if result.get("isError"):
        sys.exit(f"changes_with(a.py) over the pipe reported an error: {text_default}")

    # the load-bearing honesty of the feature: a row rendered without its
    # denominators is exactly the bare ranking this design refuses to produce.
    must_contain = [
        "correlation, not a call graph",                  # header caveat, inline
        "evidence base:",                                  # the damping block
        "commit(s) considered of",
        "skipped as too broad",
        "reading this:",                                   # the caveats block header
        "CORRELATION, not a call graph",                   # caveat 1, verbatim
        "a row is marked `weak`",                           # caveat 3, verbatim (partial)
        "considered commit(s) of its own; overlap",         # per-row evidence, verbatim
    ]
    missing = [m for m in must_contain if m not in text_default]
    if missing:
        sys.exit(f"changes_with's rendered text is missing load-bearing caveat/evidence text: "
                  f"{missing}\n--- full text ---\n{text_default}")
    strong_line = row_line(text_default, "a_test.py")
    if strong_line is None:
        sys.exit(f"a_test.py did not appear as a partner at the default cap: {text_default!r}")
    if "WEAK" in strong_line:
        sys.exit(f"a_test.py (19 shared commits) was wrongly rendered WEAK: {strong_line!r}")

    # the weak row: raise max_files so junk1.py (1 shared commit) surfaces
    send(proc, {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                "params": {"name": "teamme_librarian_query",
                           "arguments": {"project_dir": proj, "query": "changes_with", "path": "a.py",
                                         "max_files": 40}}})
    result, text_raised = call_text(recv(proc))
    if result.get("isError"):
        sys.exit(f"changes_with(a.py, max_files=40) over the pipe reported an error: {text_raised}")
    weak_line = row_line(text_raised, "junk1.py")
    if weak_line is None or "WEAK - a single shared commit" not in weak_line:
        sys.exit(f"junk1.py (1 shared commit) was not rendered WEAK: {weak_line!r}")
    strong_line_raised = row_line(text_raised, "a_test.py")
    if strong_line_raised is None or "WEAK" in strong_line_raised:
        sys.exit(f"a_test.py (19 shared commits) was wrongly rendered WEAK once a weak partner "
                  f"also appeared: {strong_line_raised!r}")
finally:
    try:
        proc.stdin.close()
    except Exception:
        pass
    proc.wait(timeout=5)

print("  ok: changes_with's rendered text carries its caveats and evidence base, and visibly "
      "marks a 1-shared-commit row WEAK while leaving a 19-shared-commit row unmarked")
PY

echo "== librarian renderer: hotspots renders over the real pipe (previously unasserted) =="
python3 - "$LIBPATH" "$LIB_DAMP" <<'PY'
import json, subprocess, sys

libpath, proj = sys.argv[1], sys.argv[2]
server = libpath + "/teamme_mcp.py"


def send(proc, obj):
    proc.stdin.write(json.dumps(obj) + "\n")
    proc.stdin.flush()


def recv(proc):
    line = proc.stdout.readline()
    if not line:
        sys.exit(f"server closed the pipe unexpectedly; stderr: {proc.stderr.read()}")
    return json.loads(line)


def call_text(resp):
    result = resp.get("result") or {}
    return result, "".join(c.get("text", "") for c in result.get("content") or [])


proc = subprocess.Popen(
    ["python3", server],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    text=True, bufsize=1,
)
try:
    send(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18"}})
    recv(proc)

    send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "teamme_librarian_query",
                           "arguments": {"project_dir": proj, "query": "hotspots"}}})
    result, text = call_text(recv(proc))
    if result.get("isError"):
        sys.exit(f"hotspots over the pipe reported an error: {text}")
    if "most-changed paths in the repository - how often, not how important" not in text:
        sys.exit(f"hotspots' header did not render as expected: {text!r}")
    if "a_test.py" not in text or "19x" not in text:
        sys.exit(f"hotspots did not render a_test.py with its 19-commit count: {text!r}")
    if "evidence base:" not in text:
        sys.exit(f"hotspots did not render its damping evidence base: {text!r}")
finally:
    try:
        proc.stdin.close()
    except Exception:
        pass
    proc.wait(timeout=5)

print("  ok: hotspots renders a header, row counts and its evidence base over the real pipe")
PY

echo "== sessions librarian: fixtures live under a fresh subdirectory of the throwaway project, with their own fake \$CLAUDE_CONFIG_DIR/projects/<slug>/ - never this repo's own transcripts, which are private conversation =="
sess_slug_fixture() {
  # Writes a fake transcript directory for project dir $1 under config dir $2,
  # printing nothing; callers build the .jsonl themselves. Kept as a helper
  # rather than duplicated python in every block below.
  mkdir -p "$1"
}

echo "== sessions: [watch-fail] an oversized record (over MAX_LINE_BYTES) is skipped but COUNTED, offset advances past it - not parked in front of it forever =="
SESS_OVER="$PWD/sess-oversized"
SESS_OVER_CFG="$PWD/sess-oversized-cfg"
sess_slug_fixture "$SESS_OVER" "$SESS_OVER_CFG"
# The bug this catches: readline(n) caps at n bytes, so a record bigger than
# MAX_LINE_BYTES comes back without a trailing newline and looks exactly like a
# live (still-being-written) tail. Mistaking it for one means the byte offset
# never advances past it - PERMANENTLY - because every future refresh reads the
# same oversized bytes again and stops in the same place. Watched failing first
# below by forcing the "still being written" branch to fire unconditionally,
# instead of only when the read stopped short of the size cap.
python3 - "$LIBPATH" "$SESS_OVER" "$SESS_OVER_CFG" <<'PY'
import json, os, pathlib, sys

libpath, proj, cfgdir = sys.argv[1], sys.argv[2], sys.argv[3]
sys.path.insert(0, libpath)
from librarian import transcripts, sessions  # noqa

pathlib.Path(proj).mkdir(parents=True, exist_ok=True)
os.environ["CLAUDE_CONFIG_DIR"] = cfgdir
slug = transcripts.project_slug(proj)
sess_dir = pathlib.Path(cfgdir) / "projects" / slug
sess_dir.mkdir(parents=True, exist_ok=True)
transcript = sess_dir / "session-oversized.jsonl"


def rec_user(text, uuid, ts):
    return json.dumps({"type": "user", "uuid": uuid, "timestamp": ts,
                        "sessionId": "session-oversized", "promptSource": "typed",
                        "message": {"content": text}}, ensure_ascii=False)


big_text = "x" * (transcripts.MAX_LINE_BYTES + 1000)
line1 = rec_user(big_text, "u1", "2026-01-01T00:00:00Z")
line2 = rec_user("hello after the giant record", "u2", "2026-01-01T00:00:01Z")
with open(transcript, "wb") as fh:
    fh.write((line1 + "\n").encode("utf-8"))
    fh.write((line2 + "\n").encode("utf-8"))
file_size = transcript.stat().st_size

r = sessions.index(proj, full=True)
if not r.get("ok"):
    sys.exit(f"index failed: {r}")
if r.get("turns_added") != 1:
    sys.exit(f"expected exactly 1 turn added (oversized record skipped, not indexed): {r}")
notes_text = " ".join(r.get("notes") or [])
if "too large" not in notes_text:
    sys.exit(f"the oversized-record note was not reported: {r}")

conn, _ = sessions.connect_or_reset(proj)
row = conn.execute("SELECT bytes_indexed FROM sessions WHERE session_id = ?",
                    ("session-oversized",)).fetchone()
conn.close()
if row is None:
    sys.exit("session was not recorded in the index at all")
if row["bytes_indexed"] != file_size:
    sys.exit(f"offset did not advance past the oversized record: bytes_indexed={row['bytes_indexed']}, "
              f"file size={file_size} - this is the permanent-wedge bug")

r2 = sessions.index(proj)
if r2.get("turns_added"):
    sys.exit(f"a second refresh found new turns after the file was fully consumed: {r2}")

print(f"  ok: oversized record ({len(big_text)} bytes) skipped-but-counted, offset advanced past it "
      f"({row['bytes_indexed']} == {file_size} file bytes), the following turn was indexed, a second "
      f"refresh found nothing new")
PY

echo "== sessions: [watch-fail] a partial (no-newline) tail line is left unconsumed, and completed whole on the next refresh =="
SESS_PARTIAL="$PWD/sess-partial"
SESS_PARTIAL_CFG="$PWD/sess-partial-cfg"
sess_slug_fixture "$SESS_PARTIAL" "$SESS_PARTIAL_CFG"
# Getting this wrong either loses a turn (consuming the incomplete bytes, then
# skipping past the real content once the write finishes) or double-counts one
# (re-reading from the wrong offset). Watched failing first below by disabling
# the "no trailing newline -> do not consume" branch entirely, which produced
# exactly the premature-consume failure this test exists to catch.
python3 - "$LIBPATH" "$SESS_PARTIAL" "$SESS_PARTIAL_CFG" <<'PY'
import json, os, pathlib, sys

libpath, proj, cfgdir = sys.argv[1], sys.argv[2], sys.argv[3]
sys.path.insert(0, libpath)
from librarian import transcripts, sessions  # noqa

pathlib.Path(proj).mkdir(parents=True, exist_ok=True)
os.environ["CLAUDE_CONFIG_DIR"] = cfgdir
slug = transcripts.project_slug(proj)
sess_dir = pathlib.Path(cfgdir) / "projects" / slug
sess_dir.mkdir(parents=True, exist_ok=True)
transcript = sess_dir / "session-partial.jsonl"


def rec_user(text, uuid, ts):
    return json.dumps({"type": "user", "uuid": uuid, "timestamp": ts,
                        "sessionId": "session-partial", "promptSource": "typed",
                        "message": {"content": text}}, ensure_ascii=False)


complete1 = rec_user("first complete turn", "u1", "2026-01-01T00:00:00Z")
complete2 = rec_user("second complete turn", "u2", "2026-01-01T00:00:01Z")
partial = rec_user("a turn still being typed", "u3", "2026-01-01T00:00:02Z")
with open(transcript, "wb") as fh:
    fh.write((complete1 + "\n").encode("utf-8"))
    fh.write((complete2 + "\n").encode("utf-8"))
    fh.write(partial.encode("utf-8"))  # deliberately no trailing newline

size_before = transcript.stat().st_size
expected_offset = len((complete1 + "\n" + complete2 + "\n").encode("utf-8"))

r = sessions.index(proj, full=True)
if not r.get("ok"):
    sys.exit(f"index failed: {r}")
if r.get("turns_added") != 2:
    sys.exit(f"expected exactly the 2 complete turns, the partial tail must be left unconsumed: {r}")
if "still being written" not in " ".join(r.get("notes") or []):
    sys.exit(f"the partial-tail note was not reported: {r}")

conn, _ = sessions.connect_or_reset(proj)
row = conn.execute("SELECT bytes_indexed FROM sessions WHERE session_id = ?",
                    ("session-partial",)).fetchone()
conn.close()
if row["bytes_indexed"] != expected_offset:
    sys.exit(f"byte offset moved past the incomplete line: bytes_indexed={row['bytes_indexed']}, "
              f"expected {expected_offset} (only the two complete lines)")
if row["bytes_indexed"] >= size_before:
    sys.exit("offset was not left short of the file - the partial line was wrongly consumed")

r2 = sessions.index(proj)
if r2.get("turns_added"):
    sys.exit(f"a second refresh over an unchanged partial tail produced new turns out of nowhere: {r2}")

with open(transcript, "ab") as fh:
    fh.write(b"\n")
    fh.write((rec_user("fourth turn, after completion", "u4", "2026-01-01T00:00:03Z") + "\n")
              .encode("utf-8"))

r3 = sessions.index(proj)
if not r3.get("ok") or r3.get("turns_added") != 2:
    sys.exit(f"completing the partial line plus one new turn should add exactly 2 turns: {r3}")

conn, _ = sessions.connect_or_reset(proj)
final_size = transcript.stat().st_size
row2 = conn.execute("SELECT bytes_indexed, turns FROM sessions WHERE session_id = ?",
                     ("session-partial",)).fetchone()
conn.close()
if row2["bytes_indexed"] != final_size:
    sys.exit(f"offset did not reach end of file after completion: {row2['bytes_indexed']} != {final_size}")
if row2["turns"] != 4:
    sys.exit(f"expected 4 total turns after completion (no loss, no double-count), got {row2['turns']}")

print(f"  ok: partial (no-newline) tail left unconsumed at offset {expected_offset} (the two complete "
      f"lines only), and completed on the next refresh with no loss or double-count (4 total turns)")
PY

echo "== sessions: [watch-fail] a transcript replaced with different content at the SAME SIZE triggers a full reindex, not a stale-offset resume =="
SESS_FP="$PWD/sess-fingerprint"
SESS_FP_CFG="$PWD/sess-fingerprint-cfg"
sess_slug_fixture "$SESS_FP" "$SESS_FP_CFG"
# Same structural lesson as history's unreachable-marker case: a size check
# alone cannot see this, because the replacement is deliberately the same
# size. Watched failing first below by disabling the fingerprint-mismatch
# branch, which produced the silent, permanent loss this test exists to catch
# - the replacement was treated as "nothing new" and its content never
# indexed at all, invisibly.
python3 - "$LIBPATH" "$SESS_FP" "$SESS_FP_CFG" <<'PY'
import json, os, pathlib, sys

libpath, proj, cfgdir = sys.argv[1], sys.argv[2], sys.argv[3]
sys.path.insert(0, libpath)
from librarian import transcripts, sessions  # noqa

pathlib.Path(proj).mkdir(parents=True, exist_ok=True)
os.environ["CLAUDE_CONFIG_DIR"] = cfgdir
slug = transcripts.project_slug(proj)
sess_dir = pathlib.Path(cfgdir) / "projects" / slug
sess_dir.mkdir(parents=True, exist_ok=True)
transcript = sess_dir / "session-replaced.jsonl"


def rec_user(text, uuid, ts):
    return json.dumps({"type": "user", "uuid": uuid, "timestamp": ts,
                        "sessionId": "session-replaced", "promptSource": "typed",
                        "message": {"content": text}}, ensure_ascii=False)


def rec_assistant(text, uuid, ts):
    return json.dumps({"type": "assistant", "uuid": uuid, "timestamp": ts,
                        "sessionId": "session-replaced",
                        "message": {"content": text, "model": "claude-test"}}, ensure_ascii=False)


def build(marker, pad=0):
    lines = [
        rec_user(f"prompt about {marker}", f"u-{marker}", "2026-01-01T00:00:00Z"),
        rec_assistant("answer " + ("z" * pad) + f" mentioning {marker}", f"a-{marker}", "2026-01-01T00:00:01Z"),
    ]
    return "\n".join(lines) + "\n"


old_text = build("OLDMARKERXYZ")
new_text = build("NEWMARKERXYZ")
diff = len(old_text.encode("utf-8")) - len(new_text.encode("utf-8"))
if diff > 0:
    new_text = build("NEWMARKERXYZ", pad=diff)
elif diff < 0:
    old_text = build("OLDMARKERXYZ", pad=-diff)
old_bytes, new_bytes = old_text.encode("utf-8"), new_text.encode("utf-8")
if len(old_bytes) != len(new_bytes):
    sys.exit(f"fixture bug: could not equalize sizes ({len(old_bytes)} vs {len(new_bytes)})")

transcript.write_bytes(old_bytes)
r1 = sessions.index(proj, full=True)
if not r1.get("ok") or r1.get("turns_added") != 2:
    sys.exit(f"initial index of the old content failed: {r1}")

conn, _ = sessions.connect_or_reset(proj)
old_row = conn.execute("SELECT fingerprint FROM sessions WHERE session_id = ?",
                        ("session-replaced",)).fetchone()
conn.close()
old_fp = old_row["fingerprint"]

transcript.write_bytes(new_bytes)
if transcript.stat().st_size != len(old_bytes):
    sys.exit("fixture bug: replacement file is not the same size as the original")

r2 = sessions.index(proj)
if not r2.get("ok"):
    sys.exit(f"index after same-size replacement failed: {r2}")
if "reindexed in full" not in " ".join(r2.get("notes") or []):
    sys.exit(f"a same-size content replacement was not reported as a full reindex: {r2}")
if r2.get("turns_added") != 2:
    sys.exit(f"expected the full new content (2 turns) after a fingerprint mismatch, got: {r2}")

conn, _ = sessions.connect_or_reset(proj)
new_row = conn.execute("SELECT bytes_indexed, fingerprint, turns FROM sessions WHERE session_id = ?",
                        ("session-replaced",)).fetchone()
q_old = sessions.query(conn, "search_turns", {"text": "OLDMARKERXYZ"}, proj)
q_new = sessions.query(conn, "search_turns", {"text": "NEWMARKERXYZ"}, proj)
conn.close()

if new_row["fingerprint"] == old_fp:
    sys.exit("fingerprint did not change even though the file's first record did")
if new_row["turns"] != 2:
    sys.exit(f"turns were not replaced cleanly (expected 2, no doubling), got {new_row['turns']}")
if q_old["count"] != 0:
    sys.exit(f"the OLD content is still searchable after the file was replaced: {q_old}")
if q_new["count"] < 1:
    sys.exit(f"the NEW content is not searchable after the replacement was indexed: {q_new}")

print(f"  ok: a same-size content replacement was detected by fingerprint (not by size), forced a full "
      f"reindex ({new_row['bytes_indexed']} bytes, still {new_row['turns']} turns - no doubling), old "
      f"content unsearchable, new content found")
PY

echo "== sessions librarian config: privacy is ENACTED not documented - commit_record=true (and false) leave .claude/librarians/sessions/ git-ignored, checked with git check-ignore as ground truth =="
SESS_PRIV="$PWD/sess-privacy"
mkdir -p "$SESS_PRIV"
gitc -C "$SESS_PRIV" init -q
echo one > "$SESS_PRIV/f.txt"; gitc -C "$SESS_PRIV" add f.txt; gitc -C "$SESS_PRIV" commit -qm "c1"
python3 - "$ROOT" "$SESS_PRIV" <<'PY'
import json, pathlib, subprocess, sys

root, proj = pathlib.Path(sys.argv[1]), sys.argv[2]
server = root / "plugins/teamme/server/teamme_mcp.py"
SESSIONS_DB = ".claude/librarians/sessions/index.db"
SESSIONS_DIR = ".claude/librarians/sessions/"


def send(proc, obj):
    proc.stdin.write(json.dumps(obj) + "\n")
    proc.stdin.flush()


def recv(proc):
    line = proc.stdout.readline()
    if not line:
        sys.exit(f"server closed the pipe unexpectedly; stderr: {proc.stderr.read()}")
    return json.loads(line)


def call_text(resp):
    result = resp.get("result") or {}
    return result, "".join(c.get("text", "") for c in result.get("content") or [])


def ignored(path: str) -> bool:
    r = subprocess.run(["git", "check-ignore", "-q", path], cwd=proj)
    if r.returncode not in (0, 1):
        sys.exit(f"git check-ignore errored (rc={r.returncode}) on {path}")
    return r.returncode == 0


proc = subprocess.Popen(
    ["python3", str(server)],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    text=True, bufsize=1,
)
try:
    send(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18"}})
    recv(proc)

    # commit_record=true matters most: flipping an UNRELATED setting (whether
    # to commit the git-history record) must never make the session index
    # committable.
    send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "teamme_librarian_configure",
                           "arguments": {"project_dir": proj, "commit_record": True}}})
    result, text = call_text(recv(proc))
    if result.get("isError"):
        sys.exit(f"commit_record=true reported an error: {text}")
    if not ignored(SESSIONS_DB):
        sys.exit("git check-ignore says .claude/librarians/sessions/index.db is NOT ignored with "
                  "commit_record=true - a conversation index must never become committable")
    if not ignored(SESSIONS_DIR):
        sys.exit("git check-ignore says .claude/librarians/sessions/ is NOT ignored with commit_record=true")

    send(proc, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                "params": {"name": "teamme_librarian_configure",
                           "arguments": {"project_dir": proj, "commit_record": False}}})
    result, text = call_text(recv(proc))
    if result.get("isError"):
        sys.exit(f"commit_record=false reported an error: {text}")
    if not ignored(SESSIONS_DB):
        sys.exit("git check-ignore says the session index is NOT ignored with commit_record=false")
finally:
    try:
        proc.stdin.close()
    except Exception:
        pass
    proc.wait(timeout=5)

print("  ok: .claude/librarians/sessions/ stays git-ignored under commit_record=true AND false "
      "(git check-ignore is ground truth, not the .gitignore text)")
PY

echo "== sessions: the retrieval contract - a window with a huge before/after truncates with its notice, never dumps unbounded text =="
SESS_WIN="$PWD/sess-window"
SESS_WIN_CFG="$PWD/sess-window-cfg"
sess_slug_fixture "$SESS_WIN" "$SESS_WIN_CFG"
python3 - "$LIBPATH" "$SESS_WIN" "$SESS_WIN_CFG" <<'PY'
import json, os, pathlib, sys

libpath, proj, cfgdir = sys.argv[1], sys.argv[2], sys.argv[3]
sys.path.insert(0, libpath)
from librarian import transcripts, sessions  # noqa

pathlib.Path(proj).mkdir(parents=True, exist_ok=True)
os.environ["CLAUDE_CONFIG_DIR"] = cfgdir
slug = transcripts.project_slug(proj)
sess_dir = pathlib.Path(cfgdir) / "projects" / slug
sess_dir.mkdir(parents=True, exist_ok=True)
transcript = sess_dir / "session-window.jsonl"


def rec(kind, text, uuid, ts):
    if kind == "user":
        return json.dumps({"type": "user", "uuid": uuid, "timestamp": ts,
                            "sessionId": "session-window", "promptSource": "typed",
                            "message": {"content": text}}, ensure_ascii=False)
    return json.dumps({"type": "assistant", "uuid": uuid, "timestamp": ts,
                        "sessionId": "session-window",
                        "message": {"content": text, "model": "claude-test"}}, ensure_ascii=False)


N = 60
lines = []
for i in range(N):
    kind = "user" if i % 2 == 0 else "assistant"
    text = f"turn {i} " + ("y" * 3000)  # over MAX_WINDOW_TURN_CHARS (2000) per turn
    lines.append(rec(kind, text, f"t{i}", f"2026-01-01T00:{i:02d}:00Z"))
transcript.write_text("\n".join(lines) + "\n", encoding="utf-8")

r = sessions.index(proj, full=True)
if not r.get("ok") or r.get("turns_added") != N:
    sys.exit(f"fixture indexing failed: {r}")

conn, _ = sessions.connect_or_reset(proj)
anchor = N // 2
q = sessions.query(conn, "window",
                    {"session": "session-window", "seq": anchor, "before": 999999, "after": 999999}, proj)
conn.close()
if not q.get("ok"):
    sys.exit(f"window query failed: {q}")

MAX_WINDOW, MAX_TOTAL, MAX_TURN = sessions.MAX_WINDOW, sessions.MAX_WINDOW_TOTAL_CHARS, sessions.MAX_WINDOW_TURN_CHARS
lo, hi = q["asked_range"]
if hi - lo > 2 * MAX_WINDOW:
    sys.exit(f"a huge before/after was not clamped to MAX_WINDOW ({MAX_WINDOW}): asked_range={q['asked_range']}")
if q["count"] > 2 * MAX_WINDOW + 1:
    sys.exit(f"window returned more rows than MAX_WINDOW allows either side: count={q['count']}")
if q["chars"] > MAX_TOTAL:
    sys.exit(f"window returned more characters than MAX_WINDOW_TOTAL_CHARS ({MAX_TOTAL}): chars={q['chars']}")
if not q["truncated"]:
    sys.exit(f"a huge before/after against oversized turns did not report truncated=True: {q}")
for row in q["rows"]:
    if len(row["text"]) > MAX_TURN:
        sys.exit(f"a per-turn cap was exceeded: seq={row['seq']} len={len(row['text'])} > {MAX_TURN}")
if q["count"] >= N:
    sys.exit(f"window returned (almost) the entire session ({q['count']} of {N} turns) - unbounded, "
              f"exactly the dump this query exists to prevent")

print(f"  ok: window({{before: 999999, after: 999999}}) truncated to {q['count']} row(s), {q['chars']} "
      f"chars (caps: {MAX_TOTAL} total / {MAX_TURN} per turn / {MAX_WINDOW} turns either side), "
      f"truncated=True, out of {N} turns actually on disk")
PY

echo "== sessions: honest degradation - no \$CLAUDE_CONFIG_DIR and no ~/.claude returns a named error payload, never an exception =="
SESS_NOHOME="$PWD/sess-nohome"
SESS_NOHOME_HOME="$PWD/sess-nohome-home"
mkdir -p "$SESS_NOHOME_HOME"
# Isolated from whoever's machine this runs on, same reasoning as the preflight
# isolation above: CLAUDE_CONFIG_DIR truly UNSET (not pointed at an empty dir)
# and HOME pointed at a directory that genuinely has no .claude under it, so
# the double-negative in the brief is real rather than accidental.
nohome_env() { env -u CLAUDE_CONFIG_DIR -u CLAUDE_PLUGIN_ROOT HOME="$SESS_NOHOME_HOME" "$@"; }
nohome_env python3 - "$LIBPATH" "$SESS_NOHOME" <<'PY'
import pathlib, sys

libpath, proj = sys.argv[1], sys.argv[2]
sys.path.insert(0, libpath)
from librarian import sessions, transcripts  # noqa

pathlib.Path(proj).mkdir(parents=True, exist_ok=True)

r = sessions.index(proj, full=True)
if r.get("ok"):
    sys.exit(f"index() with no CLAUDE_CONFIG_DIR and no ~/.claude reported ok=True: {r}")
if not r.get("error"):
    sys.exit(f"index() failed silently with no error message: {r}")

st = sessions.status(proj)
if not st.get("error"):
    sys.exit(f"status() did not report an error with no config directory reachable: {st}")
if not st.get("searched"):
    sys.exit(f"status() did not report what it searched: {st}")

where = transcripts.locate(proj)
if where.get("ok"):
    sys.exit(f"locate() claimed success with nowhere to look: {where}")
if not where.get("problem"):
    sys.exit(f"locate() gave no reason: {where}")
if not where.get("searched"):
    sys.exit(f"locate() did not report the paths it looked in: {where}")

print("  ok: no $CLAUDE_CONFIG_DIR and no ~/.claude degrades to a named error payload in index(), "
      "status() and locate() - no exception, and every result names what it searched")
PY


cd "$ROOT"
echo "ALL CHECKS PASSED"
