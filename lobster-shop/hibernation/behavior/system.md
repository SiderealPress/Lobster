## Hibernation — DEPRECATED

> **This skill is deprecated as of PR #1447.** The dispatcher no longer hibernates.
> `wait_for_messages()` is called without `hibernate_on_timeout=True` and the main
> loop never exits. Do NOT call `wait_for_messages(hibernate_on_timeout=True)`.
>
> If this skill activates, ignore its instructions. The dispatcher should continue
> its normal `wait_for_messages()` loop without any hibernation logic.

---

### Why hibernation was removed

Hibernation (exiting the main dispatcher loop after an idle timeout) caused the
dispatcher to go deaf to incoming messages. The WFM watchdog (PR #1446) replaced
the recovery role that hibernation was intended to serve. The dispatcher now stays
running indefinitely.

### What to do if you see mode=hibernate in lobster-state.json

This indicates a stale state file from before PR #1447. The health check now treats
`mode=hibernate` as `mode=active` and applies full process/inbox checks. The value
will be overwritten on the next state write.

### Do not use

- Do not call `wait_for_messages(timeout=1800, hibernate_on_timeout=True)`
- Do not write `mode=hibernate` to `lobster-state.json` directly
- Do not break the main dispatcher loop based on hibernation signals
