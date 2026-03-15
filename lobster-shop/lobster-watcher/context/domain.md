## Lobster Watcher — Domain Knowledge

**What it is:** A real-time observability dashboard for Lobster agent sessions. It renders currently running and recently completed background agents as an interactive timeline and 3D Three.js scene.

**Architecture:**

```
agent_sessions.db (SQLite, WAL mode — written by the Lobster MCP server)
        |
        | read-only poll every 2 seconds
        v
wire-server/wire_server.py  (Starlette/uvicorn, port 8765)
        |
        | GET /health        — health check, no auth
        | GET /api/sessions  — JSON snapshot (polling fallback)
        | GET /stream        — SSE stream, real-time diffs
        v
Browser frontend (TypeScript, XState, Three.js)
        |
        v
Timeline view + 3D session graph served as static files from nginx
```

**Key facts:**

- The wire server is **read-only** — it never writes to the database
- Sessions visible: `status = 'running'` OR `completed_at > now - 10 minutes`
- Default wire server port: `8765`
- Dashboard served at: `/watcher/` path on nginx
- The frontend is built with Vite and baked with `VITE_WIRE_URL` and `VITE_POLL_URL`

**Systemd service:**

The wire server runs as `lobster-wire.service` (system-level, not user-level, because nginx needs it and runs as root/www-data). Control it with:
- `sudo systemctl status lobster-wire`
- `sudo systemctl restart lobster-wire`
- `sudo journalctl -u lobster-wire -f`

**Security notes:**

- Wire server defaults `LOBSTER_WIRE_CORS_ORIGINS=*` (fine for localhost)
- For production with internet exposure, set a specific origin and `LOBSTER_WIRE_AUTH_TOKEN`
- Never expose port 8765 directly on a public interface without auth
- The nginx proxy at `/watcher-wire/` is the recommended production setup

**Source repo:** https://github.com/Bisque-Labs/lobster-watcher
