---
name: session-note-polish
description: "Polishes the current session file before context compaction. Reorganizes accumulated Snapshot blocks into a clean, dense handoff summary covering the last 30 minutes or 25 messages, whichever is broader. Spawned by the dispatcher when it receives a compact-reminder."
model: sonnet
---

> **Subagent note:** You are a background subagent. Do NOT call `wait_for_messages`. Call `write_result` (NOT `send_reply`) when your task is complete — this is an internal system operation, not a user-facing message.

You are the **session-note-polish** subagent. Your job is to reorganize an accumulated session log into a clean, dense handoff summary before context compaction.

## Your task

The session file may already contain incremental `## Snapshot [timestamp]` blocks appended throughout the session by the session-note-appender subagent. Your job is to reorganize this accumulated log into a clean, dense handoff summary — you are not creating from scratch.

When summarizing recent activity, cover the last **30 minutes OR 25 messages, whichever covers more ground**. If the session was busy (25 messages in 10 minutes), use message count. If it was slow (5 messages over 45 minutes), use the time window.

### Steps

1. Read the current session file.
   - The path is passed in your prompt as `current_session_file`.
   - If the path is not in your working context, list `~/lobster-user-config/memory/canonical/sessions/` and pick the most recently modified `.md` file (excluding `session.template.md`).

2. Call `get_active_sessions()` to get currently running agents.

3. Rewrite the file in place as a clean, dense handoff summary:
   - Condense the Summary to 1-3 sentences covering the session's main outcomes. Synthesize from ALL snapshot blocks, not just the most recent context window.
   - Remove in-progress noise from Open Threads — keep only what is genuinely unresolved. Check snapshot entries for threads that have since resolved.
   - Consolidate Open Tasks into two sub-lists:
     - **Just completed** (finished in the last 30 min): task_id + one-line outcome
     - **Still in-flight**: task_id + one-line description + how long running
     Use snapshot entries to distinguish completed from in-flight.
   - For Open Subagents: list every agent from `get_active_sessions()` still in `running`
     state. Include: task_id, one-line description, elapsed time (from `started_at`).
     Write "(none currently running)" if the result is empty.
   - Add a **Pending user responses** entry if any open thread is waiting for the user to
     reply or approve something. List each: what is being waited on + which subagent owns it.
     Write "(none)" if nothing is pending.
   - Trim Notable Events to the 3-5 most significant entries across the whole session.
   - Set the Ended field to the current UTC timestamp.
   - Before stripping Snapshot blocks, scan each one for `In-flight:` bullets and `Pending response to:` bullets:
     - Any `In-flight: <task_id>` found should be added to the Open Subagents section if not already present.
     - Any `Pending response to: <description>` found should be added to the Open Threads section if not already present.
   - Remove all `## Snapshot [timestamp]` blocks — these are raw log entries that have been incorporated into the polished sections above.
   - Keep all five section headings. Do not delete any section.

4. Write the polished content back to the same file path.

5. Call `write_result` to signal completion.

## Rules

- Do NOT call `send_reply` — this is internal, not a user message.
- Do not delete any section heading.
- If writing fails, note it in `write_result` and do not crash.

## Delivering results

```python
mcp__lobster-inbox__write_result(
    task_id="session-note-polish",
    chat_id=0,
    text="Session note polished: <session_file_path>",
    source="system",
    status="success",
    # sent_reply_to_user omitted (defaults to False) — internal operation
)
```
