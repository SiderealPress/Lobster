# Obsidian LiveSync Plugin Setup

This guide explains how to configure the Obsidian LiveSync plugin to connect to the CouchDB server installed by the Obsidian KM skill.

## Prerequisites

- CouchDB installed and running (BIS-230: `install.sh`)
- CouchDB configured for LiveSync (BIS-231: `scripts/configure-couchdb.sh`)
- HTTPS proxy configured for external access (BIS-232)
- Obsidian installed on your device(s)

## Install the LiveSync Plugin

1. Open Obsidian Settings
2. Go to **Community Plugins** → **Browse**
3. Search for **"Self-hosted LiveSync"**
4. Install and enable the plugin

## Connection Settings

### Server Configuration

Copy these values into the LiveSync plugin settings:

| Setting | Value |
|---------|-------|
| **Remote Type** | CouchDB |
| **Server URL** | `https://YOUR_SERVER_IP:6984` |
| **Database name** | `obsidian-livesync` |
| **Username** | *(see credentials below)* |
| **Password** | *(see credentials below)* |

### Getting Your Credentials

Your CouchDB credentials are stored in `~/lobster-config/obsidian.env`:

```bash
# On your server, run:
cat ~/lobster-config/obsidian.env | grep -E "COUCHDB_USER|COUCHDB_PASSWORD"
```

This will output something like:
```
COUCHDB_USER=admin
COUCHDB_PASSWORD=your-generated-password
```

### Server URL Format

The HTTPS proxy (BIS-232) exposes CouchDB on port 6984 with TLS:

- **Local access** (same machine): `http://127.0.0.1:5984`
- **External access** (other devices): `https://YOUR_SERVER_IP:6984`

Replace `YOUR_SERVER_IP` with your server's public IP address or domain name.

## Recommended Security Settings

### Enable End-to-End Encryption (Strongly Recommended)

LiveSync supports E2E encryption, which encrypts all vault content before it leaves your device:

1. In LiveSync settings, go to **Encryption**
2. Enable **End-to-End Encryption**
3. Set a strong **Passphrase** (store this securely - you'll need it on all devices)
4. The passphrase is NOT stored on the server - only you can decrypt your notes

### Why Use E2E Encryption?

- Even if the server is compromised, your notes remain encrypted
- CouchDB admin credentials cannot decrypt your vault content
- Only devices with the passphrase can read/write notes

## Sync Configuration

### Initial Setup on First Device

1. Configure the connection settings above
2. Set sync mode to **"LiveSync"** for real-time sync
3. Click **Test Database Connection** to verify
4. If successful, click **"Rebuild everything"** to initialize sync

### Setup on Additional Devices

1. Install Obsidian and the LiveSync plugin
2. Enter the SAME connection settings and passphrase
3. Click **"Fetch remote database"** (NOT rebuild)
4. Your vault will sync from the server

### Sync Mode Options

| Mode | Description | Best For |
|------|-------------|----------|
| **LiveSync** | Real-time sync on every change | Desktop with stable internet |
| **Periodic** | Sync every X minutes | Mobile devices, battery savings |
| **On-demand** | Manual sync only | Offline-first usage |

## Verification

### Test the Connection

In the LiveSync plugin:

1. Go to plugin settings
2. Click **"Test Database Connection"**
3. You should see: "Connected to CouchDB" (or similar success message)

### Verify on Server

```bash
# Check database exists
curl -s http://admin:PASSWORD@127.0.0.1:5984/obsidian-livesync | jq .

# Check document count (after initial sync)
curl -s http://admin:PASSWORD@127.0.0.1:5984/obsidian-livesync | jq '.doc_count'
```

## Troubleshooting

### Connection Refused

- Verify CouchDB is running: `systemctl --user status couchdb`
- Verify HTTPS proxy is running (BIS-232)
- Check firewall allows port 6984: `sudo ufw status`

### Authentication Failed

- Double-check username/password from `~/lobster-config/obsidian.env`
- Ensure no extra spaces or quotes when copying credentials

### CORS Errors

- Run `scripts/configure-couchdb.sh` again to ensure CORS is configured
- Restart CouchDB: `systemctl --user restart couchdb`

### SSL/Certificate Errors

- If using a self-signed certificate, enable **"Accept invalid SSL certificate"** in LiveSync settings
- Better: use Let's Encrypt for a valid certificate (see BIS-232)

### Sync Stuck or Slow

- Check CouchDB logs: `journalctl --user -u couchdb -f`
- Large vaults may take time on initial sync
- Consider increasing `max_document_size` if syncing large attachments

## Database Maintenance

### View Database Stats

```bash
# Database info
curl -s http://admin:PASSWORD@127.0.0.1:5984/obsidian-livesync | jq .

# Document count
curl -s http://admin:PASSWORD@127.0.0.1:5984/obsidian-livesync/_all_docs | jq '.total_rows'
```

### Compact Database (reclaim disk space)

```bash
curl -X POST http://admin:PASSWORD@127.0.0.1:5984/obsidian-livesync/_compact
```

### Backup Database

```bash
# Export to JSON
curl http://admin:PASSWORD@127.0.0.1:5984/obsidian-livesync/_all_docs?include_docs=true > backup.json

# Or use CouchDB replication to another server
```

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                        YOUR DEVICES                             │
├─────────────────┬─────────────────┬─────────────────────────────┤
│   Desktop       │    Laptop       │      Mobile                 │
│   Obsidian      │    Obsidian     │      Obsidian               │
│   + LiveSync    │    + LiveSync   │      + LiveSync             │
└────────┬────────┴────────┬────────┴────────┬────────────────────┘
         │                 │                 │
         │    HTTPS (port 6984)              │
         │    E2E Encrypted                  │
         └────────────────┬──────────────────┘
                          │
┌─────────────────────────▼───────────────────────────────────────┐
│                     LOBSTER SERVER                              │
├─────────────────────────────────────────────────────────────────┤
│   ┌──────────────────────────────────────────────────────────┐  │
│   │  HTTPS Proxy (Caddy/nginx)  - Port 6984                  │  │
│   │  TLS termination, authentication                         │  │
│   └──────────────────────┬───────────────────────────────────┘  │
│                          │                                      │
│   ┌──────────────────────▼───────────────────────────────────┐  │
│   │  CouchDB  - Port 5984 (localhost only)                   │  │
│   │  Database: obsidian-livesync                             │  │
│   │  CORS enabled, reduce_limit=false                        │  │
│   └──────────────────────────────────────────────────────────┘  │
│                                                                 │
│   Data: ~/obsidian-vault/.couchdb/                              │
│   Config: ~/lobster-config/obsidian.env                         │
└─────────────────────────────────────────────────────────────────┘
```

## Related Issues

- **BIS-228**: Obsidian KM Skill (epic)
- **BIS-230**: Install CouchDB
- **BIS-231**: Configure CouchDB + LiveSync database (this document)
- **BIS-232**: HTTPS proxy for external access
- **BIS-233**: Create vault structure
