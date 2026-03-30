# Obsidian KM Skill

Knowledge management integration for Lobster using Obsidian and CouchDB-powered sync.

## Overview

This skill enables Lobster to:
- Sync Obsidian vaults across devices using self-hosted CouchDB
- Access and search knowledge base contents
- Create and update notes from Telegram conversations

## Components

### CouchDB (BIS-230)

Self-hosted CouchDB instance for Obsidian LiveSync:

- **Location**: Runs as systemd user service
- **Port**: 5984 (localhost only)
- **Data**: `~/obsidian-vault/.couchdb/`
- **Config**: `~/lobster-config/obsidian.env`

### TLS Proxy (BIS-232)

Caddy reverse proxy for secure external access:

- **External URL**: `https://[server-ip]:6984/`
- **Internal target**: `http://127.0.0.1:5984`
- **TLS**: Self-signed certificate (auto-managed by Caddy)
- **Config**: `/etc/caddy/Caddyfile.d/couchdb-proxy.caddyfile`

## Installation

### Prerequisites

- Ubuntu/Debian-based system
- `sudo` access (for apt package installation)
- `systemd` with user services enabled

### Install CouchDB

```bash
cd /path/to/lobster/lobster-shop/obsidian-km
chmod +x install.sh
./install.sh
```

The installer is idempotent - safe to run multiple times.

### Verify Installation

```bash
# Check CouchDB service status
systemctl --user status couchdb

# Test CouchDB response (local)
source ~/lobster-config/obsidian.env
curl -s "http://${COUCHDB_USER}:${COUCHDB_PASSWORD}@127.0.0.1:5984/"

# Check Caddy service status
sudo systemctl status caddy

# Test HTTPS proxy (replace with your server IP)
curl -k https://YOUR_SERVER_IP:6984/
```

## Configuration

Configuration is stored in `~/lobster-config/obsidian.env`:

| Variable | Description |
|----------|-------------|
| `COUCHDB_USER` | CouchDB admin username |
| `COUCHDB_PASSWORD` | CouchDB admin password |
| `COUCHDB_HOST` | Bind address (127.0.0.1) |
| `COUCHDB_PORT` | Port (5984) |
| `OBSIDIAN_DATABASE` | Database name for LiveSync |
| `OBSIDIAN_VAULT_PATH` | Local vault directory |

## Service Management

```bash
# Start service
systemctl --user start couchdb

# Stop service
systemctl --user stop couchdb

# Restart service
systemctl --user restart couchdb

# View logs
journalctl --user -u couchdb -f

# Check if enabled at boot
systemctl --user is-enabled couchdb
```

## Boot Persistence

The installer enables `loginctl enable-linger` for the user account, ensuring the CouchDB service starts at boot without requiring a login session.

## Security

- CouchDB binds to `127.0.0.1` only (not exposed to the network)
- External access goes through Caddy TLS proxy on port 6984
- Port 5984 is blocked externally (firewall rule)
- Port 6984 is allowed externally for HTTPS access
- Caddy uses self-signed TLS certificates (clients must accept or trust)
- Admin credentials are stored with `chmod 600` in `~/lobster-config/obsidian.env`
- Anonymous access is disabled

## Directory Structure

```
lobster-shop/obsidian-km/
├── install.sh                    # Main installer script
├── README.md                     # This file
├── config/
│   ├── obsidian.env.template    # Config file template
│   └── Caddyfile.obsidian       # Caddy TLS proxy config
├── scripts/
│   └── setup-tls-proxy.sh       # TLS proxy setup script
└── services/
    └── couchdb.service          # Systemd user service unit
```

## Related Issues

- **BIS-228**: Obsidian KM Skill (epic)
- **BIS-230**: Install CouchDB on Lobster server
- **BIS-232**: Set up HTTPS/TLS for CouchDB endpoint

## Troubleshooting

### CouchDB won't start

1. Check logs: `journalctl --user -u couchdb -n 50`
2. Verify port is free: `ss -tlnp | grep 5984`
3. Check CouchDB config: `cat /opt/couchdb/etc/local.ini`

### Authentication errors

1. Verify credentials in `~/lobster-config/obsidian.env`
2. Check admin config: `sudo grep -A5 '\[admins\]' /opt/couchdb/etc/local.ini`
3. Restart service after config changes: `systemctl --user restart couchdb`

### Service not starting at boot

1. Check linger status: `ls /var/lib/systemd/linger/`
2. Enable linger: `loginctl enable-linger $USER`
3. Verify service is enabled: `systemctl --user is-enabled couchdb`

### HTTPS proxy not working

1. Check Caddy status: `sudo systemctl status caddy`
2. Check Caddy logs: `sudo journalctl -u caddy -n 50`
3. Verify port 6984 is listening: `ss -tlnp | grep 6984`
4. Test locally: `curl -k https://127.0.0.1:6984/`
5. Check firewall: `sudo ufw status | grep 6984`

### TLS certificate issues

Caddy uses self-signed certificates by default. Clients must either:
- Accept the self-signed certificate (e.g., `curl -k`)
- Trust the Caddy root CA (for Obsidian LiveSync, this is handled automatically)
