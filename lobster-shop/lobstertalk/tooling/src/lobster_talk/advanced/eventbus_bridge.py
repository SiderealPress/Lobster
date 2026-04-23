"""
EventBus Bridge — always-on emission of bot-talk messages to the EventBus.

STATUS: Design stub. The feature is specified here and partially implemented.
        Full end-to-end integration is pending.

---

## What this is

All bot-talk messages — both sent and received — are unconditionally emitted to
the EventBus. This is not a debug-only or optional path; it happens in every
mode, every time a message is sent or received.

The EventBus emission is handled here. A separate MCP server listener is
subscribed to those EventBus events and decides what to do with them:

- **In debug mode (`LOBSTER_DEBUG=true`):** the listener injects the message
  directly into the main channel (Telegram or Slack), bypassing the dispatcher's
  normal inbox poll cycle entirely. This is the "direct push" feature —
  sub-second delivery for real-time testing and monitoring.

- **In production (`LOBSTER_DEBUG=false`):** the listener is passive. Events are
  on the bus for audit/monitoring purposes but no direct-push injection occurs.
  The normal inbox-file path remains the production delivery channel.

## Architecture

```
bot-talk poll script (lobstertalk_unified.py)
        │
        │  on every sent or received message
        │
        ▼
emit_to_eventbus(msg)          ← THIS MODULE (always runs, unconditionally)
        │
        │  writes to EventBus pipe / socket
        ▼
EventBus
        │
        └──► MCP server listener (subscribed to EventBus events)
                │
                ├─── LOBSTER_DEBUG=true ──────────────────────────────────────
                │    inject message directly to Telegram / Slack channel
                │    (bypasses dispatcher; sub-second delivery)
                │
                └─── LOBSTER_DEBUG=false (production) ───────────────────────
                     passive — event is logged/audited, no direct push

```

The key distinction from the old design:

- **Emission is always-on.** The poll script never skips the EventBus step.
- **Debug mode only changes the listener's behavior**, not whether the event
  is emitted.

## Implementation notes

The EventBus interface requires a lightweight IPC channel (e.g. a named pipe,
a Unix domain socket, or an HTTP endpoint on the MCP server) that the MCP
listener subscribes to.

As of April 2026 this is a design stub — `emit_to_eventbus` falls back to a
log line and returns False, signalling the caller that the bus is not yet wired
up. Implement the IPC channel before enabling this path in production.

---
"""

from __future__ import annotations

import logging
import os
from typing import Any

log = logging.getLogger(__name__)


def emit_to_eventbus(msg: dict[str, Any]) -> bool:
    """Emit a bot-talk message (sent or received) to the EventBus.

    This is called unconditionally for every bot-talk message, regardless of
    whether `LOBSTER_DEBUG` is set. Debug mode only affects what the MCP
    server listener does with the event — it does not affect whether the event
    is emitted.

    This is a stub. In the current implementation it falls back to a log line
    and returns False, signalling the caller that the EventBus is not yet
    available.

    Parameters
    ----------
    msg:   Raw bot-talk message dict (sent or received).

    Returns
    -------
    True if the message was successfully emitted to the EventBus;
    False if the bus is unavailable (caller should continue normally —
    the normal inbox-file path is unaffected by EventBus availability).
    """
    if not _is_eventbus_available():
        log.debug("EventBus not yet available — event not emitted (non-fatal)")
        return False

    # TODO: implement actual IPC here once the MCP listener is wired up.
    # Options:
    #   A. Named pipe at ~/lobster-workspace/data/lobstertalk-eventbus.pipe
    #   B. Unix domain socket
    #   C. HTTP POST to the MCP server's /push-event endpoint
    log.warning("EventBus bridge: IPC path not yet implemented")
    return False


def _is_eventbus_available() -> bool:
    """Return True if the EventBus channel is configured and reachable.

    Currently always returns False (bridge not yet implemented).
    """
    # Future: check for named pipe / socket existence
    return False


# ---------------------------------------------------------------------------
# Design doc: how to extend this
# ---------------------------------------------------------------------------
#
# Step 1 — MCP server side:
#   Add a listener coroutine that subscribes to the EventBus pipe/socket.
#   When LOBSTER_DEBUG=true, the listener injects the received event directly
#   into the Telegram/Slack channel via the messaging API, bypassing the
#   dispatcher's wait_for_messages() poll cycle.
#   When LOBSTER_DEBUG=false, the listener logs/audits the event passively.
#
# Step 2 — IPC channel:
#   Create a named pipe at ~/lobster-workspace/data/lobstertalk-eventbus.pipe
#   or a Unix domain socket. Both the emitter (this module) and the MCP
#   listener must agree on the path and wire format (one JSON object per line).
#
# Step 3 — lobstertalk_unified.py:
#   Call emit_to_eventbus(msg) for every message sent or received.
#   The return value can be ignored — EventBus availability does not affect
#   the normal inbox-file delivery path. Both paths are independent.
#
#   Example:
#     emit_to_eventbus({"direction": "inbound", **msg})   # always
#     _write_inbox_message(msg)                            # normal path
#
# Step 4 — emitter implementation:
#   Replace the stub in emit_to_eventbus() with:
#     pipe = Path.home() / "lobster-workspace" / "data" / "lobstertalk-eventbus.pipe"
#     with pipe.open("a") as f:
#         f.write(json.dumps(msg) + "\n")
