#!/usr/bin/env python3
"""PreToolUse hook: say so when a push would share work the history index misses.

The history index is what a librarian answers from. It goes stale silently: ten
commits land, they are pushed, and a teammate's librarian keeps answering
confidently from an index that has never heard of them. Nothing anywhere says
"what you are about to share is ahead of what the index knows". This hook says
it, at the one moment it is actionable - the push.

IT ASKS, AND IT NEVER DENIES. There is no code path here that emits `deny`: a
stale index is a housekeeping matter, and a hotfix at 3am must never be blocked
by one. Approving the prompt proceeds exactly as if this hook did not exist.

It stays silent unless it is CONFIDENT. Every one of these allows with no
output: the command is not recognizably a push, no index exists here, the
history librarian is switched off, the marker is missing or unreadable, the
marker is not reachable from HEAD (rebase, force-push, shallow clone - the count
would then be meaningless, and a confidently wrong number is worse than
silence), git is missing or slow, or the payload is anything other than what was
expected. A project that never asked for a librarian is never nagged.

What is indexed is read from one line of text - `.claude/librarians/history/
indexed_head`, written by the indexer beside its own record - not from the
SQLite index. This script is COPIED into a project; the librarian modules are
not, so it could only read that database by carrying a second copy of the
schema, which would drift from the first.
"""

import json
import os
import pathlib
import re
import shlex
import subprocess
import sys

HEX = re.compile(r"^[0-9a-f]{7,64}$")

MARKER_REL = (".claude", "librarians", "history", "indexed_head")
CONFIG_REL = (".claude", "librarians", "config.json")

GIT_TIMEOUT = 4          # a hook in someone's editing loop; a slow git is an allow.
                         # Two calls at most, so the worst case stays inside the 10s
                         # the settings template gives this hook - a hook the harness
                         # kills is silent too, but arriving at silence on purpose is
                         # better than arriving there by being shot.
MAX_COMMAND_CHARS = 8000  # past this, classification is guesswork - allow instead

# git's own options that swallow the next token. If one of these appears before
# the subcommand, the token after it is its value, not the subcommand. Anything
# NOT on this list and not a recognized flag makes the segment unclassifiable,
# and unclassifiable means "not a push" - see is_push().
GIT_OPTS_WITH_VALUE = ("-C", "-c", "--git-dir", "--work-tree", "--namespace",
                       "--exec-path", "--config-env", "--super-prefix")
GIT_FLAGS = ("--no-pager", "--paginate", "-P", "--no-replace-objects", "--bare",
             "--literal-pathspecs", "--glob-pathspecs", "--noglob-pathspecs",
             "--icase-pathspecs", "--no-optional-locks", "--no-lazy-fetch",
             "--no-advice")


def is_push(command) -> bool:
    """Is this command, confidently, a `git push`?

    Conservative in one direction on purpose: a command this cannot classify is
    NOT a push, and the hook stays silent. Missing a push costs a reminder that
    did not appear; a false match costs an interruption on a command that has
    nothing to do with the index, which is how a well-meant guard becomes noise
    someone turns off. So `echo "git push"` (the words are an argument, not a
    command), `git pushd`, and `bash -c "git push"` (the push is inside a string
    this deliberately does not re-parse) are all not-a-push.

    Handled: plain `git push`, any amount of whitespace, `git -C <dir> push`,
    leading environment assignments, an absolute path to git, a push in a
    compound command (`cd x && git push`, `a; git push`, `a | git push`) or
    inside a substitution, and `--dry-run` - which is still a push, because the
    point is the reminder, not the transfer.
    """
    if not isinstance(command, str) or "push" not in command:
        return False
    if len(command) > MAX_COMMAND_CHARS:
        return False
    try:
        lex = shlex.shlex(command, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        tokens = list(lex)
    except Exception:
        return False  # unbalanced quotes, or anything else shlex dislikes

    punctuation = set("();<>|&")
    segment = []
    for token in tokens + [";"]:
        if token and set(token) <= punctuation:
            if _segment_is_push(segment):
                return True
            segment = []
        else:
            segment.append(token)
    return False


def _segment_is_push(tokens) -> bool:
    i = 0
    while i < len(tokens) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[i]):
        i += 1  # leading environment assignments: FOO=bar git push
    if i >= len(tokens):
        return False
    if os.path.basename(tokens[i]) != "git":
        return False
    i += 1
    while i < len(tokens):
        token = tokens[i]
        if not token.startswith("-"):
            return token == "push"          # the subcommand, whatever it is
        if "=" in token or token in GIT_FLAGS:
            i += 1
            continue
        if token in GIT_OPTS_WITH_VALUE:
            i += 2                          # the next token is its value
            continue
        return False                        # an option we cannot account for
    return False


