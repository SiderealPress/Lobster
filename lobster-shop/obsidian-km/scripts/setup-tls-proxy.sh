#!/usr/bin/env bash
#
# Obsidian KM Skill - TLS Proxy Setup
# BIS-232: Set up HTTPS/TLS for CouchDB endpoint
#
# This script installs Caddy (if needed) and configures it as a TLS-terminating
# reverse proxy for CouchDB. External clients connect via HTTPS on port 6984,
# which proxies to CouchDB on localhost:5984.
#
# This script is idempotent: safe to run multiple times.
#
# Usage: ./setup-tls-proxy.sh [--dry-run]
#

set -euo pipefail

# ============================================================================
# Configuration
# ============================================================================

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly CONFIG_DIR="${SCRIPT_DIR}/../config"
readonly CADDYFILE_TEMPLATE="${CONFIG_DIR}/Caddyfile.obsidian"
readonly CADDY_CONFIG_DIR="/etc/caddy"
readonly CADDY_COUCHDB_CONF="${CADDY_CONFIG_DIR}/Caddyfile.d/couchdb-proxy.caddyfile"
readonly CADDY_LOG_DIR="/var/log/caddy"
readonly PROXY_PORT="6984"
readonly COUCHDB_PORT="5984"

# Colors for output
readonly RED='\033[0;31m'
readonly GREEN='\033[0;32m'
readonly YELLOW='\033[1;33m'
readonly NC='\033[0m'

# ============================================================================
# Pure Functions
# ============================================================================

# Check if a command exists
command_exists() {
    command -v "$1" >/dev/null 2>&1
}

# Check if Caddy is installed
caddy_is_installed() {
    command_exists caddy
}

# Check if Caddy systemd service is active
caddy_service_is_active() {
    systemctl is-active --quiet caddy 2>/dev/null
}

# Check if Caddy systemd service is enabled
caddy_service_is_enabled() {
    systemctl is-enabled --quiet caddy 2>/dev/null
}

# Check if UFW is active
ufw_is_active() {
    sudo ufw status 2>/dev/null | grep -q "Status: active"
}

# Check if a UFW rule exists for a port
ufw_rule_exists() {
    local port="$1"
    sudo ufw status | grep -q "${port}/tcp"
}

# Check if port is accessible externally
port_is_externally_accessible() {
    local port="$1"
    # Check if UFW allows the port
    if ufw_is_active; then
        sudo ufw status | grep "${port}/tcp" | grep -q "ALLOW"
    else
        # If UFW is not active, assume port is accessible
        return 0
    fi
}

# Get the server's public IP
get_public_ip() {
    # Try common sources for public IP
    if [[ -n "${LOBSTER_PUBLIC_IP:-}" ]]; then
        echo "$LOBSTER_PUBLIC_IP"
    else
        curl -s --max-time 5 ifconfig.me 2>/dev/null || \
        curl -s --max-time 5 icanhazip.com 2>/dev/null || \
        echo "unknown"
    fi
}

# ============================================================================
# Logging Functions
# ============================================================================

log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1" >&2
}

log_step() {
    echo -e "\n${GREEN}==>${NC} $1"
}

# ============================================================================
# Installation Steps (each is idempotent)
# ============================================================================

# Step 1: Install Caddy package
install_caddy() {
    log_step "Installing Caddy web server..."

    if caddy_is_installed; then
        local version
        version=$(caddy version 2>/dev/null | head -1 || echo "unknown")
        log_info "Caddy is already installed: ${version}"
        return 0
    fi

    log_info "Adding Caddy GPG key and repository..."

    # Install dependencies
    sudo apt-get update
    sudo apt-get install -y debian-keyring debian-archive-keyring apt-transport-https

    # Add Caddy GPG key
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | \
        sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg

    # Add Caddy repository
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | \
        sudo tee /etc/apt/sources.list.d/caddy-stable.list > /dev/null

    # Install Caddy
    sudo apt-get update
    sudo apt-get install -y caddy

    log_info "Caddy installed successfully."
}

