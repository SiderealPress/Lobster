#!/usr/bin/env bash
#
# Obsidian KM Skill - CouchDB Configuration for LiveSync
# BIS-231: Configure CouchDB database and CORS for Obsidian LiveSync
#
# This script is idempotent: safe to run multiple times without breaking
# an existing configuration.
#
# Prerequisites:
#   - CouchDB installed and running (BIS-230)
#   - ~/lobster-config/obsidian.env exists with credentials
#
# Usage: ./configure-couchdb.sh [--dry-run]
#

set -euo pipefail

# ============================================================================
# Configuration
# ============================================================================

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly CONFIG_DIR="${HOME}/lobster-config"
readonly CONFIG_FILE="${CONFIG_DIR}/obsidian.env"

# Colors for output
readonly RED='\033[0;31m'
readonly GREEN='\033[0;32m'
readonly YELLOW='\033[1;33m'
readonly NC='\033[0m' # No Color

# ============================================================================
# Pure Functions
# ============================================================================

# Read a value from the config file
# Args: key -> string (or empty)
get_config_value() {
    local key="$1"
    if [[ -f "$CONFIG_FILE" ]]; then
        grep "^${key}=" "$CONFIG_FILE" 2>/dev/null | cut -d'=' -f2- | tr -d '"' || true
    fi
}

# Check if CouchDB is responding
# Args: host, port -> bool (exit code)
couchdb_is_ready() {
    local host="$1"
    local port="$2"
    curl -s "http://${host}:${port}/" >/dev/null 2>&1
}

