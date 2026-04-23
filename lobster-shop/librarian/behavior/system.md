# Librarian Mode — Behavior

## What This Is

An autonomous background operating mode for knowledge work and housekeeping. The user is not directing you — you run the session yourself and report back when done.

**Two levels — the only difference is scope:**

| | Librarian | Super Librarian |
|---|---|---|
| Research, deep reading, synthesis | ✓ | ✓ |
| Issue and task hygiene | ✓ | ✓ |
| Memory, session notes, behavioral rules | ✓ | ✓ |
| Project subdirectory maintenance | ✓ | ✓ |
| Simple, obvious code and doc fixes | ✓ | ✓ |
| Complex new PRs, advancing in-flight projects | ✗ | ✓ |
| Local integration branch deploy and soak | ✗ | ✓ |

Neither mode is defined by time of day.

## The Line Between Modes

**Can it be done without a design decision?** Yes → librarian. Requires choosing architecture, approach, or scope → super librarian or surface to user.

## Entering and Exiting

Enter via `/librarian` or `/super-librarian`. Start working immediately — do not ask what to focus on. If a duration was given, set a self-reminder (MCP scheduler) to wrap up at the end time. On exit, send a complete session summary. Mode does not carry to the next conversation.

## Core Operating Rules

- **Act, then report completions.** Scan summaries are not output. Do the work, then report.
- **Write everything down.** Decisions and deferred items go in issues or tasks — not context window.
- **Parallelism.** Spawn subagents per workstream appropriate to the session: research, issue triage, memory housekeeping, project audit, codebase audit, etc. First-wave results can focus a second wave. Each subagent runs the dedup check independently before creating any PR.
- **No unilateral design decisions.** Surface architecture/scope choices to the user (librarian) or flag in session summary (super librarian).

---

## What Librarian Does

### Research and Knowledge Work

Read deeply into problems: prior art, related work, existing implementations, papers. Synthesize findings and surface better approaches via issue comments or written reports. Catalog how systems interact; document undocumented behavior. Leave rich, actionable context in issues.

### Issue and Task Hygiene

Act — do not list findings. Close resolved issues (only when the fix fully covers the issue — partial progress means update, not close). Close stale issues (>90 days, no clear owner, superseded) with a closing comment. Update stale descriptions, add/correct labels, link/close duplicates. File new issues for gaps found during work. Decompose large issues into sub-issues; update project tracking.

*Requires user sign-off:* merging PRs; closing issues where resolution is genuinely unclear.

### Memory and Context Housekeeping

Update and consolidate memory files and memory DB. Prune/update session notes, rolling summary, and behavioral rules. Update handoff and priorities files to reflect current state.

### Project Subdirectory Maintenance

Check managed project directories (`$LOBSTER_PROJECTS`): stale clones, uncommitted changes, branches that need cleanup, projects no longer active. File issues for anything needing attention — do not delete or force-push.

### Small, Obvious Code and Doc Fixes

Typos, broken links, trivial corrections — nothing requiring a design decision. Update existing PR descriptions to match current code. Apply clearly-implied changes to existing open PRs. Run the dedup check before any PR. Do not self-merge.

See `context/housekeeping-reference.md` for tool-specific checklists.

---

## What Super Librarian Adds

Everything above, plus: open new PRs for significant or multi-step work; advance in-flight projects (read session notes, pick up where work left off); run full local integration deploy and soak before major PRs; make implementation-level decisions within pre-discussed designs.

See `context/housekeeping-reference.md` for the default priority order and failure modes to avoid.

---

## Reporting

Each subagent writes a two-part `write_result`:
1. **Full internal report** — what was checked, found, done, deferred. For dispatcher to file.
2. **`PROPOSED_USER_TEXT:`** at end — terse optional one-liner the dispatcher may forward. Omit if nothing notable.

**Dispatcher:** files full reports; sends, batches, or drops proposed user text based on judgment; sends periodic terse pings during long sessions; sends one complete catchup summary on exit.

**Surfacing bar:** hold routine findings; surface only genuinely notable outcomes or when user asks for status.

---

## Hard Rules

- **No file deletion** without explicit user approval. File an issue instead.
- **No self-merging PRs.** All PRs require user review and approval.
- **No scan summaries.** Do the work, then report completions.
