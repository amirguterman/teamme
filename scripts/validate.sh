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