# Step 2: Configure Caddy for CouchDB proxy
configure_caddy() {
    log_step "Configuring Caddy for CouchDB proxy..."

    # Create Caddy config directory for imports
    if [[ ! -d "${CADDY_CONFIG_DIR}/Caddyfile.d" ]]; then
        log_info "Creating Caddy config directory..."
        sudo mkdir -p "${CADDY_CONFIG_DIR}/Caddyfile.d"
    fi

    # Create log directory
    if [[ ! -d "$CADDY_LOG_DIR" ]]; then
        log_info "Creating Caddy log directory..."
        sudo mkdir -p "$CADDY_LOG_DIR"
        sudo chown caddy:caddy "$CADDY_LOG_DIR"
    fi

    # Check if our config already exists and matches
    if [[ -f "$CADDY_COUCHDB_CONF" ]]; then
        if diff -q "$CADDYFILE_TEMPLATE" "$CADDY_COUCHDB_CONF" >/dev/null 2>&1; then
            log_info "Caddy CouchDB proxy config already up to date."
            return 0
        else
            log_info "Updating Caddy CouchDB proxy config..."
        fi
    fi

    # Copy our Caddyfile
    log_info "Installing Caddyfile to ${CADDY_COUCHDB_CONF}..."
    sudo cp "$CADDYFILE_TEMPLATE" "$CADDY_COUCHDB_CONF"

    # Ensure main Caddyfile imports our config
    local main_caddyfile="${CADDY_CONFIG_DIR}/Caddyfile"
    local import_line="import ${CADDY_CONFIG_DIR}/Caddyfile.d/*.caddyfile"

    if [[ -f "$main_caddyfile" ]]; then
        if ! grep -qF "$import_line" "$main_caddyfile"; then
            log_info "Adding import directive to main Caddyfile..."
            echo "" | sudo tee -a "$main_caddyfile" > /dev/null
            echo "# Import additional configs (added by obsidian-km)" | sudo tee -a "$main_caddyfile" > /dev/null
            echo "$import_line" | sudo tee -a "$main_caddyfile" > /dev/null
        else
            log_info "Import directive already present in main Caddyfile."
        fi
    else
        log_info "Creating main Caddyfile with import directive..."
        echo "# Caddy main configuration" | sudo tee "$main_caddyfile" > /dev/null
        echo "# Import additional configs" | sudo tee -a "$main_caddyfile" > /dev/null
        echo "$import_line" | sudo tee -a "$main_caddyfile" > /dev/null
    fi

    # Validate Caddy config
    log_info "Validating Caddy configuration..."
    if ! sudo caddy validate --config "$main_caddyfile" 2>/dev/null; then
        log_error "Caddy configuration validation failed!"
        return 1
    fi

    log_info "Caddy configuration installed successfully."
}

# Step 3: Configure firewall
configure_firewall() {
    log_step "Configuring firewall..."

    if ! ufw_is_active; then
        log_warn "UFW is not active. Skipping firewall configuration."
        log_warn "Ensure port ${PROXY_PORT} is accessible and port ${COUCHDB_PORT} is blocked externally."
        return 0
    fi

    # Block external access to CouchDB port (5984)
    # This is a safety measure - CouchDB should only bind to localhost anyway
    if ufw_rule_exists "$COUCHDB_PORT"; then
        log_info "Checking if CouchDB port ${COUCHDB_PORT} is properly configured..."
        # Check if it's DENY or just not accessible from outside
        if sudo ufw status | grep "${COUCHDB_PORT}/tcp" | grep -q "ALLOW"; then
            log_warn "CouchDB port ${COUCHDB_PORT} is allowed - this should be blocked!"
            log_info "Removing ALLOW rule for port ${COUCHDB_PORT}..."
            sudo ufw delete allow "${COUCHDB_PORT}/tcp" 2>/dev/null || true
        fi
    fi

    # Note: We don't explicitly DENY 5984 because CouchDB only binds to localhost,
    # and adding a DENY rule would be redundant. If someone reconfigures CouchDB
    # to bind to 0.0.0.0, they should also manage the firewall appropriately.

    # Allow HTTPS proxy port (6984)
    if ufw_rule_exists "$PROXY_PORT"; then
        if sudo ufw status | grep "${PROXY_PORT}/tcp" | grep -q "ALLOW"; then
            log_info "Port ${PROXY_PORT}/tcp is already allowed."
        else
            log_info "Allowing port ${PROXY_PORT}/tcp..."
            sudo ufw allow "${PROXY_PORT}/tcp"
        fi
    else
        log_info "Allowing port ${PROXY_PORT}/tcp..."
        sudo ufw allow "${PROXY_PORT}/tcp"
    fi

    log_info "Firewall configured successfully."
}

# Step 4: Enable and start Caddy service
start_caddy_service() {
    log_step "Starting Caddy service..."

    if ! caddy_service_is_enabled; then
        log_info "Enabling Caddy service..."
        sudo systemctl enable caddy
    else
        log_info "Caddy service already enabled."
    fi

    if caddy_service_is_active; then
        log_info "Reloading Caddy configuration..."
        sudo systemctl reload caddy
    else
        log_info "Starting Caddy service..."
        sudo systemctl start caddy
    fi

    # Wait for service to be ready
    log_info "Waiting for Caddy to be ready..."
    local retries=10
    while [[ $retries -gt 0 ]]; do
        if caddy_service_is_active; then
            break
        fi
        sleep 1
        ((retries--))
    done

    if [[ $retries -eq 0 ]]; then
        log_error "Caddy did not start in time."
        sudo systemctl status caddy || true
        return 1
    fi

    log_info "Caddy service is running."
}