def history_enabled(root) -> bool:
    """Whether the history librarian is on for this project.

    An absent config means on (that is the librarian's documented default). A
    config that cannot be read means SILENT, which is the opposite of what the
    librarian module does with the same file - it degrades to enabled. Both are
    fail-open for their own caller: the module must not let a broken file switch
    a librarian off, and this hook must not let one produce a prompt.
    """
    path = root.joinpath(*CONFIG_REL)
    if not path.is_file():
        return True
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        entry = (raw.get("librarians") or {}).get("history")
    except Exception:
        return False
    if isinstance(entry, bool):
        return entry
    if isinstance(entry, dict):
        flag = entry.get("enabled", True)
        return flag if isinstance(flag, bool) else False
    if entry is None:
        return True
    return False


def git(root, args):
    """(returncode, stdout). Never raises; a timeout is a non-zero return."""
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_PAGER"] = "cat"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env.pop("GIT_DIR", None)
    env.pop("GIT_WORK_TREE", None)
    try:
        proc = subprocess.run(
            ["git"] + list(args), cwd=str(root), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=GIT_TIMEOUT,
        )
    except Exception:
        return (1, "")
    return (proc.returncode, (proc.stdout or b"").decode("utf-8", "replace").strip())


def commits_behind(root, marker):
    """How many commits HEAD has that the marker does not. None if unanswerable.

    The pathspec exclusion is not a nicety, it is what stops the gate re-arming
    itself. With commit_record on, the refresh this hook asks for writes
    commits.jsonl, and the commit carrying that file is itself unindexed - so the
    very act of clearing the gate would arm it again, and it could never be
    cleared. Same shape as stamping the work-log reminder against status_changed
    rather than updated: an enforcement hook must not react to its own effect.
    """
    rc, out = git(root, ["rev-list", "--count", f"{marker}..HEAD",
                         "--", ".", ":!.claude/librarians"])
    if rc != 0:
        return None
    try:
        return int(out)
    except Exception:
        return None


def reason(count, marker) -> str:
    return (
        f"{count} commit(s) on HEAD are not in this project's history index (it was last "
        f"indexed at {marker[:12]}). Pushing shares work the librarian cannot answer about, "
        f"and it will keep answering confidently from what it has. Refresh it with the "
        f"teamme_librarian_refresh MCP tool ({{\"librarian\": \"history\"}}), then push again. "
        f"Approving proceeds with the push as it is - this is a reminder, never a block."
    )


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return
    if not isinstance(payload, dict):
        return
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return
    if not is_push(tool_input.get("command")):
        return

    try:
        root = pathlib.Path(os.environ.get("CLAUDE_PROJECT_DIR") or ".").resolve()
        marker_file = root.joinpath(*MARKER_REL)
        if not marker_file.is_file():
            return  # no index here: a project that never asked for one is not nagged
        if not history_enabled(root):
            return
        marker = marker_file.read_text(encoding="utf-8").strip()
        if not HEX.match(marker):
            return
        # Unreachable from HEAD - rebased, force-pushed, or a shallow clone. The
        # count would be meaningless, and the next refresh already falls back to
        # a full reindex and says so. Silence beats a confident wrong number.
        rc, _ = git(root, ["merge-base", "--is-ancestor", marker, "HEAD"])
        if rc != 0:
            return
        count = commits_behind(root, marker)
        if not count:       # 0, or None for unanswerable
            return
        out = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "ask",
                "permissionDecisionReason": reason(count, marker),
            }
        }
    except Exception:
        return  # fail open: a broken reminder must never interrupt a push
    json.dump(out, sys.stdout)


if __name__ == "__main__":
    main()
