# Lobster Context Bridge — Specification

**Version:** 1.0 (Layer 1: Read-only API)
**Port:** 8743
**Transport:** MCP Streamable HTTP (per MCP spec §4.3)
**Related issue:** [#83 Epic](https://github.com/SiderealPress/Lobster/issues/83)

---

## Overview

The Lobster Context Bridge is a Streamable HTTP MCP server that exposes the running state of a Lobster instance to remote clients. It is designed so that a Claude agent on a laptop or phone feels like it is part of the same thread as the main Lobster instance — able to query memory, tasks, projects, and pending work without SSH access.

The bridge runs as a separate systemd service (`lobster-context-bridge`) on port 8743, distinct from:

- **Port 8741** — `lobster-inbox-http`: read-only inbox MCP clone
- **Port 8742** — `lobster-observability`: metrics/telemetry JSON endpoint

---

## Authentication

All endpoints (except `/health`) require a Bearer token in the `Authorization` header.

```
Authorization: Bearer <token>
```

The token is configured via:
1. `LOBSTER_CONTEXT_BRIDGE_TOKEN` environment variable (preferred)
2. `MCP_HTTP_TOKEN` environment variable (fallback)
3. `config/mcp-http-auth.env` file with `MCP_HTTP_TOKEN=<value>` (fallback)

Unauthorized requests receive `HTTP 401 Unauthorized`.

---

## Endpoints

### `GET /health`

Returns service health. No authentication required.

**Response (200 OK):**
```json
{
  "healthy": true,
  "service": "lobster-context-bridge",
  "layer": 1,
  "tools": 9,
  "memory_available": true
}
```

`memory_available` is `false` when the vector memory DB is unavailable (the server degrades gracefully — canonical file tools still work).

### `POST /mcp`

MCP Streamable HTTP endpoint. All MCP tool calls are routed through this endpoint. See [MCP Streamable HTTP spec](https://modelcontextprotocol.io/docs/concepts/transports#streamable-http) for protocol details.

---

## MCP Tools (Layer 1)

All tools are **read-only**. No tool in Layer 1 modifies any state.

### `get_memory`

Search Lobster's memory using hybrid vector + keyword search.

**Input:**
```json
{
  "query": "string (required) — natural language or keyword query",
  "limit": "integer (optional, default 10) — max results",
  "project": "string (optional) — filter to a specific project"
}
```

**Output:** Text listing matching memory events with ID, timestamp, type, source, project, and content preview.

**Degrades gracefully:** Returns an error message if the vector DB is unavailable (e.g., fastembed not installed).

**Data source:** `~/lobster-workspace/data/memory.db` (SQLite + sqlite-vec + FTS5)

---

### `get_recent_memory`

Get recent memory events within a time window.

**Input:**
```json
{
  "hours": "integer (optional, default 24) — look-back window",
  "project": "string (optional) — filter to a specific project"
}
```

**Output:** Text listing recent events, newest first, with timestamp and content preview.

**Data source:** `~/lobster-workspace/data/memory.db`

---

### `get_priorities`

Fetch the current priority stack.

**Input:** None

**Output:** Raw markdown content of `canonical/priorities.md`. Updated nightly by the consolidation cron job.

**Data source:** `~/lobster-workspace/memory/canonical/priorities.md`

---

### `list_projects`

List all projects tracked in canonical memory.

**Input:** None

**Output:** JSON array of `{"name": "project-name", "path": "/absolute/path/to/file.md"}` objects. Use project names with `get_project_context`.

**Data source:** `~/lobster-workspace/memory/canonical/projects/*.md`

---

### `get_project_context`

Fetch the full context document for a named project.

**Input:**
```json
{
  "project": "string (required) — project name (e.g., 'lobster', 'kissinger')"
}
```

**Output:** Raw markdown content of `canonical/projects/{project}.md`. If the project doesn't exist, returns the list of available projects.

**Security:** Rejects project names containing `/`, `\`, or `..` to prevent path traversal.

**Data source:** `~/lobster-workspace/memory/canonical/projects/{project}.md`

---

### `list_tasks`

List tasks tracked by the main Lobster instance.

**Input:**
```json
{
  "status": "string (optional, default 'all') — one of: pending, in_progress, completed, all"
}
```

**Output:** Text grouped by status (In Progress / Pending / Completed), with task ID and subject for each.

**Data source:** `~/messages/tasks.json`

---

### `get_pending_agents`

Get the list of background agents currently in-flight.

**Input:** None

**Output:** Text listing each pending agent with its ID, description, and start time.

**Data source:** `~/messages/config/pending-agents.json`

---

### `get_handoff`

Read the current session handoff document.

**Input:** None

**Output:** Raw markdown content of `canonical/handoff.md`. This document is Lobster's complete self-briefing: identity, architecture, current state, and pending items.

**Data source:** `~/lobster-workspace/memory/canonical/handoff.md`

---

### `get_daily_digest`

Fetch the latest daily digest.

**Input:** None

**Output:** Raw markdown content of `canonical/daily-digest.md`. Summarizes recent activity: key conversations, task progress, decisions made, items needing follow-up.

**Data source:** `~/lobster-workspace/memory/canonical/daily-digest.md`

---

## Client Configuration

### Claude Desktop

Add to `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows):

```json
{
  "mcpServers": {
    "lobster-context-bridge": {
      "type": "http",
      "url": "http://<your-vps-ip>:8743/mcp",
      "headers": {
        "Authorization": "Bearer <your-token>"
      }
    }
  }
}
```

### Claude Code (remote instance)

```bash
claude mcp add lobster-context-bridge \
  --transport http \
  --url "http://<your-vps-ip>:8743/mcp" \
  --header "Authorization: Bearer <your-token>"
```

### Verification

After configuring, test with:
```
get_priorities()
```
This should return the current Lobster priority stack. If it returns "No priorities file found", the connection is working but the nightly consolidation job hasn't run yet — that is expected on a fresh install.

---

## Configuration (server-side)

Set in `lobster.conf` (or as environment variables):

| Variable | Default | Description |
|----------|---------|-------------|
| `LOBSTER_CONTEXT_BRIDGE_ENABLED` | `false` | Set `true` to enable. The systemd service only starts when enabled. |
| `LOBSTER_CONTEXT_BRIDGE_PORT` | `8743` | TCP port. Change if 8743 conflicts with something else. |
| `LOBSTER_CONTEXT_BRIDGE_TOKEN` | (none) | Bearer token. Required when enabled. Falls back to `MCP_HTTP_TOKEN`. |
| `LOBSTER_BRIDGE_ALLOW_WRITES` | `false` | Enable write tools (Layer 3, not yet implemented). |

---

## Versioning Strategy

The spec version is embedded in the `get_handoff` response and in the `/health` response `layer` field. Future layers will increment the layer number. Clients should use the `layer` field from `/health` to detect which capabilities are available.

Breaking changes (tool removals, schema changes) will be avoided. Additive changes (new tools, new optional fields) are non-breaking and may ship without a version bump.

---

## Planned Layers

| Layer | Status | Description |
|-------|--------|-------------|
| 1 | Implemented | Read-only memory and state API (this document) |
| 2 | [Issue #85](https://github.com/SiderealPress/Lobster/issues/85) | Live SSE event stream |
| 3 | [Issue #86](https://github.com/SiderealPress/Lobster/issues/86) | Write/inject capabilities |
| 4 | [Issue #87](https://github.com/SiderealPress/Lobster/issues/87) | Agent-to-agent protocol (deferred) |

---

## Security Notes

- The token in `Authorization: Bearer` is compared with constant-time string comparison to prevent timing attacks.
- Project names are sanitized against path traversal before any file access.
- HTTPS is strongly recommended in production. The server itself does not terminate TLS — use nginx with Let's Encrypt as a reverse proxy.
- The server logs all tool calls at INFO level. Auth failures are logged at WARNING level with the client IP.
