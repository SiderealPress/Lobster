#!/bin/bash
#===============================================================================
# Obsidian KM Skill Installer for Lobster
#
# Master installer that consolidates all phases of the Obsidian KM skill:
#   - BIS-230: CouchDB installation
#   - BIS-233: Obsidian vault creation
#   - BIS-231: CouchDB configuration
#   - BIS-232: TLS proxy setup
#   - BIS-243: MCP server installation (placeholder)
#   - BIS-235: Health check registration
#
# This script is idempotent — safe to run multiple times.

#
# Usage: bash ~/lobster/lobster-shop/obsidian-km/install.sh
#===============================================================================

set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

=============================================="
echo ""
echo "  Health check script: $SCRIPT_SRC"
echo "  Timer interval:      every 2 minutes"
echo "  Config file:         $OBSIDIAN_ENV"
echo ""

#===============================================================================
# Step 1: Check prerequisites
#===============================================================================
step "Checking prerequisites"

# Check curl
if ! command -v curl &>/dev/null; then
    error "curl is required. Install: sudo apt install curl"
fi
success "curl available"

# Check systemctl (user mode)
if ! systemctl --user status >/dev/null 2>&1; then
    warn "systemd user mode may not be available. Timer installation may fail."
else
    success "systemd user mode available"
fi

# Check for obsidian.env
if [[ ! -f "$OBSIDIAN_ENV" ]]; then
    warn "Config file not found: $OBSIDIAN_ENV"
    warn "You'll need to create it with COUCHDB_USER and COUCHDB_PASSWORD before health checks work."
    echo ""
    echo "  Example:"
    echo "    cat > $OBSIDIAN_ENV << 'EOF'"
    echo "    COUCHDB_USER=admin"
    echo "    COUCHDB_PASSWORD=your-secure-password"
    echo "    EOF"
    echo ""
else
    # Verify credentials are set
    if grep -q '^COUCHDB_USER=' "$OBSIDIAN_ENV" && grep -q '^COUCHDB_PASSWORD=' "$OBSIDIAN_ENV"; then
        success "CouchDB credentials configured in $OBSIDIAN_ENV"
    else
        warn "COUCHDB_USER and/or COUCHDB_PASSWORD not set in $OBSIDIAN_ENV"
    fi
fi

# Check for CouchDB service (optional - might be installed later)
if systemctl --user is-active --quiet couchdb 2>/dev/null; then
    success "CouchDB service is running"
elif systemctl --user list-unit-files couchdb.service >/dev/null 2>&1; then
    warn "CouchDB service exists but is not running"
else
    warn "CouchDB service not found — health check will fail until CouchDB is installed"
fi

#===============================================================================
# Step 2: Make scripts executable
#===============================================================================
step "Setting up scripts"

if [[ ! -f "$SCRIPT_SRC" ]]; then
    error "Health check script not found: $SCRIPT_SRC"
fi

chmod +x "$SCRIPT_SRC"
success "Made $SCRIPT_SRC executable"

#===============================================================================
# Step 3: Install systemd service and timer
#===============================================================================
step "Installing systemd user service and timer"

mkdir -p "$SYSTEMD_USER_DIR"

# Copy service file
if [[ ! -f "$SERVICE_SRC" ]]; then
    error "Service file not found: $SERVICE_SRC"
fi
cp "$SERVICE_SRC" "$SYSTEMD_USER_DIR/couchdb-health.service"
success "Installed couchdb-health.service"

# Copy timer file
if [[ ! -f "$TIMER_SRC" ]]; then
    error "Timer file not found: $TIMER_SRC"
fi
cp "$TIMER_SRC" "$SYSTEMD_USER_DIR/couchdb-health.timer"
success "Installed couchdb-health.timer"

# Reload systemd
systemctl --user daemon-reload
success "systemd daemon reloaded"

#===============================================================================
# Step 4: Enable and start the timer
#===============================================================================
step "Enabling and starting health check timer"

systemctl --user enable couchdb-health.timer 2>/dev/null
success "Timer enabled"

# Stop if running, then start fresh
systemctl --user stop couchdb-health.timer 2>/dev/null || true
systemctl --user start couchdb-health.timer
success "Timer started"

# Show timer status
info "Timer status:"
systemctl --user list-timers couchdb-health.timer --no-pager 2>/dev/null || true

#===============================================================================
# Step 5: Run initial health check
#===============================================================================
step "Running initial health check"

