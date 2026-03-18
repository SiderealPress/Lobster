# Lobster WhatsApp Bridge

A WhatsApp connector for the Lobster AI assistant, powered by [whatsapp-web.js](https://wwebjs.dev/).
It bridges incoming WhatsApp messages to Lobster's file-based inbox, and forwards Lobster replies back to WhatsApp.

---

## Prerequisites

- **Node.js 18+** — check with `node --version`
- **npm** — bundled with Node.js
- **Chromium or Google Chrome** — required by whatsapp-web.js for the headless browser session

### Install Node.js 18+ (if needed)

```bash
# Ubuntu / Debian
curl -fsSL https://deb.nodesource.com/setup_18.x | sudo -E bash -
sudo apt-get install -y nodejs

# macOS (Homebrew)
brew install node
```

### Install Chromium (if needed)

```bash
# Ubuntu / Debian
sudo apt-get install -y chromium-browser

# macOS (Homebrew)
brew install --cask chromium
```

---

## Installation

The bridge source lives in:

```
/home/admin/lobster-workspace/projects/whatsapp-bridge/
```

The connector files (service definition, install script, logrotate config) live in:

```
connectors/whatsapp/          (this directory, inside the lobster repo)
```

### One-command install

Run from the lobster repo root:

```bash
bash connectors/whatsapp/install.sh
```

This script:
1. Creates required log and command directories
2. Runs `npm install` inside the bridge directory
3. Copies the systemd service file to `/etc/systemd/system/`
4. Enables the service to start on boot

### Start the service

```bash
sudo systemctl start lobster-whatsapp-bridge
```

---

## First-Run QR Scan

On the first start (and after session expiry), whatsapp-web.js displays a QR code that you must scan with the WhatsApp mobile app to authenticate.

1. Start the service and tail the log:

   ```bash
   sudo systemctl start lobster-whatsapp-bridge
   journalctl -u lobster-whatsapp-bridge -f
   ```

2. A QR code will appear in the log output. Open WhatsApp on your phone.

3. Go to **Settings > Linked Devices > Link a Device** and scan the QR code.

4. Once authenticated, the log will show `Client is ready!` and the service will begin processing messages.

The authenticated session is persisted to disk (`.wwebjs_auth/` inside the bridge directory), so you will not need to scan again unless the session expires or the auth data is deleted.

---

## Verifying the Service

```bash
# Check current status
sudo systemctl status lobster-whatsapp-bridge

# Follow live logs
journalctl -u lobster-whatsapp-bridge -f

# Check the log file directly
tail -f ~/lobster-workspace/logs/whatsapp-bridge.log
```

---

## Log Rotation

A logrotate configuration is provided at `connectors/whatsapp/logrotate.conf`. To install it:

```bash
sudo cp connectors/whatsapp/logrotate.conf /etc/logrotate.d/lobster-whatsapp-bridge
```

This rotates the bridge log daily, keeping 7 compressed archives, and creates a fresh log file owned by the `admin` user.

---

## Health Monitoring

The Lobster health check script (`~/lobster/scripts/health-check-v3.sh`) automatically monitors the bridge:

- If `lobster-whatsapp-bridge` is not `active`, an alert is written to the Lobster inbox so Lobster can notify you.
- If no WhatsApp messages have been received for more than 1 hour (based on the heartbeat file at `~/lobster-workspace/logs/whatsapp-heartbeat`), a warning is logged.

The health check runs every 4 minutes via cron.

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
