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
cd "$ROOT"
echo "ALL CHECKS PASSED"
