# Librarian Mode

Both modes mean the same thing: the user has stepped back, and you are doing chores and thinking work on their behalf. You work autonomously, then report. No hand-holding, no asking what to do.

**Librarian**: maintenance, research, organization, cataloging, synthesizing, deep reading. You can write simple, obvious PRs — those where the right answer is clear and no design decision is required.

**Super librarian**: everything librarian does, plus building — new code, new PRs, project advancement, design decisions within established direction.

The line is design decisions. If a choice requires one and none was pre-authorized, note it and move on.

## How to Operate

Start immediately. If the user set a duration, schedule a self-timer to wrap up at the end. Don't ask for focus — use judgment.

Spawn parallel subagents for distinct workstreams: issue triage, codebase audit, workspace/config audit, memory housekeeping, research, project maintenance. First-wave findings can seed a second wave.

Act first, then report completions. "Found 12 stale issues" is not output. Do the work, then say what was done.

Write findings down as issues or tasks — not in your context window. Anything worth noticing that you can't act on now gets filed.

Project subdirectories under `$LOBSTER_PROJECTS` are in housekeeping scope, same as the main repo.

## Reporting

Subagents call `write_result` with a full internal report, then optionally append:

> `PROPOSED_USER_TEXT:` one terse line the dispatcher may forward to the user.

Dispatcher: files full reports, holds routine findings, surfaces notable outcomes and the exit summary.

## Hard Rules

- No file deletion without explicit user approval — file an issue instead.
- No self-merging PRs.
- No scan summaries — do the work.

See `context/housekeeping-reference.md` for workstream checklists, staleness thresholds, and PR conventions.
