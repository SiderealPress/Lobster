## Hibernation

Lobster hibernates after a configurable idle timeout. The hibernation mechanism works through `wait_for_messages` and a state file.

### How hibernation is triggered

Call `wait_for_messages` with `hibernate_on_timeout=True` when entering a low-activity idle period:

```python
result = wait_for_messages(timeout=1800, hibernate_on_timeout=True)
```

When the timeout fires with `hibernate_on_timeout=True`, `wait_for_messages`:
1. Writes `~/messages/config/lobster-state.json` with `{"mode": "hibernate"}`
2. Returns a string containing "Hibernating" and "EXIT"

### How to detect and break the loop

```python
result = wait_for_messages(timeout=1800, hibernate_on_timeout=True)
if isinstance(result, str) and ("Hibernating" in result or "EXIT" in result):
    break  # Exit the main loop — do NOT call wait_for_messages again
```

Breaking the loop ends the Claude session cleanly. The session is not restarted — the health check reads the state file and recognizes `"hibernate"` mode, suppressing the usual restart.

### How hibernation ends

The Telegram/Slack bot restarts Claude on the next incoming message. Once a message arrives:
1. The bot clears the hibernate state file (or the health check does)
2. A new Claude session starts
3. Normal startup behavior runs (handoff read, catchup subagent, etc.)

### State file

Path: `~/messages/config/lobster-state.json`

| `mode` value | Meaning |
|---|---|
| `"active"` | Normal operation (default) |
| `"hibernate"` | Hibernating — health check must NOT restart |

### Rules

- Never call `send_reply` immediately before hibernating — there is no active user conversation
- Always write a brief session note update before breaking the loop if notable work happened this session
- The `hibernate_on_timeout=True` flag is the only supported hibernation trigger — do not write the state file directly
