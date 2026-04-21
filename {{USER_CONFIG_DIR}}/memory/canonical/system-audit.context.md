# System Audit Context

*Maintained by lobster-auditor. Updated at the end of every audit session.*

---

## Instructions for lobster-auditor

This file is your living architecture context. At the start of every session:
1. Read this file to understand the current known state of the system.
2. Compare it against what you observe during the investigation.
3. At the end of every session, update this file with any new findings.

If nothing changed and nothing new was discovered, include the string
`AUDIT_CONTEXT_UNCHANGED` as the first line of your `write_result` call body.
This signals to the SubagentStop hook that you explicitly acknowledged the state.

**Do not leave a session without either updating this file or emitting
`AUDIT_CONTEXT_UNCHANGED`. The hook will block your exit if neither condition
is met.**

---

## System Audit History

*No audit sessions recorded yet. The lobster-auditor will populate this section
after its first run.*

---

## Known Anomalies

*Populate with: ghost agents observed, reconciler errors, MCP failures,
transcription pipeline issues, or any other anomaly worth remembering across
sessions.*

---

## Root Causes Identified

*Populate with confirmed root causes and the resolution taken (or "unresolved"
if still open).*

---

## Architecture Notes

*Populate with durable observations about system internals — schema quirks,
edge cases in hooks, environment-specific behaviours — that are not captured
in the main codebase docs.*