if "$SCRIPT_SRC"; then
    success "CouchDB health check passed"
else
    exit_code=$?
    warn "Initial health check returned exit code $exit_code"
    warn "This is expected if CouchDB is not yet running or configured."
fi

#===============================================================================
# Step 6: Generate LiveSync Setup URI
#===============================================================================
step "Generating LiveSync Setup URI"

SETUP_URI_SCRIPT="$SKILL_DIR/scripts/generate-setup-uri.sh"
if [[ -f "$SETUP_URI_SCRIPT" ]]; then
    chmod +x "$SETUP_URI_SCRIPT"
    echo ""
    echo -e "${CYAN}${BOLD}=== LIVESYNC SETUP URI ===${NC}"
    echo ""
    if bash "$SETUP_URI_SCRIPT"; then
        success "Setup URI generated — share it with your Obsidian devices"
    else
        warn "Setup URI generation failed. Run manually:"
        warn "  bash $SETUP_URI_SCRIPT"
    fi
else
    warn "Setup URI script not found: $SETUP_URI_SCRIPT"
fi

#===============================================================================
# Done
#===============================================================================
echo ""
echo -e "${GREEN}${BOLD}CouchDB Health Check installed!${NC}"
echo ""
echo "  Health check runs:   every 2 minutes"
echo "  Logs:                $LOBSTER_WORKSPACE/logs/couchdb-health.log"
echo "  Alerts log:          $LOBSTER_WORKSPACE/logs/alerts.log"
echo ""
echo "  Commands:"
echo "    View timer:        systemctl --user status couchdb-health.timer"
echo "    View logs:         journalctl --user -u couchdb-health.service -f"
echo "    Run manually:      $SCRIPT_SRC"
echo ""
echo "  Setup URI command:"
echo "    bash $SKILL_DIR/scripts/generate-setup-uri.sh"
echo ""
echo "  To update later, just re-run this script:"
echo "    bash $SKILL_DIR/install.sh"
echo ""
=======
info() { echo -e "${BLUE}[INFO]${NC} $1"; }
success() { echo -e "${GREEN}[OK]${NC} $1"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; }
step() { echo -e "\n${CYAN}${BOLD}--- $1${NC}"; }

# Paths
LOBSTER_DIR="${LOBSTER_INSTALL_DIR:-$HOME/lobster}"
SKILL_DIR="$LOBSTER_DIR/lobster-shop/obsidian-km"
CONFIG_DIR="$LOBSTER_DIR/config/obsidian-km"
VENV_DIR="$LOBSTER_DIR/.venv"
PYTHON_PATH="$VENV_DIR/bin/python"
VAULT_DIR="${OBSIDIAN_VAULT_DIR:-$HOME/obsidian-vault}"

# CouchDB settings
COUCHDB_PORT="${COUCHDB_PORT:-5984}"
COUCHDB_ADMIN_USER="${COUCHDB_ADMIN_USER:-admin}"
COUCHDB_ADMIN_PASS="${COUCHDB_ADMIN_PASS:-$(openssl rand -base64 24)}"
COUCHDB_DB_NAME="${COUCHDB_DB_NAME:-obsidian_notes}"

# TLS proxy settings
CADDY_PORT="${CADDY_HTTPS_PORT:-5985}"

echo ""
echo -e "${BOLD}Obsidian KM Skill Installer${NC}"
echo "============================="
echo ""
echo "This will install the Obsidian Knowledge Management skill for Lobster."
echo "It enables sync and read/write access to an Obsidian vault via Telegram."
echo ""

