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
