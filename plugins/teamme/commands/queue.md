---
description: Park a request in the work log for later, without running intake. Records one task, prints one line, stops - no grounding, no analysis, no brief, no questions.
argument-hint: <what to park for later> [P0|P1|P2]
---

The request: **$ARGUMENTS**

This is the cheap path into the work log. Parking something must cost the user nothing: one command,
one line back, no thinking out loud. Anything more and they will stop parking things and start
dropping them.

You are **not** triaging this request, grounding it, or deciding whether it should happen. That
judgement belongs to `/intake` on the day the task is picked up. Here you are a clerk.

If `$ARGUMENTS` is empty, ask what to queue and stop. That is the only case in which you say
anything other than the one line below.

## 1. Record it

Take the request text verbatim as the title, minus a trailing priority token. Escape any `"` in it
before it goes into the shell:

| The argument | The command |
|---|---|
| ends in a bare `P0`, `P1` or `P2` | strip that token, pass the rest as the title, pass the token as `--priority` |
| anything else | the whole argument is the title; priority is `P1`, the same default `/intake`'s Queue row uses |

```bash
python3 .claude/hooks/worklog.py add "<title>" --priority <P0|P1|P2>
```

Use the `teamme_worklog` MCP tool with `action: add` instead if it is available - same effect, same
one-line reply. Leave the task at status `open`: it is parked in the queue, not deferred or declined.
Do not set a lane, do not add notes, do not re-prioritize anything else.

## 2. Reply with exactly one line

The task id and the title, as the work log recorded them:

```
T12 queued (P1) - add a dark mode toggle
```

That is the whole response. This is a hard output constraint, not a style preference - the failure
mode is a model that helpfully explains what it just queued. So:

- No preamble, no summary, no restatement of the request in your own words.
- No analysis, no plan, no file paths, no estimate of effort, no lane guess.
- No "let me know if you'd like me to start on this" and no follow-up question.
- Do not offer to run `/intake` on it. The user parked it on purpose.

Two exceptions, each still one line: an empty argument (ask for the request) and a failed `add`. If
`worklog.py` is missing or the command exits non-zero, say so in one line and name the fix -
`queue: teamme is not installed here - run /teamme:init-team.` Do not run the preflight check, and do
not attempt a repair from here.

## 3. Touch nothing else

Do not read the spec, the README or any project file. Do not run `intake-state.py` in any form -
not `show`, not `begin`, not `release`. This command works identically whether the intake phase is
`idle`, `grounding` or `approved`, precisely because it never reads or moves that lock: a queued
request is a note for later, so it can never disturb work in flight or start a brief of its own.
`worklog.py add` is the only state this command changes.