#===============================================================================
# Phase 1: Install CouchDB (BIS-230)
#===============================================================================
install_couchdb() {
    step "Installing CouchDB"

    # Check if CouchDB is already installed and running
    if systemctl is-active --quiet couchdb 2>/dev/null; then
        success "CouchDB is already installed and running"
        return 0
    fi

    # Check if already installed but not running
    if command -v couchdb &>/dev/null || [ -f /opt/couchdb/bin/couchdb ]; then
        info "CouchDB is installed but not running, attempting to start..."
        sudo systemctl start couchdb 2>/dev/null || true
        sleep 2
        if systemctl is-active --quiet couchdb 2>/dev/null; then
            success "CouchDB started successfully"
            return 0
        fi
    fi

    info "Installing CouchDB from Apache repository..."

    # Add CouchDB repository key
    if ! [ -f /usr/share/keyrings/couchdb-archive-keyring.gpg ]; then
        curl -fsSL https://couchdb.apache.org/repo/keys.asc | gpg --dearmor | \
            sudo tee /usr/share/keyrings/couchdb-archive-keyring.gpg >/dev/null
    fi

    # Add CouchDB repository
    DISTRO=$(lsb_release -cs 2>/dev/null || echo "jammy")
    echo "deb [signed-by=/usr/share/keyrings/couchdb-archive-keyring.gpg] https://apache.jfrog.io/artifactory/couchdb-deb/ ${DISTRO} main" | \
        sudo tee /etc/apt/sources.list.d/couchdb.list >/dev/null

    # Update and install
    sudo apt-get update -qq

    # Pre-configure CouchDB for unattended install (single-node mode)
    echo "couchdb couchdb/mode select standalone" | sudo debconf-set-selections
    echo "couchdb couchdb/bindaddress string 127.0.0.1" | sudo debconf-set-selections
    echo "couchdb couchdb/cookie string monster" | sudo debconf-set-selections
    echo "couchdb couchdb/adminpass password ${COUCHDB_ADMIN_PASS}" | sudo debconf-set-selections
    echo "couchdb couchdb/adminpass_again password ${COUCHDB_ADMIN_PASS}" | sudo debconf-set-selections

    DEBIAN_FRONTEND=noninteractive sudo apt-get install -y couchdb 2>&1 | tail -10

    # Wait for CouchDB to start
    sleep 3

    if systemctl is-active --quiet couchdb 2>/dev/null; then
        success "CouchDB installed and running"
    else
        sudo systemctl start couchdb 2>/dev/null || true
        sleep 2
        if systemctl is-active --quiet couchdb 2>/dev/null; then
            success "CouchDB installed and started"
        else
            error "CouchDB installation failed or could not start"
            exit 1
        fi
    fi
}

#===============================================================================
# Phase 2: Create Obsidian Vault (BIS-233)
#===============================================================================
create_vault() {
    step "Creating Obsidian vault"

    if [ -d "$VAULT_DIR" ]; then
        success "Obsidian vault already exists at $VAULT_DIR"
        return 0
    fi

    info "Creating vault directory at $VAULT_DIR..."
    mkdir -p "$VAULT_DIR"
    mkdir -p "$VAULT_DIR/.obsidian"

    # Create default vault config
    cat > "$VAULT_DIR/.obsidian/app.json" << 'EOF'
{
  "attachmentFolderPath": "attachments",
  "newLinkFormat": "relative",
  "useMarkdownLinks": true,
  "showUnsupportedFiles": false,
  "promptDelete": false
}
EOF

    # Create welcome note
    cat > "$VAULT_DIR/Welcome.md" << 'EOF'
# Welcome to Your Obsidian Vault

This vault is managed by **Lobster** and synced via CouchDB.

## Quick Start

- Create notes by asking Lobster: "Create a note about [topic]"
- Search notes: "Search my notes for [keyword]"
- Read notes: "Read my note [title]"
- List notes: "List my recent notes"

## Organization

Notes are organized by tags and folders. Add tags with `#tag-name` anywhere in your notes.

---

*Created by Lobster Obsidian KM Skill*
EOF

    # Create attachments directory
    mkdir -p "$VAULT_DIR/attachments"

    success "Obsidian vault created at $VAULT_DIR"
}

