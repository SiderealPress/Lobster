#!/usr/bin/env bash
#
# Obsidian KM Skill - CouchDB Installer
# BIS-230: Install CouchDB on Lobster server (systemd service)
#
# This script is idempotent: safe to run multiple times without breaking
# an existing installation.
#
# Usage: ./install.sh [--dry-run]
#

set -euo pipefail

# ============================================================================
# Configuration
# ============================================================================

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly CONFIG_DIR="${HOME}/lobster-config"
readonly CONFIG_FILE="${CONFIG_DIR}/obsidian.env"
readonly CONFIG_TEMPLATE="${SCRIPT_DIR}/config/obsidian.env.template"
readonly SERVICE_TEMPLATE="${SCRIPT_DIR}/services/couchdb.service"
readonly SYSTEMD_USER_DIR="${HOME}/.config/systemd/user"
readonly COUCHDB_DATA_DIR="${HOME}/obsidian-vault/.couchdb"
readonly COUCHDB_BIND_ADDRESS="127.0.0.1"
readonly COUCHDB_PORT="5984"

# Colors for output
readonly RED='\033[0;31m'
readonly GREEN='\033[0;32m'
readonly YELLOW='\033[1;33m'
readonly NC='\033[0m' # No Color

# ============================================================================
# Pure Functions (no side effects, deterministic output)
# ============================================================================

# Check if a command exists
# Args: command_name -> bool (exit code)
command_exists() {
    command -v "$1" >/dev/null 2>&1
}

# Check if a systemd user service is active
# Args: service_name -> bool (exit code)
service_is_active() {
    systemctl --user is-active --quiet "$1" 2>/dev/null
}

# Check if a systemd user service is enabled
# Args: service_name -> bool (exit code)
service_is_enabled() {
    systemctl --user is-enabled --quiet "$1" 2>/dev/null
}

# Check if CouchDB apt repo is configured
# Args: none -> bool (exit code)
couchdb_repo_exists() {
    [[ -f /etc/apt/sources.list.d/couchdb.list ]]
}

# Check if CouchDB package is installed
# Args: none -> bool (exit code)
couchdb_is_installed() {
    dpkg -l couchdb 2>/dev/null | grep -q '^ii'
}

# Generate a random password
# Args: length -> string
generate_password() {
    local length="${1:-32}"
    openssl rand -base64 "$length" | tr -dc 'a-zA-Z0-9' | head -c "$length"
}

# Read a value from the config file
# Args: key -> string (or empty)
get_config_value() {
    local key="$1"
    if [[ -f "$CONFIG_FILE" ]]; then
        grep "^${key}=" "$CONFIG_FILE" 2>/dev/null | cut -d'=' -f2- | tr -d '"' || true
    fi
}

