# Lobster Wire Protocol

This document covers all three wire-level protocols used in the Lobster system:

1. **Bisque Wire Protocol v2** — WebSocket protocol between the bisque-chat PWA and the relay server
2. **Lobster Wire Server** — HTTP/SSE protocol streaming agent session data to dashboards
3. **Lobster Dashboard Protocol** — WebSocket protocol between bisque-computer and the dashboard server

---

## Table of Contents

- [Architecture Overview](#architecture-overview)
- [Bisque Wire Protocol v2](#bisque-wire-protocol-v2)
  - [Transport](#bisque-transport)
  - [Envelope Format](#envelope-format)
  - [Authentication Flow](#authentication-flow)
  - [Frame Types](#frame-types)
  - [Client Frames](#client-frames)
  - [Server Frames](#server-frames)
  - [Message Routing](#bisque-message-routing)
  - [Event Bus and Filesystem Sources](#event-bus-and-filesystem-sources)
  - [Event Log and Replay](#event-log-and-replay)
  - [Error Handling](#bisque-error-handling)
- [Lobster Wire Server (SSE)](#lobster-wire-server-sse)
  - [Transport](#wire-server-transport)
  - [Endpoints](#wire-server-endpoints)
  - [Authentication](#wire-server-auth)
  - [Message Types](#wire-server-message-types)
  - [Session Object Schema](#session-object-schema)
  - [PII Redaction](#pii-redaction)
  - [Change Notification](#change-notification)
  - [Configuration](#wire-server-configuration)
- [Lobster Dashboard Protocol](#lobster-dashboard-protocol)
  - [Transport](#dashboard-transport)
  - [Authentication](#dashboard-auth)
  - [Frame Types](#dashboard-frames)
- [Versioning and Compatibility](#versioning-and-compatibility)
- [Production Checklist](#production-checklist)
- [Known Limitations](#known-limitations)

---

## Architecture Overview

Lobster has two independent wire protocols serving different consumers:

```
bisque-chat (PWA)
      |
      | WebSocket (Wire Protocol v2, port 9101)
      v
BisqueRelayServer  <---  bisque-outbox/  (Lobster MCP replies)
      |                  wire-events/    (status/agent frames)
      |
      +---> inbox/  (user messages → Lobster MCP)

bisque-computer (Tauri/Electron dashboard)
      |
      | WebSocket (Dashboard Protocol, port 9100)
      v
DashboardServer  (polls system/process state)

lobster-watcher (web dashboard)
      |
      | HTTP + SSE (Wire Server Protocol, port 8765)
      v
WireServer  <---  agent_sessions.db  (SQLite, WAL)
                  /notify  (POST from MCP after session writes)
```

The **Bisque Wire Protocol v2** (`src/bisque/`) is the full-featured conversational protocol.

The **Lobster Wire Server** (`src/mcp/wire_server.py`) is a read-only SSE feed of agent session state for monitoring dashboards.

The **Dashboard Protocol** (`src/dashboard/server.py`) delivers system telemetry to bisque-computer.

---

## Bisque Wire Protocol v2

**Source:** `src/bisque/protocol.py`, `src/bisque/relay_server.py`, `src/bisque/auth.py`, `src/bisque/event_bus.py`, `src/bisque/event_log.py`

### Bisque Transport

- **Protocol:** WebSocket (text frames only; binary frames are rejected)
- **Default port:** `9101`
- **Paths:** `GET /` and `GET /{any-path}` — all paths are handled identically and upgrade to WebSocket
- **HTTP endpoint:** `POST /auth/exchange` — bootstrap token exchange (see [Authentication Flow](#authentication-flow))

TLS is not terminated by the relay server itself. Deploy nginx or Caddy in front for HTTPS/WSS.

### Envelope Format

Every message (in both directions) is a flat JSON object. Payload fields are merged into the top level alongside the four mandatory envelope fields:

```json
{
  "v":    2,
  "id":   "<uuid4>",
  "ts":   "<ISO-8601 UTC timestamp>",
  "type": "<frame_type>",
  // ...payload fields...
}
```

| Field  | Type   | Required | Description |
|--------|--------|----------|-------------|
| `v`    | int    | yes      | Protocol version. Always `2`. |
| `id`   | string | yes      | UUID4 uniquely identifying this frame. |
| `ts`   | string | yes      | ISO-8601 UTC timestamp of when the frame was created. |
| `type` | string | yes      | Frame type identifier (see [Frame Types](#frame-types)). |

All additional fields are payload and depend on the `type`.

**Serialization notes:**
- Compact JSON with no extra whitespace (`separators=(",", ":")`).
- Unicode is preserved as-is (emoji, CJK, etc.).
- The `payload` key does not appear in the wire format — payload fields are merged flat into the top-level object.

**Deserialization:**
- `v` and `type` are required; missing either raises a `ProtocolError`.
- `id` defaults to a fresh UUID4 if absent.
- `ts` defaults to the current UTC time if absent.
- All other keys become the `payload` dict.

### Authentication Flow

Authentication uses a two-token scheme to avoid embedding long-lived credentials in the WebSocket handshake.

#### Step 1 — Bootstrap Token Exchange (HTTP)

Before opening a WebSocket, the bisque-chat client exchanges a short-lived **bootstrap token** for a long-lived **session token**:

```
POST /auth/exchange
Content-Type: application/json

{"token": "<bootstrap_token>"}
```

**Success (200):**
```json
{
  "sessionToken": "<session_token>",
  "email": "user@example.com"
}
```

**Failure (400 — missing token):**
```json
{"error": "Missing 'token' field"}
```

**Failure (401 — invalid token):**
```json
{"error": "Invalid or expired bootstrap token"}
```

Bootstrap tokens are one-time-use. They are stored on disk at `<bisque-chat-project>/data/tokens.json` under the key `bootstrapTokens`. On successful exchange the token is consumed (deleted from disk). Bootstrap tokens are generated by the bisque-chat Next.js app's `/api/auth/generate-login-token` endpoint.

Session tokens are 48-byte URL-safe random strings (`secrets.token_urlsafe(48)`). They are held **in memory only** — clients must re-authenticate after a server restart. Sessions expire after 7 days of inactivity (configurable in `auth.py`).

#### Step 2 — WebSocket Auth Handshake

After the WebSocket connection is established, the **first frame the client sends must be an `auth` frame**. The server waits up to 5 seconds; if no `auth` frame arrives, it closes the connection with close code `4401`.

**Client sends:**
```json
{"v":2,"id":"...","ts":"...","type":"auth","token":"<session_token>","last_event_id":"<optional>"}
```

**Server responds (success):**
```json
{"v":2,"id":"...","ts":"...","type":"auth_success","email":"user@example.com"}
```
Immediately followed by a [snapshot or replay](#event-log-and-replay).

**Server responds (failure):**
```json
{"v":2,"id":"...","ts":"...","type":"auth_error","message":"Invalid session token"}
```
The server then closes the WebSocket with close code `4401`.

The optional `last_event_id` field in the `auth` frame enables [event replay](#event-log-and-replay).

#### WebSocket Close Codes

| Code | Meaning |
|------|---------|
| `4400` | Bad request (invalid JSON, wrong frame type, binary frame) |
| `4401` | Unauthorized (auth timeout, invalid token, auth required) |

### Frame Types

There are 19 frame types total: 4 client-to-server and 15 server-to-client. The sets are disjoint — no type appears in both directions.

### Client Frames

#### `auth`

First frame sent after WebSocket connect. See [Authentication Flow](#authentication-flow).

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `token` | string | yes | Session token obtained from `/auth/exchange`. |
| `last_event_id` | string | no | If present, server replays events after this ID instead of sending a snapshot. |

```json
{"v":2,"id":"a1b2c3d4-...","ts":"2026-03-15T00:00:00Z","type":"auth","token":"<session_token>"}
```

#### `send_message`

Send a user message to Lobster. The relay injects it into `~/messages/inbox/` as a bisque-source message.

| Field | Type | Required | Constraints |
|-------|------|----------|-------------|
| `text` | string | yes | Must be non-empty and at most 32,000 characters. |

```json
{"v":2,"id":"...","ts":"...","type":"send_message","text":"What's the weather today?"}
```

On success the server responds with an `ack` frame (not using the `ack` frame type, but a custom `ack`-typed response; see relay_server.py lines 339-347) containing `message_id` and `status: "received"`.

#### `ack`

Client acknowledges receipt of a server event. No server-side action is taken.

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `event_id` | string | yes | The `id` of the server frame being acknowledged. |

```json
{"v":2,"id":"...","ts":"...","type":"ack","event_id":"<server-frame-id>"}
```

#### `ping`

Keepalive / round-trip latency check. Server responds immediately with `pong`.

```json
{"v":2,"id":"...","ts":"...","type":"ping"}
```

### Server Frames

#### `auth_success`

Sent after successful authentication.

| Field | Type | Description |
|-------|------|-------------|
| `email` | string | The authenticated user's email address. |

```json
{"v":2,"id":"...","ts":"...","type":"auth_success","email":"user@example.com"}
```

#### `auth_error`

Sent when authentication fails. The server closes the connection immediately after.

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `message` | string | yes | Human-readable error description. |
| `code` | string/int | no | Machine-readable error code. |

```json
{"v":2,"id":"...","ts":"...","type":"auth_error","message":"Invalid session token"}
```

#### `snapshot`

Sent immediately after `auth_success`. Contains current inbox/task state.

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `status` | string | yes | Current agent status (`idle`, `thinking`, `executing`, `waiting`). |
| `recent_messages` | array | no | Up to 20 recent sent messages from `~/messages/sent/`. |
| `tasks` | array | no | Current task list. |
| `last_event_id` | string | no | ID of the most recent event in the server's event log. Clients should store this for reconnect replay. |

```json
{
  "v":2,"id":"...","ts":"...",
  "type":"snapshot",
  "status":"idle",
  "recent_messages":[
    {"id":"msg-001","text":"Hello!","source":"bisque","chat_id":"user@example.com","timestamp":"2026-03-15T00:00:00Z"}
  ],
  "last_event_id":"evt-uuid-..."
}
```

#### `message`

A conversational message (either direction as payload; always server → client on the wire).

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `text` | string | yes | Message content. |
| `role` | string | yes | `"assistant"` or `"user"`. |
| `source` | string | no | Origin source (e.g., `"bisque"`). |
| `chat_id` | string | no | Sender/recipient identifier (email for bisque). |
| `msg_id` | string | no | Lobster message ID. |

```json
{"v":2,"id":"...","ts":"...","type":"message","text":"Here's your answer.","role":"assistant","source":"bisque","chat_id":"user@example.com","msg_id":"bisque_1742000000000_abc12345"}
```

#### `inbox_update`

Notifies the client of an inbox state change (message received, processed, etc.).

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `action` | string | yes | What happened: `"received"`, `"processed"`, `"failed"`, etc. |
| `message_id` | string | yes | The affected message ID. |
| `preview` | string | no | Short preview of the message text. |

```json
{"v":2,"id":"...","ts":"...","type":"inbox_update","action":"received","message_id":"bisque_...","preview":"Hello..."}
```

#### `status`

Agent processing status update.

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `status` | string | yes | One of `idle`, `thinking`, `executing`, `waiting`. |
| `detail` | string | no | Human-readable detail about the current operation. |

```json
{"v":2,"id":"...","ts":"...","type":"status","status":"thinking","detail":"Processing your request"}
```

#### `tool_call`

Emitted when the agent invokes an MCP tool.

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `tool_name` | string | yes | Name of the tool being called. |
| `arguments` | object | no | Tool arguments as a key/value map. |

```json
{"v":2,"id":"...","ts":"...","type":"tool_call","tool_name":"read_file","arguments":{"path":"/tmp/x"}}
```

#### `tool_result`

Result of an MCP tool call.

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `tool_name` | string | yes | Name of the tool that returned. |
| `result` | any | no | Tool return value. |
| `error` | string | no | Error message if the tool failed. |

```json
{"v":2,"id":"...","ts":"...","type":"tool_result","tool_name":"read_file","result":"file contents here"}
```

#### `stream_start`

Marks the beginning of a streaming response.

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `stream_id` | string | no | Correlator for matching deltas and end to this stream. |

```json
{"v":2,"id":"...","ts":"...","type":"stream_start","stream_id":"s-uuid-..."}
```

#### `stream_delta`

A chunk of streaming text output.

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `text` | string | yes | This chunk's text content. |
| `stream_id` | string | no | Correlates to the matching `stream_start`. |

```json
{"v":2,"id":"...","ts":"...","type":"stream_delta","text":"Here is the ","stream_id":"s-uuid-..."}
```

#### `stream_end`

Marks the end of a streaming response.

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `stream_id` | string | no | Correlates to the matching `stream_start`. |

```json
{"v":2,"id":"...","ts":"...","type":"stream_end","stream_id":"s-uuid-..."}
```

#### `agent_started`

Emitted when Lobster spawns a background subagent.

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `task` | string | no | Human-readable description of the task. |

```json
{"v":2,"id":"...","ts":"...","type":"agent_started","task":"Researching issue #42"}
```

#### `agent_completed`

Emitted when a background subagent finishes.

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `task` | string | no | Human-readable description of the completed task. |
| `result` | any | no | Task result or summary. |

```json
{"v":2,"id":"...","ts":"...","type":"agent_completed","task":"Researching issue #42","result":"PR #43 opened"}
```

#### `error`

A protocol or application error.

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `message` | string | yes | Human-readable error description. |
| `code` | string/int | no | Machine-readable error code. |

```json
{"v":2,"id":"...","ts":"...","type":"error","message":"Message too long (max 32000 chars)"}
```

#### `pong`

Response to a client `ping`.

```json
{"v":2,"id":"...","ts":"...","type":"pong"}
```

### Bisque Message Routing

When a user sends a `send_message` frame:

1. The relay calls `_inject_into_inbox()`, which writes a JSON file to `~/messages/inbox/` with an atomically-renamed temp file:
   ```json
   {
     "id": "bisque_<timestamp_ms>_<8-hex>",
     "source": "bisque",
     "chat_id": "<user_email>",
     "text": "<message_text>",
     "timestamp": "<ISO-8601>",
     "type": "text"
   }
   ```
2. The Lobster MCP server picks up the file from `inbox/` and processes it.
3. Lobster's `send_reply()` MCP tool, when called with `source="bisque"`, writes the reply to `~/messages/bisque-outbox/` instead of the standard outbox.
4. The relay's `OutboxEventSource` watchdog detects the new file, converts it to a `message` frame (role=`"assistant"`), emits it on the event bus, and deletes the file.
5. The event is logged in the `EventLog` and fanned out to all connected WebSocket clients.

Status and agent lifecycle frames (`status`, `tool_call`, `tool_result`, `stream_*`, `agent_started`, `agent_completed`) are delivered via the `wire-events/` directory. Any process can drop a JSON file there:
```json
{"type": "status", "status": "thinking", "event_id": "<optional-uuid>"}
```
The `FileSystemEventSource` watchdog picks it up, wraps it in a v2 envelope, emits it on the bus, and deletes the file.

### Event Bus and Filesystem Sources

The relay uses an `EventBus` pub/sub mechanism:

- **Subscribers** are registered via `EventBus.subscribe(callback)`.
- **Events** are emitted as `(event_id, serialized_frame)` tuples.
- **Sources** push events onto the bus:
  - `OutboxEventSource`: watches `~/messages/bisque-outbox/` for reply files.
  - `FileSystemEventSource`: watches `~/messages/wire-events/` for pre-built event files.
- The relay server subscribes and fans out each event to all connected clients while appending it to the `EventLog`.

### Event Log and Replay

The `EventLog` maintains a bounded in-memory ring buffer of `(event_id, frame)` pairs (default capacity: 500 events).

On reconnect, a client can include `last_event_id` in its `auth` frame. The server calls `EventLog.replay_after(last_event_id)` and replays all frames that occurred after that event — giving the client a gap-free stream without a full snapshot.

If `last_event_id` is not found in the log (evicted from the ring buffer, or from before a server restart), the server falls back to sending a full `snapshot` frame.

The latest `last_event_id` is included in every `snapshot` frame so clients can always recover it.

**Important:** The event log lives in memory. It is lost on server restart, so clients that reconnect after a restart will always receive a snapshot rather than a replay.

### Bisque Error Handling

| Condition | Server action |
|-----------|--------------|
| First frame is not `auth` | Send `auth_error`, close with code `4401` |
| No `auth` frame within 5s | Send `auth_error`, close with code `4401` |
| Invalid JSON | Send `auth_error` or `error`, close with code `4400` |
| Binary frame received | Send `error`, close with code `4400` |
| Invalid session token | Send `auth_error`, close with code `4401` |
| Unknown client frame type | Send `error` (connection stays open) |
| Missing required payload field | Send `error` (connection stays open) |
| Message text is empty | Send `error` (connection stays open) |
| Message text exceeds 32,000 chars | Send `error` (connection stays open) |
| Client disconnect mid-session | Cleaned up from `_clients` set silently |

---

## Lobster Wire Server (SSE)

**Source:** `src/mcp/wire_server.py`

### Wire Server Transport

- **Protocol:** HTTP with Server-Sent Events (SSE) for the streaming endpoint; plain JSON for the polling endpoint
- **Default port:** `8765` (configured via `LOBSTER_WIRE_PORT`)
- **Framework:** Starlette / uvicorn
- **TLS:** Not handled internally; terminate TLS with nginx or Caddy

### Wire Server Endpoints

| Method | Path | Auth required | Description |
|--------|------|---------------|-------------|
| `GET`  | `/health` | No | Health check. Returns `{"status":"ok","sessions_count":<n>}`. Always 200. |
| `GET`  | `/api/sessions` | Yes | Full session snapshot as JSON (polling fallback). |
| `GET`  | `/stream` | Yes | SSE stream of session changes (real-time push). |
| `POST` | `/notify` | No | Change notification from MCP server. Wakes all SSE generators immediately. |

### Wire Server Auth

Authentication is optional (disabled by default). When `LOBSTER_WIRE_AUTH_TOKEN` is set, all endpoints except `/health` and `/notify` require the token.

Two delivery methods are supported:

1. **Authorization header** (for polling clients using `fetch()`):
   ```
   Authorization: Bearer <token>
   ```

2. **Query parameter** (for SSE clients using `EventSource`, which cannot set request headers):
   ```
   GET /stream?token=<token>
   ```

Both methods are accepted on any protected endpoint.

Unauthorized requests return `401 Unauthorized`.

### Wire Server Message Types

The SSE stream sends newline-delimited `data:` lines per the SSE spec. Each event is a JSON object.

#### `snapshot` (on connect)

Sent once, immediately after the SSE connection is established. Contains all currently active sessions plus completed/failed sessions within the `LOBSTER_WIRE_HISTORY_HOURS` window.

```
data: {"type":"snapshot","sessions":[...],"timestamp":"2026-03-15T00:00:00Z"}
```

#### `session_start`

A new session appeared in the database.

```
data: {"type":"session_start","session":{...},"timestamp":"..."}
```

#### `session_update`

A running session's state changed (e.g., `last_seen_at` updated) but it has not ended.

```
data: {"type":"session_update","session":{...},"timestamp":"..."}
```

#### `session_end`

A session reached a terminal state (`completed`, `failed`, or `dead`), or it aged out of the history window.

For sessions that aged out of the `HISTORY_HOURS` window (no longer returned by the DB query), a minimal synthetic event is emitted:
```
data: {"type":"session_end","session":{"id":"<id>","status":"completed","completed_at":"<ts>"},"timestamp":"..."}
```

For sessions that ended normally, the full session object is included.

### Session Object Schema

All session objects (in both `snapshot` and individual events) share this shape, matching the `AgentSession` TypeScript interface in the lobster-watcher frontend:

| Field | Type | Description |
|-------|------|-------------|
| `id` | string | Session identifier (primary key) |
| `task_id` | string\|null | External task ID (e.g., from the pending agent tracker) |
| `agent_type` | string\|null | Agent type (e.g., `functional-engineer`, `general-purpose`) |
| `description` | string | Human-readable task description. **PII-sensitive** — redacted when `LOBSTER_WIRE_REDACT_PII=true`. |
| `chat_id` | string | Telegram/bisque chat ID of the requester |
| `source` | string | Message source (`telegram`, `bisque`, etc.) |
| `status` | string | `running`, `completed`, `failed`, or `dead` |
| `output_file` | string\|null | Path to the agent's JSONL output file |
| `timeout_minutes` | int\|null | Agent timeout in minutes |
| `input_summary` | string\|null | Short summary of the input. **PII-sensitive**. |
| `result_summary` | string\|null | Short summary of the result. **PII-sensitive**. |
| `parent_id` | string\|null | Parent session ID for nested agents |
| `spawned_at` | string | ISO-8601 UTC timestamp when the session was created |
| `completed_at` | string\|null | ISO-8601 UTC timestamp when the session ended |
| `last_seen_at` | string\|null | ISO-8601 UTC timestamp of last heartbeat |
| `trigger_message_id` | string\|null | ID of the inbox message that triggered this session. **PII-sensitive**. |
| `reply_message_ids` | string\|null | JSON-encoded list of outgoing message IDs sent in reply |
| `notified_at` | string\|null | ISO-8601 UTC timestamp when the user was notified of completion |
| `trigger_snippet` | string\|null | Short snippet of the triggering message text. **PII-sensitive**. |

**Note:** `elapsed_seconds` was removed in an earlier iteration (unused by frontend, wastes bandwidth).

### PII Redaction

When `LOBSTER_WIRE_REDACT_PII=true`, the following fields are replaced with the literal string `"[redacted]"` in all outgoing events:

- `description`
- `input_summary`
- `result_summary`
- `trigger_snippet`

These fields are **never logged** by the wire server regardless of the redact setting (they are excluded from all log calls for safety).

### Change Notification

The wire server uses an event-driven push model to minimize latency:

1. The MCP server (`inbox_server.py`) calls `_notify_wire_server()` after every session write. This posts to `http://localhost:<LOBSTER_WIRE_PORT>/notify` with a 150ms timeout (fire-and-forget).
2. The `/notify` endpoint calls `_broadcast_change()`, which sets an `asyncio.Event` for every active SSE generator.
3. Each SSE generator wakes immediately and queries the database for changes.

If `/notify` is not called (MCP server is down, or the POST fails), SSE generators fall back to polling the database every `LOBSTER_WIRE_POLL_INTERVAL` seconds (default 0.5s).

Change detection is based on a fingerprint of `(status, completed_at, last_seen_at, result_summary)` per session. Only sessions with a changed fingerprint emit `session_start`/`session_update`/`session_end` events.

### Wire Server Configuration

All configuration is via environment variables with no required values:

| Variable | Default | Description |
|----------|---------|-------------|
| `LOBSTER_WIRE_PORT` | `8765` | Server listen port |
| `LOBSTER_WIRE_POLL_INTERVAL` | `0.5` | DB poll interval in seconds (fallback safety net) |
| `LOBSTER_WIRE_CORS_ORIGINS` | `*` | Comma-separated allowed CORS origins |
| `LOBSTER_WIRE_AUTH_TOKEN` | *(unset)* | Bearer token; if set, required on non-health endpoints |
| `LOBSTER_WIRE_REDACT_PII` | `false` | Strip PII fields (`description`, `input_summary`, `result_summary`, `trigger_snippet`) from all events |
| `LOBSTER_WIRE_HISTORY_HOURS` | `24` | Hours of completed session history to serve |
| `LOBSTER_DB_PATH` | `~/messages/config/agent_sessions.db` | Path to the SQLite agent sessions database |
| `LOBSTER_WIRE_NOTIFY_URL` | `http://localhost:8765/notify` | URL the MCP server POSTs to on session changes |

---

## Lobster Dashboard Protocol

**Source:** `src/dashboard/server.py`

### Dashboard Transport

- **Protocol:** WebSocket (text JSON frames)
- **Default port:** `9100`
- **Authentication:** UUID token passed as `?token=<uuid>` query parameter in the WebSocket URL

The dashboard token is a UUID4 stored at `~/messages/config/dashboard-token`. It is generated once on first startup and persists across server restarts.

### Dashboard Auth

Authentication is token-based via the WebSocket URL path:

```
ws://<host>:9100?token=<uuid>
```

If the token is missing or incorrect, the server sends an `error` frame and closes the connection with code `4401`.

The bisque-computer client gets the full connection URL including token via the MCP tool `get_bisque_connection_url`.

### Dashboard Frames

The dashboard protocol uses a simpler framing scheme than the bisque protocol:

```json
{
  "version": "1.0.0",
  "type": "<frame_type>",
  "timestamp": "<ISO-8601>",
  "data": { ... }
}
```

#### Server → Client

| Type | Description |
|------|-------------|
| `hello` | Sent on connect. `data: {"server":"lobster-dashboard","protocol_version":"1.0.0"}` |
| `snapshot` | Full system state dump (sent on connect and in response to `request_snapshot`). `data` contains the output of `collect_full_snapshot()` covering system info, process health, inbox counts, agent sessions, etc. |
| `update` | Periodic system state update (same structure as `snapshot`). Sent every `interval` seconds (default 3s). |
| `pong` | Response to a client `ping`. |
| `error` | Error frame. `data: {"message":"<text>"}` |

#### Client → Server

| Type | Description |
|------|-------------|
| `ping` | Keepalive. Server responds with `pong`. |
| `request_snapshot` | Request an immediate full snapshot. |

---

## Versioning and Compatibility

### Bisque Wire Protocol

The protocol version is carried in the `v` field of every envelope. The current version is **2**. Version 1 was a previous iteration; no v1 clients are expected in production.

The server rejects unknown client frame types with an `error` frame (connection stays open, allowing the client to recover). Unknown server frame types should be silently ignored by clients for forward compatibility.

### Wire Server

The wire server has no explicit versioning in its API. The session object schema evolves via SQLite `ALTER TABLE` migrations in `session_store.py`. New nullable columns are added without breaking existing readers. The `elapsed_seconds` field was removed in an earlier iteration; frontends must not depend on fields that have been removed.

### Dashboard Protocol

Version is included as a static string `"1.0.0"` in the `version` field of every frame. No negotiation is performed.

---

## Production Checklist

### Bisque Relay

- Put nginx/Caddy in front for WSS (TLS) — the relay does not handle TLS
- The `/auth/exchange` endpoint serves CORS with `Access-Control-Allow-Origin: *` by default; restrict this in production via your reverse proxy

### Wire Server

- Set `LOBSTER_WIRE_CORS_ORIGINS` to the specific dashboard origin instead of `*`
- Set `LOBSTER_WIRE_AUTH_TOKEN` to a strong random token
- Put TLS termination (nginx/Caddy) in front for HTTPS/SSE over TLS
- Consider `LOBSTER_WIRE_REDACT_PII=true` for external monitoring pipelines or shared dashboards

### Dashboard Server

- The token is a plain UUID; for public-facing deployments, restrict access via firewall or reverse proxy
- The `get_bisque_connection_url` MCP tool returns the full URL with token — treat it as a credential

---

## Known Limitations

1. **Bisque event log is in-memory.** Clients that reconnect after a relay server restart will always receive a full snapshot, never a replay. The `last_event_id` from the previous session will not be found.

2. **Session tokens are in-memory.** Bisque clients must re-authenticate after a relay server restart. The bisque-chat app handles this by redirecting to the login screen on `auth_error`.

3. **Wire server is read-only.** It only reads from `agent_sessions.db`. It uses a persistent read-only SQLite connection opened with `?mode=ro`. No writes are made to the database.

4. **Wire server `/notify` has no auth.** It is intended for localhost-only use (called by the MCP server on the same host). Do not expose `/notify` externally.

5. **Bisque relay CORS is `*` by default.** The HTTP `/auth/exchange` endpoint returns `Access-Control-Allow-Origin: *` unconditionally. Restrict this via reverse proxy for production deployments.

6. **Dashboard token is a UUID stored in plaintext.** It persists across restarts at `~/messages/config/dashboard-token`. Treat it as a secret.

7. **Wire-events/ directory requires a running relay.** Status frames (`status`, `tool_call`, etc.) are only delivered if the relay server is running and watching `~/messages/wire-events/`. They are silently dropped if the relay is not running.

8. **Session tombstones for aged-out sessions are minimal.** When a session ages out of the `HISTORY_HOURS` window, the SSE stream emits a synthetic `session_end` with only `{id, status, completed_at}` — not the full session object. Clients should handle missing fields in tombstone events.