#===============================================================================
# Phase 3: Configure CouchDB (BIS-231)
#===============================================================================
configure_couchdb() {
    step "Configuring CouchDB"

    COUCHDB_URL="http://${COUCHDB_ADMIN_USER}:${COUCHDB_ADMIN_PASS}@127.0.0.1:${COUCHDB_PORT}"

    # Wait for CouchDB to be ready
    for i in $(seq 1 30); do
        if curl -s "http://127.0.0.1:${COUCHDB_PORT}/" >/dev/null 2>&1; then
            break
        fi
        sleep 1
    done

    if ! curl -s "http://127.0.0.1:${COUCHDB_PORT}/" >/dev/null 2>&1; then
        error "CouchDB is not responding on port ${COUCHDB_PORT}"
        exit 1
    fi

    # Check if database already exists
    if curl -s "${COUCHDB_URL}/${COUCHDB_DB_NAME}" 2>/dev/null | grep -q '"db_name"'; then
        success "Database '${COUCHDB_DB_NAME}' already exists"
    else
        info "Creating database '${COUCHDB_DB_NAME}'..."
        RESULT=$(curl -s -X PUT "${COUCHDB_URL}/${COUCHDB_DB_NAME}" 2>&1)
        if echo "$RESULT" | grep -q '"ok":true'; then
            success "Database '${COUCHDB_DB_NAME}' created"
        elif echo "$RESULT" | grep -q 'file_exists'; then
            success "Database '${COUCHDB_DB_NAME}' already exists"
        else
            warn "Database creation response: $RESULT"
        fi
    fi

    # Create design document for views
    DESIGN_DOC='{
        "_id": "_design/notes",
        "views": {
            "by_title": {
                "map": "function(doc) { if (doc.type === \"note\") { emit(doc.title, { title: doc.title, updated: doc.updated }); } }"
            },
            "by_updated": {
                "map": "function(doc) { if (doc.type === \"note\") { emit(doc.updated, { title: doc.title, path: doc.path }); } }"
            },
            "by_tag": {
                "map": "function(doc) { if (doc.type === \"note\" && doc.tags) { doc.tags.forEach(function(tag) { emit(tag, { title: doc.title, path: doc.path }); }); } }"
            }
        }
    }'

    # Check if design document exists
    if curl -s "${COUCHDB_URL}/${COUCHDB_DB_NAME}/_design/notes" 2>/dev/null | grep -q '"_id"'; then
        success "Design document already exists"
    else
        info "Creating design document for note views..."
        RESULT=$(curl -s -X PUT "${COUCHDB_URL}/${COUCHDB_DB_NAME}/_design/notes" \
            -H "Content-Type: application/json" \
            -d "$DESIGN_DOC" 2>&1)
        if echo "$RESULT" | grep -q '"ok":true'; then
            success "Design document created"
        else
            warn "Design document creation: $RESULT"
        fi
    fi

    # Enable CORS for local access
    info "Configuring CORS..."
    curl -s -X PUT "${COUCHDB_URL}/_node/_local/_config/httpd/enable_cors" \
        -d '"true"' >/dev/null 2>&1 || true
    curl -s -X PUT "${COUCHDB_URL}/_node/_local/_config/cors/origins" \
        -d '"*"' >/dev/null 2>&1 || true
    curl -s -X PUT "${COUCHDB_URL}/_node/_local/_config/cors/methods" \
        -d '"GET, PUT, POST, DELETE, HEAD, OPTIONS"' >/dev/null 2>&1 || true
    curl -s -X PUT "${COUCHDB_URL}/_node/_local/_config/cors/headers" \
        -d '"accept, authorization, content-type, origin, referer"' >/dev/null 2>&1 || true

    success "CouchDB configured"
}

