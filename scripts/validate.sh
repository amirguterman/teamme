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
        if not c.read_text().startswith("---"):
            sys.exit(f"{c.relative_to(root)}: missing YAML frontmatter")
    print(f"  ok: {p['name']} v{p.get('version','?')} - {len(cmds)} command(s)")
PY

echo "== hook syntax =="
python3 -m py_compile plugins/*/templates/hooks/*.py
echo "  ok: all hooks compile"

echo "== mcp server syntax =="
python3 -m py_compile plugins/*/server/*.py
echo "  ok: the mcp server compiles"

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
set +e
env -u CLAUDE_PLUGIN_ROOT python3 "$ISO_PF" check --project-dir "$STALE_FIXTURE" >/dev/null
ISO_RC=$?
set -e
[ "$ISO_RC" -eq 0 ] || fail "preflight exited $ISO_RC on a stale-but-otherwise-healthy install once its templates were unreachable (must degrade to existence-only and PASS)"
ISO_JSON_FILE="$PWD/pf-degraded.json"
env -u CLAUDE_PLUGIN_ROOT python3 "$ISO_PF" check --json --project-dir "$STALE_FIXTURE" > "$ISO_JSON_FILE"
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
"
echo "  ok: templates unreachable from a project -> freshness not verified, hooks check still PASSES"

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

cd "$ROOT"
echo "ALL CHECKS PASSED"