# Check if a database exists
# Args: base_url, db_name -> bool (exit code)
database_exists() {
    local base_url="$1"
    local db_name="$2"
    local status
    status=$(curl -s -o /dev/null -w "%{http_code}" "${base_url}/${db_name}")
    [[ "$status" == "200" ]]
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
# Configuration Steps (each is idempotent)
# ============================================================================

# Step 1: Create the obsidian database
create_obsidian_database() {
    local base_url="$1"
    local db_name="$2"

    log_step "Creating database: ${db_name}..."

    if database_exists "$base_url" "$db_name"; then
        log_info "Database '${db_name}' already exists, skipping."
        return 0
    fi

    log_info "Creating database '${db_name}'..."
    local response
    response=$(curl -s -X PUT "${base_url}/${db_name}")

    if echo "$response" | grep -q '"ok":true'; then
        log_info "Database '${db_name}' created successfully."
    else
        log_error "Failed to create database '${db_name}': ${response}"
        return 1
    fi
}

# Step 2: Ensure system databases exist
ensure_system_databases() {
    local base_url="$1"

    log_step "Ensuring system databases exist..."

    local system_dbs=("_users" "_replicator" "_global_changes")

    for db in "${system_dbs[@]}"; do
        if database_exists "$base_url" "$db"; then
            log_info "System database '${db}' exists."
        else
            log_info "Creating system database '${db}'..."
            local response
            response=$(curl -s -X PUT "${base_url}/${db}")
            if echo "$response" | grep -q '"ok":true'; then
                log_info "System database '${db}' created."
            else
                # Some system databases may already exist or be auto-created
                log_warn "Could not create '${db}' (may already exist): ${response}"
            fi
        fi
    done
}

# Step 3: Configure CORS for LiveSync
configure_cors() {
    log_step "Configuring CORS for Obsidian LiveSync..."

    local local_ini="/opt/couchdb/etc/local.ini"

    # Check if CORS section exists and is configured
    if sudo grep -q "^\[cors\]" "$local_ini" && \
       sudo grep -q "^origins = \*" "$local_ini"; then
        log_info "CORS already configured, skipping."
        return 0
    fi

    log_info "Backing up local.ini..."
    sudo cp "$local_ini" "${local_ini}.bak.$(date +%Y%m%d%H%M%S)"

    # Enable CORS in httpd section
    log_info "Enabling CORS in httpd section..."
    if ! sudo grep -q "^\[httpd\]" "$local_ini"; then
        echo -e "\n[httpd]" | sudo tee -a "$local_ini" > /dev/null
    fi

    # Remove existing enable_cors if present and add fresh
    sudo sed -i '/^enable_cors/d' "$local_ini"
    sudo sed -i '/^\[httpd\]/a enable_cors = true' "$local_ini"

    # Add CORS section if it doesn't exist
    log_info "Adding CORS configuration section..."
    if ! sudo grep -q "^\[cors\]" "$local_ini"; then
        cat <<'EOF' | sudo tee -a "$local_ini" > /dev/null

[cors]
origins = *
credentials = true
methods = GET, PUT, POST, HEAD, DELETE
headers = accept, authorization, content-type, origin, referer, x-csrf-token
EOF
    else
        # Update existing CORS section
        sudo sed -i 's/^origins = .*/origins = */' "$local_ini"
        sudo sed -i 's/^credentials = .*/credentials = true/' "$local_ini"
    fi

    log_info "CORS configuration applied."
}

# Step 4: Configure LiveSync-compatible settings
configure_livesync_settings() {
    log_step "Configuring LiveSync-compatible CouchDB settings..."

    local local_ini="/opt/couchdb/etc/local.ini"

    # Check if reduce_limit is already configured
    if sudo grep -q "^reduce_limit = false" "$local_ini"; then
        log_info "LiveSync settings already configured, skipping."
        return 0
    fi

    log_info "Setting reduce_limit = false for LiveSync compatibility..."

    # Add query_server_config section if it doesn't exist
    if ! sudo grep -q "^\[query_server_config\]" "$local_ini"; then
        echo -e "\n[query_server_config]" | sudo tee -a "$local_ini" > /dev/null
    fi

    # Remove existing reduce_limit if present
    sudo sed -i '/^reduce_limit/d' "$local_ini"

    # Add reduce_limit = false under query_server_config
    sudo sed -i '/^\[query_server_config\]/a reduce_limit = false' "$local_ini"

    # Configure max_document_size for large attachments (LiveSync needs this)
    log_info "Setting max_document_size for attachments..."
    if ! sudo grep -q "^\[couchdb\]" "$local_ini"; then
        echo -e "\n[couchdb]" | sudo tee -a "$local_ini" > /dev/null
    fi

    # Remove existing max_document_size if present
    sudo sed -i '/^max_document_size/d' "$local_ini"

    # Set max_document_size to 50MB (default is 8MB, LiveSync may need more)
    sudo sed -i '/^\[couchdb\]/a max_document_size = 52428800' "$local_ini"

    log_info "LiveSync-compatible settings applied."
}

# Step 5: Restart CouchDB to apply changes
restart_couchdb() {
    log_step "Restarting CouchDB to apply configuration changes..."

    if systemctl --user is-active --quiet couchdb 2>/dev/null; then
        log_info "Restarting CouchDB user service..."
        systemctl --user restart couchdb

        # Wait for CouchDB to be ready
        log_info "Waiting for CouchDB to be ready..."
        local retries=15
        local host
        local port
        host=$(get_config_value "COUCHDB_HOST")
        port=$(get_config_value "COUCHDB_PORT")
        host="${host:-127.0.0.1}"
        port="${port:-5984}"

        while [[ $retries -gt 0 ]]; do
            if couchdb_is_ready "$host" "$port"; then
                break
            fi
            sleep 1
            ((retries--))
        done

        if [[ $retries -eq 0 ]]; then
            log_error "CouchDB did not become ready in time after restart."
            return 1
        fi

        log_info "CouchDB restarted and ready."
    else
        log_warn "CouchDB user service not running. Configuration will apply on next start."
    fi
}

# Step 6: Verify configuration
verify_configuration() {
    local base_url="$1"
    local db_name="$2"

    log_step "Verifying configuration..."

    # Test 1: Database exists
    log_info "Checking database '${db_name}'..."
    if database_exists "$base_url" "$db_name"; then
        log_info "Database '${db_name}' is accessible."
    else
        log_error "Database '${db_name}' is not accessible."
        return 1
    fi

    # Test 2: CORS headers are returned
    log_info "Checking CORS headers..."
    local cors_check
    cors_check=$(curl -s -I -X OPTIONS \
        -H "Origin: http://localhost" \
        -H "Access-Control-Request-Method: PUT" \
        "${base_url}/${db_name}" 2>/dev/null || true)

    if echo "$cors_check" | grep -qi "access-control-allow"; then
        log_info "CORS headers are being returned."
    else
        log_warn "CORS headers may not be configured correctly."
        log_warn "This might be okay - some CouchDB versions handle CORS differently."
    fi

    # Test 3: Write a test document and delete it
    log_info "Testing write access..."
    local test_doc='{"_id":"_livesync_test","test":true}'
    local write_response
    write_response=$(curl -s -X PUT \
        -H "Content-Type: application/json" \
        -d "$test_doc" \
        "${base_url}/${db_name}/_livesync_test")

    if echo "$write_response" | grep -q '"ok":true'; then
        log_info "Write access verified."

        # Clean up test document
        local rev
        rev=$(echo "$write_response" | grep -o '"rev":"[^"]*"' | cut -d'"' -f4)
        curl -s -X DELETE "${base_url}/${db_name}/_livesync_test?rev=${rev}" >/dev/null
        log_info "Test document cleaned up."
    else
        log_error "Write access test failed: ${write_response}"
        return 1
    fi

    echo ""
    log_info "============================================"
    log_info "CouchDB LiveSync configuration verified!"
    log_info "============================================"
    echo ""
}

# ============================================================================
# Main Configuration Flow
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
                echo "Configures CouchDB for Obsidian LiveSync."
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
    echo "Obsidian KM - CouchDB LiveSync Configuration"
    echo "============================================"
    echo ""

    # Load configuration
    if [[ ! -f "$CONFIG_FILE" ]]; then
        log_error "Config file not found: ${CONFIG_FILE}"
        log_error "Run install.sh first to create the configuration."
        exit 1
    fi

    local couchdb_user couchdb_password couchdb_host couchdb_port db_name
    couchdb_user=$(get_config_value "COUCHDB_USER")
    couchdb_password=$(get_config_value "COUCHDB_PASSWORD")
    couchdb_host=$(get_config_value "COUCHDB_HOST")
    couchdb_port=$(get_config_value "COUCHDB_PORT")
    db_name=$(get_config_value "OBSIDIAN_DATABASE")

    # Use defaults if not set
    couchdb_host="${couchdb_host:-127.0.0.1}"
    couchdb_port="${couchdb_port:-5984}"
    db_name="${db_name:-obsidian}"

    if [[ -z "$couchdb_user" ]] || [[ -z "$couchdb_password" ]]; then
        log_error "COUCHDB_USER or COUCHDB_PASSWORD not found in ${CONFIG_FILE}"
        exit 1
    fi

    local base_url="http://${couchdb_user}:${couchdb_password}@${couchdb_host}:${couchdb_port}"

    if $dry_run; then
        log_warn "DRY RUN MODE - No changes will be made"
        echo ""
        echo "Would perform the following steps:"
        echo "  1. Create database '${db_name}' at ${couchdb_host}:${couchdb_port}"
        echo "  2. Ensure system databases exist (_users, _replicator, _global_changes)"
        echo "  3. Configure CORS (origins=*, credentials=true)"
        echo "  4. Configure LiveSync settings (reduce_limit=false, max_document_size=50MB)"
        echo "  5. Restart CouchDB to apply changes"
        echo "  6. Verify configuration"
        exit 0
    fi

    # Check if CouchDB is running
    if ! couchdb_is_ready "$couchdb_host" "$couchdb_port"; then
        log_error "CouchDB is not responding at ${couchdb_host}:${couchdb_port}"
        log_error "Make sure CouchDB is running: systemctl --user status couchdb"
        exit 1
    fi

    log_info "CouchDB is running at ${couchdb_host}:${couchdb_port}"

    # Run configuration steps
    create_obsidian_database "$base_url" "$db_name"
    ensure_system_databases "$base_url"
    configure_cors
    configure_livesync_settings
    restart_couchdb
    verify_configuration "$base_url" "$db_name"

    log_info "Configuration complete!"
    log_info ""
    log_info "Next steps:"
    log_info "  1. Set up HTTPS proxy (BIS-232) for external access"
    log_info "  2. Configure Obsidian LiveSync plugin with settings from:"
    log_info "     ${SCRIPT_DIR}/../docs/livesync-setup.md"
}

# Run main function
main "$@"
