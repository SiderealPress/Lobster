# Lobster Upgrade Guide

How to pull the latest Lobster changes and redeploy lobster-watcher on an existing
installation. This guide covers the March 2026 release (Lobster `v0.1.0` /
lobster-watcher `v0.1.0`), but the steps apply to all future upgrades.

---

## What changed in this release

### Lobster (SiderealPress/Lobster)

| PR | Description |
|----|-------------|
| [#358](https://github.com/SiderealPress/Lobster/pull/358) | **Dedup fix** — `upsertSession()` merge strategy prevents duplicate agent-session rows. `check_inbox` now handles `subagent_notification` messages correctly; no more `[TELEGRAM] from Unknown` leaking to the dispatcher. |
| [#359](https://github.com/SiderealPress/Lobster/pull/359) | **BIS-78 Slice A backend** — Four new causality columns in `agent_sessions` (`notified_at`, `trigger_message_id`, `trigger_snippet`, `causality_chain`). Wire server SELECT updated to include them. Schema migration is idempotent — safe to run on an existing DB. |
| [#360](https://github.com/SiderealPress/Lobster/pull/360) | **Entrypoint guard** — All launch scripts (`claude-persistent.sh`, service templates, Docker entrypoints) now unset both `CLAUDECODE` **and** `CLAUDE_CODE_ENTRYPOINT`. Prevents "cannot launch inside another Claude Code session" errors in tmux/subagents. |

### lobster-watcher (Bisque-Labs/lobster-watcher)

| Change | Description |
|--------|-------------|
| Filter toggle fix | All filter buttons (Has Trigger, status buttons, Active Only, Show All) were stuck on second click due to event-listener accumulation. Fixed — listeners attach once; DOM mutates in place. |
| Message text in UI | Session cards now show the actual triggering message (`trigger_snippet`) as an italic quote, plus spawned-at and notified-at timestamps. |
| Causality columns | Wire server SELECT and `upsertSession()` both handle the new causality columns from PR #359. |
| Theme toggle | `applyTheme()` propagates the light/dark setting to the Three.js 3D scene — no more mismatched background color after switching themes. |
| Stale nginx assets | Build pipeline and nginx serve config updated so the browser always receives fresh assets after a redeploy. |

---

## Step 1 — Pull Lobster

```bash
cd /home/admin/lobster    # or wherever you cloned Lobster
git pull origin main
```

Expected: `cc944ab` or later at the tip of main.

---

## Step 2 — Re-run the installer

The installer is idempotent. It applies any new systemd service definitions,
script changes, and dependency updates:

```bash
./install.sh
```

The installer will:
- Update all scripts in `~/lobster/scripts/`
- Reload and restart `lobster-claude.service` and `lobster-router.service`
- Leave your existing config (`~/messages/`, `.env`, Telegram token) untouched

> **Note:** If you run Lobster inside Docker, rebuild the image instead:
> ```bash
> docker compose pull && docker compose up -d --build
> ```

---

## Step 3 — Verify the DB migration (causality columns)

PR #359 adds four columns to the `agent_sessions` table using a safe
`ALTER TABLE ... ADD COLUMN IF NOT EXISTS` pattern. The migration runs
automatically when Lobster starts — no manual SQL needed.

To confirm the columns are present after restart:

```bash
sqlite3 ~/messages/config/agent_sessions.db \
  "PRAGMA table_info(agent_sessions);" | grep -E 'notified_at|trigger_message_id|trigger_snippet|causality_chain'
```

You should see four rows. If the table is empty (fresh install), that is fine —
columns will be there on first insert.

---

## Step 4 — Pull and redeploy lobster-watcher

### 4a. Pull latest

```bash
cd $LOBSTER_PROJECTS/lobster-watcher   # default: ~/lobster-workspace/projects/lobster-watcher
git pull origin main
```

Expected: `33a4207` or later at the tip of main.

### 4b. Rebuild the frontend

The new causality columns and UI changes require a fresh build:

```bash
npm install   # picks up any new/updated dependencies
npm run build
```

### 4c. Redeploy static files

```bash
sudo rm -rf /var/www/html/watcher/*
sudo cp -r dist/* /var/www/html/watcher/
```

> **Why clear first?** Previous builds left stale hashed JS/CSS chunks on disk.
> nginx served them even after the HTML referenced new filenames. Clearing first
> ensures the browser always gets the fresh bundle.

### 4d. Restart the wire server

The wire server must reload to pick up the new SQL query (causality columns):

```bash
# If running as a systemd service:
sudo systemctl restart lobster-wire

# If running in tmux:
tmux send-keys -t lobster-wire C-c
# Then relaunch:
LOBSTER_DB_PATH=~/messages/config/agent_sessions.db \
  python3 wire-server/wire_server.py
```

---

## Step 5 — Verify everything works

1. **Lobster dispatcher** — Send yourself a message and confirm it responds.

2. **Agent sessions DB** — Spawn a short background task and check it appears
   in the sessions table:
   ```bash
   sqlite3 ~/messages/config/agent_sessions.db \
     "SELECT id, status, trigger_snippet FROM agent_sessions ORDER BY spawned_at DESC LIMIT 5;"
   ```

3. **Wire server health** — Confirm the wire server is up:
   ```bash
   curl -s http://localhost:8765/health
   # Expected: {"status":"ok",...}
   ```

4. **Dashboard** — Open `https://yourhost/watcher/` (or `http://localhost:5174`
   in dev mode). You should see:
   - Session cards with trigger-message text (italic quote) if any sessions exist
   - Filter buttons respond on repeated clicks (no stuck state)
   - Theme toggle changes the 3D scene background to match

5. **Entrypoint guard** — If you run nested tmux sessions or Docker workers, confirm
   no more "cannot launch inside another Claude Code session" errors:
   ```bash
   grep -r 'CLAUDE_CODE_ENTRYPOINT' /home/admin/lobster/scripts/
   # Should see `unset CLAUDE_CODE_ENTRYPOINT` in the launch scripts
   ```

---

## Rollback

If anything goes wrong, roll back with:

```bash
# Lobster
cd /home/admin/lobster
git checkout <previous-sha>
./install.sh

# lobster-watcher
cd $LOBSTER_PROJECTS/lobster-watcher
git checkout <previous-sha>
npm run build
sudo cp -r dist/* /var/www/html/watcher/
sudo systemctl restart lobster-wire
```

Previous commit SHAs are visible in `git log --oneline`.

---

## Getting help

- File an issue: [SiderealPress/Lobster](https://github.com/SiderealPress/Lobster/issues)
- lobster-watcher issues: [Bisque-Labs/lobster-watcher](https://github.com/Bisque-Labs/lobster-watcher/issues)
