# Obsidian Client Setup Guide

Connect any Obsidian client to the self-hosted CouchDB sync server.

## Prerequisites

Before connecting a client, ensure:

1. **CouchDB server is running** on `178.104.15.109:6984`
2. **Credentials are configured** in `~/lobster-config/obsidian.env`
3. **Database `obsidian` exists** on the server

### Verify Server Status

```bash
# Check CouchDB is responding (from any machine with network access)
curl -k https://178.104.15.109:6984/

# Expected response:
# {"couchdb":"Welcome","version":"3.x.x",...}

# Verify database exists
curl -k -u "${COUCHDB_USER}:${COUCHDB_PASSWORD}" https://178.104.15.109:6984/obsidian

# Expected response:
# {"db_name":"obsidian","doc_count":...}
```

---

## Plugin Installation

The sync plugin is **Self-hosted LiveSync** by vrtmrz.

| Property | Value |
|----------|-------|
| Plugin ID | `obsidian-livesync` |
| Author | vrtmrz |
| Repository | https://github.com/vrtmrz/obsidian-livesync |

### Install via Community Plugins

1. Open Obsidian → **Settings** (gear icon)
2. Navigate to **Community plugins**
3. Click **Browse** (or disable Restricted Mode first if prompted)
4. Search for **"Self-hosted LiveSync"**
5. Click **Install**, then **Enable**

### Install Manually (if Community Plugins unavailable)

```bash
# Clone the plugin into your vault's plugins directory
cd /path/to/your/vault/.obsidian/plugins
git clone https://github.com/vrtmrz/obsidian-livesync.git obsidian-livesync
cd obsidian-livesync
npm install && npm run build
```

Restart Obsidian and enable the plugin in Settings → Community plugins.

---

## Connection Settings

After enabling the plugin, configure the connection:

### Open Plugin Settings

1. **Settings** → **Community plugins** → **Self-hosted LiveSync** → **Options**
2. Or use command palette: `Ctrl/Cmd + P` → "Open setting dialog"

### Required Settings

| Setting | Value |
|---------|-------|
| **Remote Database URI** | `https://178.104.15.109:6984` |
| **Database name** | `obsidian` |
| **Username** | Value of `${COUCHDB_USER}` from `~/lobster-config/obsidian.env` |
| **Password** | Value of `${COUCHDB_PASSWORD}` from `~/lobster-config/obsidian.env` |

### Retrieve Credentials

```bash
# On the server (178.104.15.109), source the env file
source ~/lobster-config/obsidian.env
echo "Username: $COUCHDB_USER"
echo "Password: $COUCHDB_PASSWORD"
```

### Recommended Settings

| Setting | Value | Notes |
|---------|-------|-------|
| **End-to-end Encryption** | Disabled | Enable if desired, but all clients must share the passphrase |
| **Live Sync** | Enabled | Real-time sync when online |
| **Periodic Sync** | Every 5 minutes | Fallback for intermittent connections |
| **Sync on Start** | Enabled | Pull changes when Obsidian opens |
| **Sync on Save** | Enabled | Push changes immediately on file save |

---

## Platform-Specific Instructions

### iOS (iPhone / iPad)

1. **Install Obsidian** from the App Store
2. **Open or create a vault** (local vault first)
3. **Enable Community Plugins:**
   - Settings → Community plugins → Turn off Restricted Mode
   - Browse → Search "Self-hosted LiveSync" → Install → Enable
4. **Configure connection:**
   - Settings → Self-hosted LiveSync → Remote Database
   - Enter: `https://178.104.15.109:6984`
   - Database: `obsidian`
   - Enter username and password
5. **Test connection:** Click "Test" or "Check database configuration"
6. **Initial sync:** Choose "Rebuild everything" on first device, "Receive" on subsequent devices

**iOS Notes:**
- Background sync is limited; open Obsidian to trigger sync
- Large vaults may take several minutes on first sync over cellular

### Android

1. **Install Obsidian** from Google Play Store
2. **Open or create a vault**
3. **Enable Community Plugins:**
   - Settings → Community plugins → Turn off Restricted Mode
   - Browse → Search "Self-hosted LiveSync" → Install → Enable