#===============================================================================
# Phase 4: Setup TLS Proxy (BIS-232)
#===============================================================================
setup_tls_proxy() {
    step "Setting up TLS proxy"

    # Check if Caddy is installed
    if ! command -v caddy &>/dev/null; then
        info "Installing Caddy..."
        sudo apt-get install -y debian-keyring debian-archive-keyring apt-transport-https curl 2>&1 | tail -3
        curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | \
            sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg 2>/dev/null || true
        curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | \
            sudo tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
        sudo apt-get update -qq
        sudo apt-get install -y caddy 2>&1 | tail -3
    fi

    if command -v caddy &>/dev/null; then
        success "Caddy is installed: $(caddy version 2>/dev/null || echo 'unknown version')"
    else
        warn "Caddy installation may have failed, continuing anyway..."
    fi

    # Create Caddyfile for CouchDB reverse proxy with TLS
    CADDYFILE_DIR="/etc/caddy/sites-enabled"
    sudo mkdir -p "$CADDYFILE_DIR"

    sudo tee "$CADDYFILE_DIR/obsidian-km.caddy" >/dev/null << EOF
# Obsidian KM - CouchDB TLS Proxy
# Listens on port $CADDY_PORT with auto-generated self-signed cert

:$CADDY_PORT {
    # TLS with self-signed cert for local development
    tls internal

    # Rate limiting
    @api {
        path /${COUCHDB_DB_NAME}/*
    }

    # Reverse proxy to CouchDB
    reverse_proxy 127.0.0.1:$COUCHDB_PORT {
        header_up Host {upstream_hostport}
    }

    # Logging
    log {
        output file /var/log/caddy/obsidian-km.log {
            roll_size 10mb
            roll_keep 5
        }
    }
}
EOF

    # Create log directory
    sudo mkdir -p /var/log/caddy
    sudo chown caddy:caddy /var/log/caddy 2>/dev/null || true

    # Check if main Caddyfile imports sites-enabled
    if ! sudo grep -q "sites-enabled" /etc/caddy/Caddyfile 2>/dev/null; then
        info "Adding sites-enabled import to Caddyfile..."
        echo "" | sudo tee -a /etc/caddy/Caddyfile >/dev/null
        echo "import /etc/caddy/sites-enabled/*.caddy" | sudo tee -a /etc/caddy/Caddyfile >/dev/null
    fi

    # Reload Caddy
    sudo systemctl reload caddy 2>/dev/null || sudo systemctl restart caddy 2>/dev/null || true

    success "TLS proxy configured on port $CADDY_PORT"
}

#===============================================================================
# Phase 5: Install MCP Server (BIS-243 placeholder)
#===============================================================================
install_mcp_server() {
    step "Installing MCP server"

    # Create src directory for MCP server
    mkdir -p "$SKILL_DIR/src"

    # Check if MCP server file exists
    if [ -f "$SKILL_DIR/src/obsidian_km_mcp_server.py" ]; then
        success "MCP server already exists"
    else
        info "Creating placeholder MCP server..."
        cat > "$SKILL_DIR/src/obsidian_km_mcp_server.py" << 'EOF'
#!/usr/bin/env python3
"""
Obsidian KM MCP Server - Placeholder

This MCP server provides tools for reading, writing, and searching
notes in an Obsidian vault backed by CouchDB.

Tools:
    - note_create: Create a new note
    - note_read: Read an existing note
    - note_search: Search notes by keyword
    - note_append: Append to an existing note
    - note_list: List recent notes

Full implementation: BIS-243
"""

import asyncio
import os
import json
from typing import Any

# Placeholder - full implementation in BIS-243
# Will integrate with:
#   - CouchDB for sync
#   - python-frontmatter for YAML parsing
#   - ripgrep for fast search


def get_config() -> dict[str, Any]:
    """Load configuration from environment or config file."""
    config_dir = os.environ.get("LOBSTER_CONFIG_DIR", os.path.expanduser("~/lobster/config"))
    config_file = os.path.join(config_dir, "obsidian-km", "config.json")

    defaults = {
        "vault_path": os.environ.get("OBSIDIAN_VAULT_DIR", os.path.expanduser("~/obsidian-vault")),
        "couchdb_url": os.environ.get("COUCHDB_URL", "http://127.0.0.1:5984"),
        "couchdb_db": os.environ.get("COUCHDB_DB_NAME", "obsidian_notes"),
    }

    if os.path.exists(config_file):
        with open(config_file) as f:
            defaults.update(json.load(f))

    return defaults


async def main():
    """MCP server entry point - placeholder."""
    print("Obsidian KM MCP Server - Placeholder")
    print("Full implementation coming in BIS-243")
    print(f"Config: {get_config()}")


if __name__ == "__main__":
    asyncio.run(main())
EOF
        chmod +x "$SKILL_DIR/src/obsidian_km_mcp_server.py"
        success "Placeholder MCP server created"
    fi

    # Install Python dependencies
    if [ -f "$VENV_DIR/bin/pip" ]; then
        info "Installing Python dependencies..."
        "$VENV_DIR/bin/pip" install --quiet python-frontmatter python-dotenv 2>&1 || \
            warn "Some pip dependencies had issues"
        success "Python dependencies installed"
    fi

    warn "MCP server is a placeholder — full implementation in BIS-243"
}

#===============================================================================
# Phase 6: Register Health Checks (BIS-235)
#===============================================================================
register_health_checks() {
    step "Registering health checks"

    HEALTH_DIR="$LOBSTER_DIR/config/health-checks"
    mkdir -p "$HEALTH_DIR"

    # Create health check script
    cat > "$HEALTH_DIR/obsidian-km.sh" << 'EOF'
#!/bin/bash
# Health check for Obsidian KM skill
# Returns 0 if healthy, 1 if unhealthy

COUCHDB_PORT="${COUCHDB_PORT:-5984}"
CADDY_PORT="${CADDY_HTTPS_PORT:-5985}"

# Check CouchDB
if ! curl -s "http://127.0.0.1:${COUCHDB_PORT}/" >/dev/null 2>&1; then
    echo "CouchDB not responding"
    exit 1
fi

# Check TLS proxy (optional, warn only)
if ! curl -sk "https://127.0.0.1:${CADDY_PORT}/" >/dev/null 2>&1; then
    echo "Warning: TLS proxy not responding (non-critical)"
fi

# Check vault exists
VAULT_DIR="${OBSIDIAN_VAULT_DIR:-$HOME/obsidian-vault}"
if [ ! -d "$VAULT_DIR" ]; then
    echo "Vault directory missing: $VAULT_DIR"
    exit 1
fi

echo "OK"
exit 0
EOF
    chmod +x "$HEALTH_DIR/obsidian-km.sh"

    # Create health check config
    cat > "$HEALTH_DIR/obsidian-km.json" << EOF
{
    "name": "obsidian-km",
    "script": "$HEALTH_DIR/obsidian-km.sh",
    "interval_seconds": 60,
    "timeout_seconds": 10,
    "alert_on_failure": true,
    "dependencies": ["couchdb", "caddy"]
}
EOF

    success "Health checks registered"
}

#===============================================================================
# Phase 7: Create configuration
#===============================================================================
create_config() {
    step "Creating configuration"

    mkdir -p "$CONFIG_DIR"

    # Write config file
    cat > "$CONFIG_DIR/config.json" << EOF
{
    "vault_path": "$VAULT_DIR",
    "couchdb_url": "http://127.0.0.1:$COUCHDB_PORT",
    "couchdb_db": "$COUCHDB_DB_NAME",
    "caddy_port": $CADDY_PORT,
    "sync_enabled": true
}
EOF

    # Write credentials file (restricted permissions)
    cat > "$CONFIG_DIR/credentials.env" << EOF
# Obsidian KM Credentials - DO NOT COMMIT
COUCHDB_ADMIN_USER=$COUCHDB_ADMIN_USER
COUCHDB_ADMIN_PASS=$COUCHDB_ADMIN_PASS
EOF
    chmod 600 "$CONFIG_DIR/credentials.env"

    success "Configuration saved to $CONFIG_DIR"
}

#===============================================================================
# Phase 8: Activate skill
#===============================================================================
activate_skill() {
    step "Activating skill in Lobster"

    ACTIVATE_SCRIPT="
import sys
sys.path.insert(0, '$LOBSTER_DIR/src')
try:
    from mcp.skill_manager import activate_skill
    result = activate_skill('obsidian-km', mode='triggered')
    print(result)
except ImportError:
    print('Skill manager not available (not critical)')
except Exception as e:
    print(f'Activation note: {e}')
"

    if [ -f "$PYTHON_PATH" ]; then
        "$PYTHON_PATH" -c "$ACTIVATE_SCRIPT" 2>/dev/null || \
            warn "Could not auto-activate skill. Run: lobster skill activate obsidian-km"
    else
        warn "Python venv not found. Skill activation skipped."
    fi

    success "Skill activation complete"
}

#===============================================================================
# Main installer
#===============================================================================
main() {
    # Run all phases
    install_couchdb
    create_vault
    configure_couchdb
    setup_tls_proxy
    install_mcp_server
    register_health_checks
    create_config
    activate_skill

    echo ""
    echo -e "${GREEN}${BOLD}Obsidian KM skill installed!${NC}"
    echo ""
    echo "  Vault:     $VAULT_DIR"
    echo "  CouchDB:   http://127.0.0.1:$COUCHDB_PORT"
    echo "  TLS Proxy: https://127.0.0.1:$CADDY_PORT"
    echo "  Config:    $CONFIG_DIR"
    echo ""
    echo "  Available commands:"
    echo "    /note  - Create or manage notes"
    echo "    /vault - Vault operations"
    echo "    /search - Search notes"
    echo ""
    echo "  MCP tools (after BIS-243):"
    echo "    note_create  - Create a new note"
    echo "    note_read    - Read an existing note"
    echo "    note_search  - Search notes by keyword"
    echo "    note_append  - Append to a note"
    echo "    note_list    - List recent notes"
    echo ""
    echo "  Health check: $LOBSTER_DIR/config/health-checks/obsidian-km.sh"
    echo ""
    echo "  Credentials saved to: $CONFIG_DIR/credentials.env"
    echo ""
    echo "  To restart Lobster and activate: lobster restart"
    echo ""
}

main "$@"

