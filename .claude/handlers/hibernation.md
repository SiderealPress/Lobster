# Hibernation Handler

Lobster supports a **hibernation mode** to avoid idle resource usage. When no messages arrive for a configurable idle period, Claude writes a hibernate state and exits gracefully. The bot detects the next incoming message, sees that Claude is not running, and starts a fresh session automatically.

## Hibernate-aware main loop

Use `hibernate_on_timeout=True` when you want automatic hibernation after the idle period:

```
while True:
    result = wait_for_messages(timeout=1800, hibernate_on_timeout=True)
    # If the response text contains "Hibernating" or "EXIT", stop the loop
    if "Hibernating" in result or "EXIT" in result:
        break   # Claude session exits; bot will restart on next message
    # ... process messages ...
```

The `hibernate_on_timeout` flag tells `wait_for_messages` to:
1. Write `~/messages/config/lobster-state.json` with `{"mode": "hibernate"}`
2. Return a message containing the word "Hibernating" and "EXIT"
3. **You must then break out of the loop and let the session end.**

The health check recognises the hibernate state and does **not** attempt to restart Claude.
The bot (`lobster-router.service`) checks the state file when a new message arrives and restarts Claude if it is hibernating.

## State file

Location: `~/messages/config/lobster-state.json`

```json
{"mode": "hibernate", "updated_at": "2026-01-01T00:00:00+00:00"}
```

Modes: `"active"` (default) | `"hibernate"`
