## Lobster Watcher — Usage Guidelines

When the user asks about running agents, wants to see the dashboard, or types `/watcher`, guide them to the lobster-watcher observability interface.

**What to tell users:**

- The dashboard lives at `http://localhost/watcher/` (or through an SSH tunnel from their local machine)
- It shows all currently running and recently completed background agent sessions as a timeline and 3D view
- Live updates are pushed via SSE — no manual refresh needed

**Checking agent status without the dashboard:**

Use `get_active_sessions` MCP tool to get current session data programmatically. For a visual overview, point users to the dashboard URL.

**SSH tunnel for remote access:**

If the user is on a remote machine, they can tunnel in with:
```bash
ssh -L 8080:localhost:80 admin@<lobster-host>
```
Then open `http://localhost:8080/watcher/` locally.

**If the dashboard is not responding:**

1. Check the wire server: `curl http://localhost:8765/health`
2. Check the wire server process: `ps aux | grep wire_server`
3. Check nginx: `sudo systemctl status nginx`
4. View wire server logs: `sudo journalctl -u lobster-wire -f`

**Installing or updating:**

Tell the user to run: `bash ~/lobster/lobster-shop/lobster-watcher/install.sh`

This is idempotent — safe to re-run for updates.
