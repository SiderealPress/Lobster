# Lobster Watcher

**See all your running and recent Lobster agents in a live dashboard.**

Lobster Watcher is a real-time observability dashboard that renders agent sessions as an interactive timeline and 3D scene. It connects to your Lobster instance over a Server-Sent Events stream and updates live as agents start, run, and complete.

## What It Lets You Do

- **See agents as they run** — timeline view shows all active and recent background agents
- **Understand what Lobster is doing** — each agent session shows status, timing, and description
- **Diagnose stuck or failed agents** — quickly spot sessions that have been running too long
- **Access from anywhere** — SSH tunnel from your laptop, no VPN required

## Install

```bash
bash ~/lobster/lobster-shop/lobster-watcher/install.sh
```

The script handles everything:
1. Clones [lobster-watcher](https://github.com/Bisque-Labs/lobster-watcher) into `$LOBSTER_PROJECTS/lobster-watcher/`
2. Builds the frontend (Vite/TypeScript) with nginx-proxied wire URLs baked in
3. Deploys static files to `/var/www/html/watcher/`
4. Patches nginx to serve `/watcher/` and proxy `/watcher-wire/`
5. Installs and starts the wire server as `lobster-wire.service` (systemd)

To update later, re-run the same command — it is idempotent.

## Access

**On the instance:**
```
http://localhost/watcher/
```

**Via SSH tunnel (recommended for remote access):**
```bash
ssh -L 8080:localhost:80 admin@<your-lobster-host>
```
Then open `http://localhost:8080/watcher/` in your browser.

## Components

| Component | Role |
|---|---|
| `dist/` (nginx) | Static frontend — HTML/JS/CSS served at `/watcher/` |
| `wire_server.py` | Python/Starlette SSE server on port 8765 (internal) |
| `lobster-wire.service` | systemd service keeping the wire server alive |
| nginx `/watcher-wire/` | Reverse proxy for the wire server (SSE + REST) |

## Health Check

```bash
curl http://localhost/watcher-wire/health
# {"status": "ok", "sessions_count": N}
```

## Source

[github.com/Bisque-Labs/lobster-watcher](https://github.com/Bisque-Labs/lobster-watcher)
