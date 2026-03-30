# Obsidian KM Skill

Knowledge management integration using CouchDB for Lobster.

## Components

### CouchDB Health Check (BIS-235)

Monitors CouchDB service health and alerts via Telegram when unhealthy.

**Files:**
- `scripts/health-check.sh` - Health check script
- `services/couchdb-health.service` - systemd oneshot service
- `services/couchdb-health.timer` - systemd timer (runs every 2 minutes)

**Checks performed:**
1. CouchDB systemd service is running
2. Port 5984 is responding
3. Authentication is working

**Installation:**
```bash
bash ~/lobster/lobster-shop/obsidian-km/install.sh
```

**Prerequisites:**
- CouchDB running as user service (`couchdb.service`)
- Config file at `~/lobster-config/obsidian.env` with:
  ```
  COUCHDB_USER=admin
  COUCHDB_PASSWORD=your-secure-password
  ```

**Logs:**
- Health check log: `~/lobster-workspace/logs/couchdb-health.log`
- Alerts log: `~/lobster-workspace/logs/alerts.log`

**Commands:**
```bash
# View timer status
systemctl --user status couchdb-health.timer

# View service logs
journalctl --user -u couchdb-health.service -f

# Run health check manually
~/lobster/lobster-shop/obsidian-km/scripts/health-check.sh
```

## Related Issues

- BIS-228: Obsidian KM Skill (epic)
- BIS-230: CouchDB installation
- BIS-235: CouchDB health monitoring (this component)
