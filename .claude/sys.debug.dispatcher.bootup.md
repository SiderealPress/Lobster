# Debug Mode — Dispatcher Startup Supplement

Loaded when `LOBSTER_DEBUG=true` AND the session is the dispatcher. Extends
`sys.debug.bootup.md` and `sys.dispatcher.bootup.md` with debug-specific
startup invariant checks.

> **Note:** `local-dev` is the running branch for local soak testing. GitHub PRs always target `main`, not `local-dev`. Deploying to `local-dev` means `git merge origin/<branch>` locally — it is never a GitHub PR base.

## Branch Invariant Check (REQUIRED at startup)

**Before entering your main loop**, spawn a background subagent to verify the
branch invariant. This must fire before the first `wait_for_messages()` call.

```
Task(
    prompt="""
---
task_id: debug-branch-check
chat_id: ADMIN_CHAT_ID_REDACTED
source: system
---

Check the git branch of ~/lobster/ and report the result.

Steps:
1. Run: git -C ~/lobster branch --show-current
2. If the branch is NOT 'local-dev':
   - call send_reply(chat_id=ADMIN_CHAT_ID_REDACTED, text="BRANCH ALERT: ~/lobster/ is on '<branch>' — expected local-dev. Debug mode is active but the local-dev fixes are NOT running. Run: git -C ~/lobster checkout local-dev")
3. If the branch IS 'local-dev': no action needed (do not send a reply)
4. call write_result(task_id='debug-branch-check', chat_id=0, source='system', text='Branch check complete. Branch: <branch>. Alert sent: <yes/no>.', status='success')
""",
    subagent_type="general-purpose",
    run_in_background=True,
)
```

This check runs in parallel with the startup-catchup subagent and costs
<1 second on the main thread.

## Startup Invariant Checklist

In debug mode, the dispatcher must verify these invariants on startup. These
are code-level contracts — treat a failed invariant as an urgent alert, not
a minor warning.

| Invariant | How to check | Alert if violated |
|---|---|---|
| `~/lobster/` is on `local-dev` | `git -C ~/lobster branch --show-current` | Yes — send_reply to the instance owner immediately |
| `LOBSTER_DEBUG=true` in config | Check `~/lobster-config/config.env` | No — you are in debug mode if this file is loaded |
| No stale sessions from previous run | on-fresh-start.py hook handles this automatically | File bug if sessions persist after 5 min |

## Debug-Mode Startup Sequence

Full startup sequence in debug mode (extends the base startup in
`sys.dispatcher.bootup.md`):

1. (Base) Read `handoff.md`, `_context.md`, create session file
2. (Base) Check context-handoff.json, send warming-up notification if stale
3. **(Debug-only)** Spawn branch-check subagent (parallel with step 4)
4. (Base) Spawn `compact-catchup` in background
5. (Base) Call `wait_for_messages()` — enter the main loop

The branch-check result arrives as a `subagent_result` with
`task_id: "debug-branch-check"` and `chat_id: 0`. Process it silently —
the subagent already sent the alert if the branch was wrong. Just
`mark_processed`.

## Debug-Mode Alerting Policy

When an invariant check fails in debug mode, **always alert the instance owner immediately**
via `send_reply(chat_id=ADMIN_CHAT_ID_REDACTED, ...)`. Do not wait for the user to ask.

The branch invariant is critical: if `~/lobster/` is on `main` in debug mode,
every fix from `local-dev` is absent and the system is silently degraded.
