# BIS-49: Session Management and Auto-Reconnect

## Overview

This document describes the session management additions made to the WhatsApp bridge
(`/home/admin/lobster-workspace/projects/whatsapp-bridge/index.js`) as part of BIS-49.

## Changes

### 1. `notifyLobster(text)` function

Writes a system notification message to the Lobster inbox directory via the filesystem.

- Uses `LOBSTER_MESSAGES_DIR` env var; falls back to `$HOME/messages/inbox`
- No hardcoded personal paths
- Message format: `{ id, source, type, chat_id, user_id, text, timestamp }`
- Source is set to `'whatsapp'`, type to `'system'`

### 2. Disconnect handler (`client.on('disconnected', ...)`)

Replaces the old handler that called `process.exit(1)` on every disconnect.

Two cases are handled:

| Reason | Action |
|--------|--------|
| `'LOGOUT'` | Notify Lobster inbox, delete `./session` directory so next startup prompts a fresh QR scan |
| Any other reason (transient) | Schedule `client.initialize()` retry after 5 seconds |

### 3. `authenticated` handler update

The `authenticated` event handler now:
- Logs `[AUTHENTICATED] Session validated` to stderr
- Calls `notifyLobster('WhatsApp reconnected and authenticated')` so Lobster can relay
  the reconnection status to the user

### 4. `touchHeartbeat()` function and `heartbeatPath()`

- `heartbeatPath()` returns the path to the heartbeat file using `LOBSTER_WORKSPACE`
  env var (falls back to `$HOME/lobster-workspace`), resolving to
  `<workspace>/logs/whatsapp-heartbeat`
- `touchHeartbeat()` writes the current epoch-ms timestamp to that file
- Called on every received `message` event
- Allows `health-check.sh` to verify bridge liveness by checking file recency

### 5. `lobster-qr.sh` helper script

Location: `whatsapp-bridge/lobster-qr.sh`

- Clears the `./session` directory
- Starts `index.js` so a fresh QR code is displayed in the terminal
- Intended for running in a tmux pane when re-authentication is required

## Health Check Integration

`/home/admin/lobster/scripts/health-check.sh` was updated to check the heartbeat file.
If the file is older than a configurable threshold, the bridge is considered stale and
a warning is logged. See the health check script for details.

## Testing

Unit tests for the disconnect handler live in:
`whatsapp-bridge/test/session-mgmt.test.js`

Tests verify:
- `LOGOUT` reason causes session directory removal and an inbox notification
- Non-LOGOUT reason schedules a reconnect (no immediate exit)
