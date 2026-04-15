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

The previous bridge used `whatsapp-web.js` (Puppeteer + Chromium). Baileys is a direct WebSocket implementation:

| | Baileys (this bridge) | whatsapp-web.js (old) |
|-|-----------------------|-----------------------|
| Mechanism | Direct WebSocket | Chromium browser |
| Memory | ~100 MB | ~500 MB |
| Startup | ~3 seconds | ~20 seconds |
| Reconnect | Fast (WebSocket) | Slow (browser restart) |
| Session storage | JSON files | Browser LocalStorage |
| VPS compatibility | Excellent | Requires sandbox workarounds |
