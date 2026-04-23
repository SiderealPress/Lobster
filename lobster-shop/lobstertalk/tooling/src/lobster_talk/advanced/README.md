# Advanced Lobster Integration

**This directory is only relevant if you are running the full Lobster dispatcher.**

The modules here assume a live Lobster instance with:
- A running Claude Code dispatcher process
- The MCP server (`lobster-mcp-local`) active
- `~/messages/inbox/` managed by the Lobster scheduler

If you are setting up a new Lobster instance using the basic cron path
(`lobstertalk_poll.py`), ignore everything in this directory.

---

## eventbus_bridge.py

Design stub for always-on EventBus emission of bot-talk messages.

### Architecture

All bot-talk messages (sent and received) unconditionally emit to the EventBus
via `emit_to_eventbus()`. This is not conditional on debug mode — emission
always happens.

A separate MCP server listener subscribes to those EventBus events:

- **`LOBSTER_DEBUG=true`:** the listener injects the message directly into the
  main Telegram/Slack channel, bypassing the dispatcher's inbox poll cycle.
  This is the "direct push" feature — sub-second delivery for real-time testing.

- **`LOBSTER_DEBUG=false` (production):** the listener is passive — events are
  on the bus for audit/monitoring only. Normal inbox-file delivery is unaffected.

The bridge: `bot-talk poll script → emit to EventBus → MCP listener → (if debug) inject to Telegram/Slack`

### Current status

`emit_to_eventbus()` is a stub that currently returns False (IPC channel not
yet implemented). The EventBus path is independent of the normal inbox-file
delivery path — both can coexist.

**Not needed for basic bot-talk polling.** The inbox-file path in
`lobstertalk_poll.py` is the stable production path.
