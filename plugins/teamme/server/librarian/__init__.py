"""The librarian substrate: an append-only text ledger plus a disposable SQLite index.

Phase 1 ships the machinery only - a store and a git history indexer, both
invoked by teamme's MCP server. Deliberately NOT copied into a target project:
nothing here joins the hook scripts an install is required to have, so adding a
librarian later can never grow that list again (see CLAUDE.md, "installed must
never be derived from a hook list that grows").

Two artifacts, per project, created at runtime:

  .claude/librarians/history/commits.jsonl   append-only text - the committable
                                             artifact, one JSON record per line
  .claude/librarians/index.db                SQLite - ALWAYS gitignored, always
                                             disposable
  .claude/librarians/config.json             which librarians are enabled here,
                                             and whether the record is committed

The whole design rests on one property: `store.rebuild()` reconstructs the
entire database from the JSONL alone, with no git access. That is what makes the
.db disposable, and it is what lets a project choose whether to commit its
librarian data (commit the JSONL) or keep it alongside the code uncommitted
(gitignore the JSONL too). The .db is never committed either way - a binary file
cannot be merged, and two teammates indexing different commits produce
irreconcilable files.

A second librarian, `sessions`, indexes this project's CONVERSATIONS rather than
its code, from the transcripts the harness already writes. It diverges on one
point deliberately: it keeps no JSONL record, because the transcript is the
record and a second text copy of a conversation would double a private surface
for nothing. `.claude/librarians/sessions/` is therefore one disposable SQLite
index, always gitignored, never a choice.

  .claude/librarians/sessions/index.db       derived; a full reindex rebuilds it
                                             from the transcripts

Stdlib only: sqlite3, subprocess, json, os. Same bare-machine assumption as the
hooks and the MCP server.
"""

__all__ = ["store", "history", "config", "sessions", "transcripts"]
