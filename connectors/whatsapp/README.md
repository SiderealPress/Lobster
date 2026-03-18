# Lobster WhatsApp Bridge

Connects Lobster to WhatsApp using [Baileys](https://github.com/WhiskeySockets/Baileys) — a direct WebSocket implementation of the WhatsApp Web multi-device protocol. No browser, no Puppeteer, no Chromium.

---

## What this connector does

Incoming WhatsApp DMs (and @mentions in groups) are written to Lobster's inbox as JSON files, identical in structure to Telegram messages. Outgoing replies written by Lobster are picked up and sent via WhatsApp.

Two services work together:

| Service | Language | Role |
|---------|----------|------|
| `lobster-whatsapp-bridge` | Node.js | Speaks WhatsApp protocol; reads/writes JSON files |
| `lobster-whatsapp-adapter` | Python | Normalizes bridge events → Lobster inbox format |

---

## Prerequisites

- **Node.js 18+** — check with `node --version`
- **npm** — bundled with Node.js
- **A WhatsApp account** — the bridge links as a "companion device" (no separate number needed)

No Chromium, no browser, no Twilio, no Meta Business account required.

---

## Setup (3 steps)

### Step 1 — Run the setup script

From the lobster repo root:

```bash
bash connectors/whatsapp/setup.sh
```

This installs npm dependencies, copies the systemd user service files, and creates `~/.config/lobster/whatsapp.env` from the example config.

### Step 2 — Scan the QR code

```bash
systemctl --user start lobster-whatsapp-bridge
journalctl --user -u lobster-whatsapp-bridge -f
```

A QR code will appear in the log. Scan it with WhatsApp:

> **WhatsApp → Settings → Linked Devices → Link a Device**

After scanning, the bridge prints your JID:

```
[READY] Detected Lobster JID: 15551234567@c.us
```

### Step 3 — Set your JID and start the adapter

Edit `~/.config/lobster/whatsapp.env` and add:

```bash
WHATSAPP_LOBSTER_JID=15551234567@c.us
```

Then restart the bridge and start the adapter:

```bash
systemctl --user restart lobster-whatsapp-bridge
systemctl --user start lobster-whatsapp-adapter
```

Send yourself a WhatsApp DM and verify it appears in `check_inbox()`. Done.

---

## Config reference

All settings are optional. Edit `~/.config/lobster/whatsapp.env` (this file is NOT committed to git):

| Variable | Default | Description |
|----------|---------|-------------|
| `WHATSAPP_SESSION_PATH` | `~/.config/lobster/whatsapp-session` | Where Baileys stores auth credentials |
| `WHATSAPP_LOBSTER_JID` | *(none)* | Lobster's WhatsApp JID — required for group @mention filtering |
| `WHATSAPP_ALLOWED_JIDS` | *(empty = allow all)* | Comma-separated whitelist of sender JIDs |
| `WA_EVENTS_DIR` | `~/messages/wa-events` | Incoming event JSON files (bridge → adapter) |
| `WA_COMMANDS_DIR` | `~/messages/wa-commands` | Outgoing command JSON files (adapter → bridge) |
| `WA_HEARTBEAT_FILE` | `~/lobster-workspace/logs/whatsapp-heartbeat` | Heartbeat timestamp for health monitoring |

See `connectors/whatsapp/config.example.env` for the full annotated example.

---

## Re-authenticating when session expires

If WhatsApp logs out the linked device (rare, but happens after extended inactivity):

```bash
# Stop the service
systemctl --user stop lobster-whatsapp-bridge

# Delete the saved session
rm -rf ~/.config/lobster/whatsapp-session

# Start and scan the QR code again
systemctl --user start lobster-whatsapp-bridge
journalctl --user -u lobster-whatsapp-bridge -f
```

The bridge automatically emits a `session_expired` event to Lobster's inbox when this happens, so you'll get a Telegram notification to re-scan.

---

## Logs and health

```bash
# Live bridge logs (includes QR code on first run)
journalctl --user -u lobster-whatsapp-bridge -f

# Bridge log file
tail -f ~/lobster-workspace/logs/whatsapp-bridge.log

# Adapter log
tail -f ~/lobster-workspace/logs/whatsapp-adapter.log

# Service status
systemctl --user status lobster-whatsapp-bridge
systemctl --user status lobster-whatsapp-adapter
```

---

## Architecture

```
WhatsApp network
    ↓ (WebSocket, no browser)
Baileys (Node.js) in lobster-whatsapp-bridge
    ↓ writes JSON to ~/messages/wa-events/
whatsapp_bridge_adapter.py in lobster-whatsapp-adapter
    ↓ normalizes to Lobster inbox schema
~/messages/inbox/<msg_id>.json
    ↓ Lobster calls check_inbox()
Lobster processes and calls send_reply(source='whatsapp', ...)
    ↓ reply written to ~/messages/outbox/
whatsapp_bridge_adapter.py
    ↓ converts to ~/messages/wa-commands/<ts>_wa_cmd.json
Baileys reads command, calls sock.sendMessage()
    ↓
WhatsApp delivers the reply
```

---

## Why Baileys instead of whatsapp-web.js

The WhatsApp health check script (`~/lobster/scripts/whatsapp-health-check.sh`) monitors the bridge:

- If `lobster-whatsapp-bridge` is not `active`, an alert is written to the Lobster inbox so Lobster can notify you.
- If no WhatsApp messages have been received for more than 10 minutes (based on the heartbeat file at `~/lobster-workspace/logs/whatsapp-heartbeat`), a warning is logged.

Run this script manually or wire it to your cron schedule to enable monitoring.

---

## Troubleshooting

### QR code not appearing

- Confirm Node.js 18+ is installed: `node --version`
- Confirm Chromium is installed and accessible
- Check logs: `journalctl -u lobster-whatsapp-bridge -f`

### Service fails to start

```bash
sudo systemctl status lobster-whatsapp-bridge
journalctl -u lobster-whatsapp-bridge --no-pager -n 50
```

Common causes:
- `node` binary not at `/usr/bin/node` — check with `which node` and update the service file if needed
- Bridge directory missing or `npm install` not run
- Chromium not installed

### Messages not reaching Lobster

- Verify the `LOBSTER_MESSAGES_DIR` environment variable points to the correct inbox directory (`~/messages/inbox`)
- Check that the bridge process has write permission to that directory
- Review the bridge log for errors: `tail -100 ~/lobster-workspace/logs/whatsapp-bridge.log`

### Service keeps restarting

The service is configured with `Restart=always` and a 10-second backoff. If it loops rapidly:

1. Check for authentication errors (session may need re-scan)
2. Check for missing dependencies (`npm install` again)
3. Check for port or resource conflicts

---

## Re-authenticating When Session Expires

WhatsApp sessions can expire after extended inactivity or due to changes on the WhatsApp side.

1. Stop the service:
   ```bash
   sudo systemctl stop lobster-whatsapp-bridge
   ```

2. Delete the stored auth data:
   ```bash
   rm -rf /home/admin/lobster-workspace/projects/whatsapp-bridge/.wwebjs_auth
   ```

3. Start the service and scan the QR code again:
   ```bash
   sudo systemctl start lobster-whatsapp-bridge
   journalctl -u lobster-whatsapp-bridge -f
   ```

---

## File Layout

```
connectors/whatsapp/
  install.sh                       -- one-command setup script
  lobster-whatsapp-bridge.service  -- systemd unit file
  logrotate.conf                   -- log rotation config
  README.md                        -- this file

/home/admin/lobster-workspace/projects/whatsapp-bridge/
  index.js                         -- bridge entry point
  package.json
  .wwebjs_auth/                    -- persisted WhatsApp session (created at runtime)
```