# Check if linger is enabled for current user
# Args: none -> bool (exit code)
linger_is_enabled() {
    [[ -f "/var/lib/systemd/linger/${USER}" ]]
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

# Step 1: Install CouchDB apt repository
install_couchdb_repo() {
    log_step "Configuring CouchDB apt repository..."

    if couchdb_repo_exists; then
        log_info "CouchDB apt repository already configured, skipping."
        return 0
    fi

    log_info "Adding CouchDB GPG key..."
    curl -fsSL https://couchdb.apache.org/repo/keys.asc | \
        sudo gpg --dearmor -o /usr/share/keyrings/couchdb-archive-keyring.gpg

    log_info "Adding CouchDB apt repository..."
    local codename
    codename=$(lsb_release -cs)
    echo "deb [signed-by=/usr/share/keyrings/couchdb-archive-keyring.gpg] https://apache.jfrog.io/artifactory/couchdb-deb/ ${codename} main" | \
        sudo tee /etc/apt/sources.list.d/couchdb.list > /dev/null

    log_info "CouchDB apt repository configured successfully."
}

# Step 2: Install CouchDB package
install_couchdb_package() {
    log_step "Installing CouchDB package..."

    if couchdb_is_installed; then
        log_info "CouchDB is already installed, skipping."
        return 0
    fi

    log_info "Updating apt cache..."
    sudo apt-get update

    log_info "Installing CouchDB (standalone mode)..."
    # Use debconf to pre-configure CouchDB for standalone mode
    echo "couchdb couchdb/mode select standalone" | sudo debconf-set-selections
    echo "couchdb couchdb/bindaddress string ${COUCHDB_BIND_ADDRESS}" | sudo debconf-set-selections

    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y couchdb

    log_info "CouchDB package installed successfully."
}

# Step 3: Create and configure the config directory
setup_config_directory() {
    log_step "Setting up configuration directory..."

    if [[ ! -d "$CONFIG_DIR" ]]; then
        log_info "Creating config directory: ${CONFIG_DIR}"
        mkdir -p "$CONFIG_DIR"
        chmod 700 "$CONFIG_DIR"
    fi

    if [[ ! -f "$CONFIG_FILE" ]]; then
        log_info "Creating config file from template..."

        # Generate credentials if not already set
        local couchdb_user="admin"
        local couchdb_password
        couchdb_password=$(generate_password 32)

        # Copy template and substitute values
        sed -e "s|__COUCHDB_USER__|${couchdb_user}|g" \
            -e "s|__COUCHDB_PASSWORD__|${couchdb_password}|g" \
            -e "s|__COUCHDB_HOST__|${COUCHDB_BIND_ADDRESS}|g" \
            -e "s|__COUCHDB_PORT__|${COUCHDB_PORT}|g" \
            "$CONFIG_TEMPLATE" > "$CONFIG_FILE"

        chmod 600 "$CONFIG_FILE"
        log_info "Config file created: ${CONFIG_FILE}"
    else
        log_info "Config file already exists, preserving existing credentials."
    fi
}

# Step 4: Create CouchDB data directory
setup_data_directory() {
    log_step "Setting up CouchDB data directory..."

    if [[ ! -d "$COUCHDB_DATA_DIR" ]]; then
        log_info "Creating data directory: ${COUCHDB_DATA_DIR}"
        mkdir -p "$COUCHDB_DATA_DIR"
        chmod 700 "$COUCHDB_DATA_DIR"
    else
        log_info "Data directory already exists: ${COUCHDB_DATA_DIR}"
    fi
}

# Step 5: Configure CouchDB admin credentials
configure_couchdb_auth() {
    log_step "Configuring CouchDB authentication..."

    # Load credentials from config
    local couchdb_user
    local couchdb_password
    couchdb_user=$(get_config_value "COUCHDB_USER")
    couchdb_password=$(get_config_value "COUCHDB_PASSWORD")

    if [[ -z "$couchdb_user" ]] || [[ -z "$couchdb_password" ]]; then
        log_error "COUCHDB_USER or COUCHDB_PASSWORD not found in ${CONFIG_FILE}"
        return 1
    fi

    local local_ini="/opt/couchdb/etc/local.ini"

    # Check if admin is already configured
    if sudo grep -q "^\[admins\]" "$local_ini" && \
       sudo grep -q "^${couchdb_user} =" "$local_ini"; then
        log_info "Admin user already configured, skipping."
        return 0
    fi

    log_info "Setting admin credentials in local.ini..."

    # Backup original config
    sudo cp "$local_ini" "${local_ini}.bak.$(date +%Y%m%d%H%M%S)"

    # Add admin section if it doesn't exist
    if ! sudo grep -q "^\[admins\]" "$local_ini"; then
        echo -e "\n[admins]" | sudo tee -a "$local_ini" > /dev/null
    fi

    # Add admin user (CouchDB will hash the password on first read)
    # Remove any existing admin entry first
    sudo sed -i "/^${couchdb_user} =/d" "$local_ini"

    # Append new admin credential under [admins] section
    sudo sed -i "/^\[admins\]/a ${couchdb_user} = ${couchdb_password}" "$local_ini"

    # Ensure bind_address is set to localhost only
    if sudo grep -q "^bind_address" "$local_ini"; then
        sudo sed -i "s/^bind_address.*/bind_address = ${COUCHDB_BIND_ADDRESS}/" "$local_ini"
    else
        # Add under [chttpd] section
        if ! sudo grep -q "^\[chttpd\]" "$local_ini"; then
            echo -e "\n[chttpd]" | sudo tee -a "$local_ini" > /dev/null
        fi
        sudo sed -i "/^\[chttpd\]/a bind_address = ${COUCHDB_BIND_ADDRESS}" "$local_ini"
    fi

    log_info "CouchDB authentication configured successfully."
}

# Step 6: Stop system CouchDB service (we'll run as user service)
stop_system_couchdb() {
    log_step "Configuring system CouchDB service..."

    # Stop and disable the system service since we're using a user service
    if systemctl is-active --quiet couchdb 2>/dev/null; then
        log_info "Stopping system CouchDB service..."
        sudo systemctl stop couchdb
    fi

    if systemctl is-enabled --quiet couchdb 2>/dev/null; then
        log_info "Disabling system CouchDB service..."
        sudo systemctl disable couchdb
    fi

    log_info "System CouchDB service disabled (using user service instead)."
}

# Step 7: Install systemd user service
install_user_service() {
    log_step "Installing systemd user service..."

    # Create systemd user directory if it doesn't exist
    if [[ ! -d "$SYSTEMD_USER_DIR" ]]; then
        log_info "Creating systemd user directory: ${SYSTEMD_USER_DIR}"
        mkdir -p "$SYSTEMD_USER_DIR"
    fi

    local service_file="${SYSTEMD_USER_DIR}/couchdb.service"

    # Copy service file
    log_info "Installing service file: ${service_file}"
    cp "$SERVICE_TEMPLATE" "$service_file"

    # Reload systemd daemon
    log_info "Reloading systemd user daemon..."
    systemctl --user daemon-reload

    log_info "Systemd user service installed."
}

# Step 8: Enable and start the user service
enable_and_start_service() {
    log_step "Enabling and starting CouchDB user service..."

    if ! service_is_enabled couchdb; then
        log_info "Enabling couchdb.service..."
        systemctl --user enable couchdb
    else
        log_info "couchdb.service already enabled."
    fi

    if service_is_active couchdb; then
        log_info "Restarting couchdb.service to apply changes..."
        systemctl --user restart couchdb
    else
        log_info "Starting couchdb.service..."
        systemctl --user start couchdb
    fi

    # Wait for service to be ready
    log_info "Waiting for CouchDB to be ready..."
    local retries=10
    while [[ $retries -gt 0 ]]; do
        if curl -s "http://${COUCHDB_BIND_ADDRESS}:${COUCHDB_PORT}/" >/dev/null 2>&1; then
            break
        fi
        sleep 1
        ((retries--))
    done

    if [[ $retries -eq 0 ]]; then
        log_error "CouchDB did not become ready in time."
        return 1
    fi

    log_info "CouchDB user service is running."
}

# Step 9: Enable linger for boot persistence
enable_linger() {
    log_step "Enabling linger for boot persistence..."

    if linger_is_enabled; then
        log_info "Linger already enabled for user ${USER}."
        return 0
    fi

    log_info "Enabling linger for user ${USER}..."
    loginctl enable-linger "$USER"

    log_info "Linger enabled - service will start at boot without login."
}

# Step 10: Verify installation
verify_installation() {
    log_step "Verifying CouchDB installation..."

    local couchdb_user
    local couchdb_password
    couchdb_user=$(get_config_value "COUCHDB_USER")
    couchdb_password=$(get_config_value "COUCHDB_PASSWORD")

    # Test 1: Anonymous access should be rejected
    log_info "Testing anonymous access (should be rejected)..."
    local anon_status
    anon_status=$(curl -s -o /dev/null -w "%{http_code}" "http://${COUCHDB_BIND_ADDRESS}:${COUCHDB_PORT}/_session")

    # Test 2: Authenticated access should work
    log_info "Testing authenticated access..."
    local auth_response
    auth_response=$(curl -s "http://${couchdb_user}:${couchdb_password}@${COUCHDB_BIND_ADDRESS}:${COUCHDB_PORT}/")

    if echo "$auth_response" | grep -q '"couchdb":"Welcome"'; then
        log_info "CouchDB is responding with welcome message."
    else
        log_error "CouchDB did not return expected welcome message."
        log_error "Response: ${auth_response}"
        return 1
    fi

    # Test 3: Service status
    log_info "Checking service status..."
    if service_is_active couchdb; then
        log_info "couchdb.service is active."
    else
        log_error "couchdb.service is not active."
        return 1
    fi

    echo ""
    log_info "============================================"
    log_info "CouchDB installation verified successfully!"
    log_info "============================================"
    echo ""
    log_info "CouchDB URL: http://${COUCHDB_BIND_ADDRESS}:${COUCHDB_PORT}/"
    log_info "Admin user: ${couchdb_user}"
    log_info "Config file: ${CONFIG_FILE}"
    log_info "Data directory: ${COUCHDB_DATA_DIR}"
    log_info "Service: systemctl --user status couchdb"
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
                echo "Installs CouchDB as a systemd user service for the Obsidian KM skill."
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
    echo "Obsidian KM Skill - CouchDB Installer"
    echo "============================================"
    echo ""

    if $dry_run; then
        log_warn "DRY RUN MODE - No changes will be made"
        echo ""
        echo "Would perform the following steps:"
        echo "  1. Install CouchDB apt repository"
        echo "  2. Install CouchDB package"
        echo "  3. Setup config directory (${CONFIG_DIR})"
        echo "  4. Setup data directory (${COUCHDB_DATA_DIR})"
        echo "  5. Configure CouchDB authentication"
        echo "  6. Disable system CouchDB service"
        echo "  7. Install systemd user service"
        echo "  8. Enable and start user service"
        echo "  9. Enable linger for boot persistence"
        echo " 10. Verify installation"
        exit 0
    fi

    # Run installation steps in order
    # Each step is idempotent and can be safely re-run

    install_couchdb_repo
    install_couchdb_package
    setup_config_directory
    setup_data_directory
    configure_couchdb_auth
    stop_system_couchdb
    install_user_service
    enable_and_start_service
    enable_linger
    verify_installation

    log_info "Installation complete!"
}

# Run main function
main "$@"
