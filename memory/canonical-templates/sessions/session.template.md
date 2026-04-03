# Session YYYYMMDD-NNN

<!--
  Session note file — created automatically when a new session starts.
  File name format: YYYYMMDD-NNN.md (e.g. 20260325-001.md)
    - YYYYMMDD: date the session started (UTC)
    - NNN: zero-padded sequence number, resets each day (001, 002, ...)
  Location: ~/lobster-user-config/memory/canonical/sessions/
  These files are committed to the private user-config repo and survive machine migrations.
-->

**Started:** <ISO timestamp, e.g. 2026-03-25T14:32:00Z>
**Ended:** <ISO timestamp or "active">
**Messages processed:** <count or "unknown">
**End reason:** <"active" | "graceful wind-down" | "context_warning" | "short session" | "crash">

## Summary
<1-3 sentence summary of what happened this session: main topics, decisions made, work completed.>

## Open Threads

<!--
  Each thread represents a user request or question that was addressed (or is still in flight).
  Add one entry per distinct request. Use the schema below.
-->

<!--
  Thread entry schema:
  - **What was asked**: the user's original request or question
  - **What was done**: what Lobster (or a subagent) actually did
  - **What's still pending**: what remains unresolved, unanswered, or in-flight (or "nothing")
  - **User acknowledged**: yes | no | silence
    (silence = user went quiet without explicit dismissal)
-->

<!-- Example:
- **What was asked**: "Check if the GitHub Actions workflow is failing"
- **What was done**: Spawned engineer subagent; PR #123 opened with fix
- **What's still pending**: Reviewer subagent verdict not yet received
- **User acknowledged**: silence
-->

## Open Tasks
<Any in-flight work: tasks, PRs, analyses, delegated work — anything not yet complete. List with status.>

## Open Subagents
<Subagents spawned this session that may not have written results yet. Include task_id and brief description.>

## Notable Events
<Significant things that happened this session: user decisions, system changes, errors, unexpected outcomes, etc.>
