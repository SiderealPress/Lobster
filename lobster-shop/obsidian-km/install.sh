#!/bin/bash
#===============================================================================
# Obsidian KM Skill Installer - CouchDB Health Check Component
#
# Installs the CouchDB health check monitoring for the Obsidian KM skill.
# This sets up:
#   1. Health check script in ~/lobster/lobster-shop/obsidian-km/scripts/
#   2. systemd user service and timer for periodic health checks
#   3. Telegram alerting when CouchDB is unhealthy
#
# Prerequisites:
#   - CouchDB running as a user service (couchdb.service)
#   - obsidian.env configured with COUCHDB_USER and COUCHDB_PASSWORD
#
# Idempotent — safe to re-run for updates.
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

info()    { echo -e "${BLUE}[INFO]${NC} $1"; }
success() { echo -e "${GREEN}[OK]${NC} $1"; }
warn()    { echo -e "${YELLOW}[WARN]${NC} $1"; }
error()   { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }
step()    { echo -e "\n${CYAN}${BOLD}--- $1${NC}"; }

# ---------------------------------------------------------------------------
# Paths and defaults
# ---------------------------------------------------------------------------
LOBSTER_DIR="${LOBSTER_INSTALL_DIR:-$HOME/lobster}"
LOBSTER_CONFIG="${LOBSTER_CONFIG_DIR:-$HOME/lobster-config}"
LOBSTER_WORKSPACE="${LOBSTER_WORKSPACE:-$HOME/lobster-workspace}"

SKILL_DIR="$LOBSTER_DIR/lobster-shop/obsidian-km"
SCRIPT_SRC="$SKILL_DIR/scripts/health-check.sh"
SERVICE_SRC="$SKILL_DIR/services/couchdb-health.service"
TIMER_SRC="$SKILL_DIR/services/couchdb-health.timer"
SYSTEMD_USER_DIR="$HOME/.config/systemd/user"

OBSIDIAN_ENV="$LOBSTER_CONFIG/obsidian.env"

echo ""
echo -e "${BOLD}Obsidian KM Skill - CouchDB Health Check Installer${NC}"
echo "======================================================"
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