4. **Configure connection:**
   - Settings → Self-hosted LiveSync → Remote Database
   - Enter: `https://178.104.15.109:6984`
   - Database: `obsidian`
   - Enter username and password
5. **Test connection:** Click "Test"
6. **Initial sync:** Choose appropriate sync direction

**Android Notes:**
- Grant Obsidian storage permissions if prompted
- Battery optimization may delay background sync; consider excluding Obsidian

### macOS

1. **Install Obsidian** from obsidian.md or Homebrew: `brew install --cask obsidian`
2. **Open or create a vault**
3. **Enable Community Plugins:**
   - Settings (Cmd + ,) → Community plugins → Turn off Restricted Mode
   - Browse → Search "Self-hosted LiveSync" → Install → Enable
4. **Configure connection:**
   - Settings → Self-hosted LiveSync → Remote Database
   - Enter: `https://178.104.15.109:6984`
   - Database: `obsidian`
   - Enter username and password
5. **Test connection:** Click "Test"
6. **Initial sync:** Choose sync direction

**macOS Verification:**
```bash
# Test connectivity from terminal
curl -k -u "USERNAME:PASSWORD" https://178.104.15.109:6984/obsidian

# Check if vault is syncing (look for _local databases)
ls -la ~/path/to/vault/.obsidian/plugins/obsidian-livesync/
```

### Windows

1. **Install Obsidian** from obsidian.md or winget: `winget install Obsidian.Obsidian`
2. **Open or create a vault**
3. **Enable Community Plugins:**
   - Settings (Ctrl + ,) → Community plugins → Turn off Restricted Mode
   - Browse → Search "Self-hosted LiveSync" → Install → Enable
4. **Configure connection:**
   - Settings → Self-hosted LiveSync → Remote Database
   - Enter: `https://178.104.15.109:6984`
   - Database: `obsidian`
   - Enter username and password
5. **Test connection:** Click "Test"
6. **Initial sync:** Choose sync direction

**Windows Verification (PowerShell):**
```powershell
# Test connectivity
Invoke-WebRequest -Uri "https://178.104.15.109:6984/" -SkipCertificateCheck

# Test with auth
$cred = Get-Credential
Invoke-WebRequest -Uri "https://178.104.15.109:6984/obsidian" -Credential $cred -SkipCertificateCheck
```

---

## CLI Verification Commands

Run these commands to verify each step of the setup process.

### 1. Verify Network Connectivity

```bash
# Check port is reachable
nc -zv 178.104.15.109 6984

# Or with timeout
timeout 5 bash -c 'cat < /dev/tcp/178.104.15.109/6984' && echo "Port open" || echo "Port closed"
```

### 2. Verify CouchDB is Responding

```bash
# Basic health check (ignore self-signed cert warning)
curl -k https://178.104.15.109:6984/

# Expected: {"couchdb":"Welcome",...}
```

### 3. Verify Authentication Works

```bash
# Replace with actual credentials
curl -k -u "YOUR_USER:YOUR_PASSWORD" https://178.104.15.109:6984/_session

# Expected: {"ok":true,"userCtx":{"name":"YOUR_USER",...}}
```

### 4. Verify Database Access

```bash
# Check database exists and is accessible
curl -k -u "YOUR_USER:YOUR_PASSWORD" https://178.104.15.109:6984/obsidian

# Expected: {"db_name":"obsidian","doc_count":...}
```

### 5. Verify Write Permissions

```bash
# Create a test document
curl -k -X POST \
  -u "YOUR_USER:YOUR_PASSWORD" \
  -H "Content-Type: application/json" \
  -d '{"_id":"test-doc","test":true}' \
  https://178.104.15.109:6984/obsidian

# Expected: {"ok":true,"id":"test-doc","rev":"1-..."}

# Clean up test document
curl -k -X DELETE \
  -u "YOUR_USER:YOUR_PASSWORD" \
  "https://178.104.15.109:6984/obsidian/test-doc?rev=REV_FROM_ABOVE"
```

### 6. Verify CORS Configuration