# Step 5: Verify TLS proxy
verify_tls_proxy() {
    log_step "Verifying TLS proxy setup..."

    local public_ip
    public_ip=$(get_public_ip)

    # Test 1: Check if HTTPS port is listening
    log_info "Checking if port ${PROXY_PORT} is listening..."
    if ! ss -tlnp | grep -q ":${PROXY_PORT}"; then
        log_error "Port ${PROXY_PORT} is not listening!"
        return 1
    fi
    log_info "Port ${PROXY_PORT} is listening."

    # Test 2: Check HTTPS connection to localhost
    log_info "Testing HTTPS connection to localhost:${PROXY_PORT}..."
    local response
    response=$(curl -sk --max-time 10 "https://127.0.0.1:${PROXY_PORT}/" 2>/dev/null || echo "FAILED")

    if echo "$response" | grep -q '"couchdb":"Welcome"'; then
        log_info "HTTPS proxy to CouchDB is working (localhost)."
    else
        log_error "HTTPS proxy test failed!"
        log_error "Response: ${response}"
        return 1
    fi

    # Test 3: Check that CouchDB port is NOT externally accessible
    log_info "Verifying CouchDB port ${COUCHDB_PORT} is not externally accessible..."
    if ufw_is_active && port_is_externally_accessible "$COUCHDB_PORT"; then
        log_warn "CouchDB port ${COUCHDB_PORT} may be externally accessible!"
        log_warn "Ensure CouchDB only binds to 127.0.0.1."
    else
        log_info "CouchDB port ${COUCHDB_PORT} is properly protected."
    fi

    # Test 4: Check Caddy service status
    log_info "Checking Caddy service status..."
    if caddy_service_is_active && caddy_service_is_enabled; then
        log_info "Caddy service is active and enabled (will survive reboot)."
    else
        log_error "Caddy service is not properly configured."
        return 1
    fi

    echo ""
    log_info "============================================"
    log_info "TLS proxy verified successfully!"
    log_info "============================================"
    echo ""
    log_info "CouchDB HTTPS URL: https://${public_ip}:${PROXY_PORT}/"
    log_info "Test command: curl -k https://${public_ip}:${PROXY_PORT}/"
    log_info "Caddy config: ${CADDY_COUCHDB_CONF}"
    log_info "Caddy logs: ${CADDY_LOG_DIR}/couchdb-proxy.log"
    log_info "Caddy service: sudo systemctl status caddy"
    echo ""
}

# ============================================================================
# Main Installation Flow
# ============================================================================

main() {
    local dry_run=false

    # Parse arguments
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --dry-run)
                dry_run=true
                shift
                ;;
            --help|-h)
                echo "Usage: $0 [--dry-run]"
                echo ""
                echo "Sets up Caddy as a TLS-terminating reverse proxy for CouchDB."
                echo ""
                echo "Options:"
                echo "  --dry-run    Show what would be done without making changes"
                echo "  --help       Show this help message"
                exit 0
                ;;
            *)
                log_error "Unknown option: $1"
                exit 1
                ;;
        esac
    done

    echo ""
    echo "============================================"
    echo "Obsidian KM Skill - TLS Proxy Setup"
    echo "============================================"
    echo ""

    if $dry_run; then
        log_warn "DRY RUN MODE - No changes will be made"
        echo ""
        echo "Would perform the following steps:"
        echo "  1. Install Caddy web server (if not installed)"
        echo "  2. Configure Caddy for CouchDB proxy"
        echo "     - Source: ${CADDYFILE_TEMPLATE}"
        echo "     - Target: ${CADDY_COUCHDB_CONF}"
        echo "  3. Configure firewall"
        echo "     - Allow port ${PROXY_PORT}/tcp (HTTPS proxy)"
        echo "     - Verify port ${COUCHDB_PORT} is blocked externally"
        echo "  4. Enable and start Caddy service"
        echo "  5. Verify TLS proxy is working"
        exit 0
    fi

    # Verify prerequisites
    if [[ ! -f "$CADDYFILE_TEMPLATE" ]]; then
        log_error "Caddyfile template not found: ${CADDYFILE_TEMPLATE}"
        exit 1
    fi

    # Run installation steps
    install_caddy
    configure_caddy
    configure_firewall
    start_caddy_service
    verify_tls_proxy

    log_info "TLS proxy setup complete!"
}

# Run main function
main "$@"