```bash
# Check CORS headers are present
curl -k -I -X OPTIONS \
  -H "Origin: app://obsidian.md" \
  -H "Access-Control-Request-Method: GET" \
  https://178.104.15.109:6984/obsidian

# Should include:
# Access-Control-Allow-Origin: *
# Access-Control-Allow-Methods: GET, POST, PUT, DELETE
```

---

## Troubleshooting

### Error 1: Certificate Not Trusted

**Symptoms:**
- "SSL certificate problem" in curl
- "Certificate error" or "Connection not private" in Obsidian
- Plugin shows "Network error" or "HTTPS error"

**Cause:** Self-signed certificate is not trusted by the client.

**Solutions:**

1. **Accept the certificate in plugin settings:**
   - Some versions of LiveSync have "Ignore certificate errors" option
   - Enable it in Settings → Self-hosted LiveSync → Advanced

2. **Add certificate to system trust store (macOS):**
   ```bash
   # Download the certificate
   openssl s_client -connect 178.104.15.109:6984 -showcerts </dev/null 2>/dev/null | \
     openssl x509 -outform PEM > /tmp/couchdb-cert.pem

   # Add to Keychain (will prompt for password)
   sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain /tmp/couchdb-cert.pem
   ```

3. **Add certificate to system trust store (Linux):**
   ```bash
   # Download and install
   openssl s_client -connect 178.104.15.109:6984 -showcerts </dev/null 2>/dev/null | \
     openssl x509 -outform PEM | sudo tee /usr/local/share/ca-certificates/couchdb.crt
   sudo update-ca-certificates
   ```

4. **Windows:** Import the certificate via `certmgr.msc` → Trusted Root Certification Authorities

### Error 2: CORS Error

**Symptoms:**
- "CORS error" in plugin
- "Blocked by CORS policy" in browser console
- Sync fails but curl commands work

**Cause:** CouchDB CORS configuration doesn't allow Obsidian's origin.

**Solutions:**

1. **Verify CORS is enabled on server:**
   ```bash
   curl -k -u "admin:password" https://178.104.15.109:6984/_node/_local/_config/httpd/enable_cors
   # Should return: "true"
   ```

2. **Check allowed origins:**
   ```bash
   curl -k -u "admin:password" https://178.104.15.109:6984/_node/_local/_config/cors/origins
   # Should return: "*" or include "app://obsidian.md"
   ```

3. **Fix on server (if you have admin access):**
   ```bash
   # Enable CORS
   curl -k -X PUT -u "admin:password" \
     https://178.104.15.109:6984/_node/_local/_config/httpd/enable_cors \
     -d '"true"'

   # Allow all origins
   curl -k -X PUT -u "admin:password" \
     https://178.104.15.109:6984/_node/_local/_config/cors/origins \
     -d '"*"'

   # Allow credentials
   curl -k -X PUT -u "admin:password" \
     https://178.104.15.109:6984/_node/_local/_config/cors/credentials \
     -d '"true"'
   ```

### Error 3: 401 Unauthorized

**Symptoms:**
- "401 Unauthorized" error
- "Authentication required" message
- "Wrong username or password"

**Cause:** Incorrect credentials or user doesn't exist.

**Solutions:**

1. **Verify credentials are correct:**
   ```bash
   # Source the env file and test
   source ~/lobster-config/obsidian.env
   curl -k -u "${COUCHDB_USER}:${COUCHDB_PASSWORD}" https://178.104.15.109:6984/_session
   ```

2. **Check for typos:** Copy-paste credentials directly from the env file

3. **Verify user exists on server:**
   ```bash
   curl -k -u "admin:admin_password" https://178.104.15.109:6984/_users/org.couchdb.user:YOUR_USER
   ```

4. **Reset password if needed (server admin):**
   ```bash
   # Create or update user
   curl -k -X PUT -u "admin:admin_password" \
     -H "Content-Type: application/json" \
     https://178.104.15.109:6984/_users/org.couchdb.user:obsidian_user \
     -d '{"name":"obsidian_user","password":"new_password","roles":[],"type":"user"}'
   ```

### Error 4: Database Not Found (404)

**Symptoms:**
- "Database not found" error
- "404 Object Not Found"
- `{"error":"not_found","reason":"Database does not exist."}`

**Cause:** The `obsidian` database hasn't been created.

**Solutions:**

1. **Check if database exists:**
   ```bash
   curl -k -u "YOUR_USER:YOUR_PASSWORD" https://178.104.15.109:6984/_all_dbs
   # Should include "obsidian" in the list
   ```

2. **Create the database (if missing):**
   ```bash
   curl -k -X PUT -u "admin:admin_password" https://178.104.15.109:6984/obsidian
   # Expected: {"ok":true}
   ```

3. **Grant user access to database:**
   ```bash
   curl -k -X PUT -u "admin:admin_password" \
     -H "Content-Type: application/json" \
     https://178.104.15.109:6984/obsidian/_security \
     -d '{"admins":{"names":[],"roles":[]},"members":{"names":["obsidian_user"],"roles":[]}}'
   ```

### Error 5: Sync Stuck / Not Progressing

**Symptoms:**
- Sync indicator spins indefinitely
- "Syncing..." message never completes
- Document count doesn't increase
- Changes on one device don't appear on another

**Cause:** Various — network issues, conflicts, or plugin state corruption.

**Solutions:**

1. **Check sync status in plugin:**
   - Open command palette → "Show sync log"
   - Look for error messages or stuck operations

2. **Force a full resync:**
   - Settings → Self-hosted LiveSync → Sync Settings
   - Click "Rebuild everything" (WARNING: may cause data loss if conflicts exist)

3. **Check for conflicts:**
   ```bash
   # List conflicted documents
   curl -k -u "YOUR_USER:YOUR_PASSWORD" \
     'https://178.104.15.109:6984/obsidian/_all_docs?conflicts=true' | \
     jq '.rows[] | select(.doc._conflicts)'
   ```

4. **Restart the sync:**
   - Disable the plugin → Restart Obsidian → Re-enable the plugin

5. **Clear local sync state:**
   - Close Obsidian
   - Delete: `YOUR_VAULT/.obsidian/plugins/obsidian-livesync/data.json`
   - Reopen Obsidian and reconfigure

6. **Check server health:**
   ```bash
   # Verify CouchDB is healthy
   curl -k https://178.104.15.109:6984/_up
   # Expected: {"status":"ok",...}

   # Check compaction status
   curl -k -u "admin:password" https://178.104.15.109:6984/obsidian
   # Look at "compact_running" field
   ```

---

## First Device vs. Additional Devices

### First Device (Source of Truth)

When connecting the **first device** with an existing vault:

1. Configure plugin settings as above
2. Click **"Test"** to verify connection
3. Choose **"Rebuild everything → Send"** to push your vault to the server
4. Wait for initial sync to complete (may take several minutes for large vaults)

### Additional Devices

When connecting **subsequent devices**:

1. Create a **new empty vault** (or use existing empty vault)
2. Configure plugin settings identically
3. Click **"Test"** to verify connection
4. Choose **"Rebuild everything → Receive"** to pull from server
5. Wait for sync to complete

**Never choose "Send" on a second device** unless you want to overwrite the server with that device's content.

---

## Security Notes

- **Credentials:** Never commit credentials to version control. Always use environment variables or secure credential storage.
- **Network:** The connection uses HTTPS but with a self-signed certificate. For production, consider using Let's Encrypt.
- **Firewall:** Port 6984 should only be accessible from trusted networks. Consider VPN for remote access.
- **Encryption:** Enable end-to-end encryption in the plugin if syncing sensitive notes. All clients must use the same passphrase.

---

## Quick Reference Card

| Item | Value |
|------|-------|
| Server URL | `https://178.104.15.109:6984` |
| Database | `obsidian` |
| Plugin | Self-hosted LiveSync (`obsidian-livesync`) |
| Credentials | See `~/lobster-config/obsidian.env` |
| Test command | `curl -k https://178.104.15.109:6984/` |
| Auth test | `curl -k -u "USER:PASS" https://178.104.15.109:6984/_session` |
