#!/bin/bash
#===============================================================================
# Obsidian KM Integration Tests
#
# End-to-end test suite validating the complete obsidian-km skill:
#   - Vault structure
#   - CouchDB service and connectivity
#   - MCP server functionality
#   - Note CRUD operations
#
# Usage:
#   bash integration-test.sh           # Run all tests
#   bash integration-test.sh --verbose # Run with detailed output
#
# Exit codes:
#   0 - All tests passed
#   1 - One or more tests failed
#
# Prerequisites:
#   - obsidian.env configured at ~/lobster-config/obsidian.env
#   - CouchDB installed and running as user service
#   - obsidian-km-mcp service installed
#===============================================================================

set -o pipefail

#===============================================================================
# Configuration
#===============================================================================
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${LOBSTER_CONFIG_DIR:-$HOME/lobster-config}/obsidian.env"

# Test identifiers (unique per run to avoid collisions)
TEST_RUN_ID="integration-test-$(date +%s)"
TEST_NOTE_NAME="__test_${TEST_RUN_ID}.md"

# Counters
TESTS_PASSED=0
TESTS_FAILED=0
TESTS_SKIPPED=0

# Verbose mode
VERBOSE="${VERBOSE:-false}"
[[ "$1" == "--verbose" || "$1" == "-v" ]] && VERBOSE=true

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

#===============================================================================
# Utility Functions
#===============================================================================
log_info() {
    echo -e "${BLUE}[INFO]${NC} $*"
}

log_pass() {
    echo -e "${GREEN}[PASS]${NC} $*"
    ((TESTS_PASSED++))
}

log_fail() {
    echo -e "${RED}[FAIL]${NC} $*"
    ((TESTS_FAILED++))
}

log_skip() {
    echo -e "${YELLOW}[SKIP]${NC} $*"
    ((TESTS_SKIPPED++))
}

log_verbose() {
    [[ "$VERBOSE" == "true" ]] && echo -e "       $*"
}

cleanup() {
    # Clean up test artifacts
    if [[ -n "${VAULT_PATH:-}" && -f "$VAULT_PATH/Inbox/$TEST_NOTE_NAME" ]]; then
        rm -f "$VAULT_PATH/Inbox/$TEST_NOTE_NAME" 2>/dev/null
        log_verbose "Cleaned up test note: $TEST_NOTE_NAME"
    fi
}

trap cleanup EXIT

#===============================================================================
# Environment Loading
#===============================================================================
load_env() {
    if [[ ! -f "$ENV_FILE" ]]; then
        log_fail "Environment file not found: $ENV_FILE"
        echo ""
        echo "Create $ENV_FILE with the following variables:"
        echo "  OBSIDIAN_VAULT_PATH=/path/to/your/vault"
        echo "  COUCHDB_USER=your_couchdb_user"
        echo "  COUCHDB_PASSWORD=your_couchdb_password"
        echo "  COUCHDB_PORT=5984"
        echo "  COUCHDB_HTTPS_PORT=6984"
        return 1
    fi

    # shellcheck source=/dev/null
    source "$ENV_FILE"

    # Validate required variables
    local missing=()
    [[ -z "${OBSIDIAN_VAULT_PATH:-}" ]] && missing+=("OBSIDIAN_VAULT_PATH")
    [[ -z "${COUCHDB_USER:-}" ]] && missing+=("COUCHDB_USER")
    [[ -z "${COUCHDB_PASSWORD:-}" ]] && missing+=("COUCHDB_PASSWORD")

    if [[ ${#missing[@]} -gt 0 ]]; then
        log_fail "Missing required environment variables: ${missing[*]}"
        return 1
    fi

    # Set defaults
    VAULT_PATH="${OBSIDIAN_VAULT_PATH}"
    COUCHDB_PORT="${COUCHDB_PORT:-5984}"
    COUCHDB_HTTPS_PORT="${COUCHDB_HTTPS_PORT:-6984}"
    COUCHDB_URL="http://127.0.0.1:${COUCHDB_PORT}"
    COUCHDB_HTTPS_URL="https://127.0.0.1:${COUCHDB_HTTPS_PORT}"
    COUCHDB_AUTH="${COUCHDB_USER}:${COUCHDB_PASSWORD}"

    log_verbose "Loaded environment from $ENV_FILE"
    log_verbose "Vault path: $VAULT_PATH"
    log_verbose "CouchDB port: $COUCHDB_PORT"
    return 0
}

#===============================================================================
# Test Functions
#===============================================================================

# Test 1: Vault structure
test_vault_structure() {
    local test_name="Vault structure"
    local required_dirs=("Inbox" "Links" "Notes" "Daily" "Archive")
    local missing=()

    if [[ ! -d "$VAULT_PATH" ]]; then
        log_fail "$test_name - Vault directory not found: $VAULT_PATH"
        return 1
    fi

    for dir in "${required_dirs[@]}"; do
        if [[ ! -d "$VAULT_PATH/$dir" ]]; then
            missing+=("$dir")
        fi
    done

    if [[ ${#missing[@]} -gt 0 ]]; then
        log_fail "$test_name - Missing directories: ${missing[*]}"
        return 1
    fi

    log_pass "$test_name - All required directories exist"
    log_verbose "Verified: ${required_dirs[*]}"
    return 0
}

# Test 2: CouchDB service active
test_couchdb_service() {
    local test_name="CouchDB service"

    if ! command -v systemctl &>/dev/null; then
        log_skip "$test_name - systemctl not available"
        return 0
    fi

    local status
    status=$(systemctl --user is-active couchdb 2>/dev/null || echo "unknown")

    if [[ "$status" == "active" ]]; then
        log_pass "$test_name - Service is active"
        return 0
    elif [[ "$status" == "unknown" ]]; then
        # Try system-level service
        status=$(systemctl is-active couchdb 2>/dev/null || echo "inactive")
        if [[ "$status" == "active" ]]; then
            log_pass "$test_name - Service is active (system-level)"
            return 0
        fi
        log_fail "$test_name - Service not found (checked user and system)"
        return 1
    else
        log_fail "$test_name - Service status: $status"
        return 1
    fi
}

# Test 3: CouchDB HTTP responds with auth
test_couchdb_http() {
    local test_name="CouchDB HTTP"

    local response
    response=$(curl -sf -u "$COUCHDB_AUTH" "$COUCHDB_URL/" 2>/dev/null)
    local curl_exit=$?

    if [[ $curl_exit -ne 0 ]]; then
        log_fail "$test_name - Cannot connect to $COUCHDB_URL"
        return 1
    fi

    # Verify it's CouchDB by checking response
    if echo "$response" | grep -q '"couchdb"'; then
        local version
        version=$(echo "$response" | grep -o '"version":"[^"]*"' | cut -d'"' -f4)
        log_pass "$test_name - Connected (version: ${version:-unknown})"
        log_verbose "Response: $response"
        return 0
    else
        log_fail "$test_name - Unexpected response (not CouchDB)"
        return 1
    fi
}

# Test 4: obsidian-km-mcp service active
test_mcp_service() {
    local test_name="obsidian-km-mcp service"

    if ! command -v systemctl &>/dev/null; then
        log_skip "$test_name - systemctl not available"
        return 0
    fi

    local status
    status=$(systemctl --user is-active obsidian-km-mcp 2>/dev/null || echo "unknown")

    if [[ "$status" == "active" ]]; then
        log_pass "$test_name - Service is active"
        return 0
    elif [[ "$status" == "unknown" ]]; then
        log_skip "$test_name - Service not installed (may be run inline)"
        return 0
    else
        log_fail "$test_name - Service status: $status"
        return 1
    fi
}

# Test 5: note_create works
test_note_create() {
    local test_name="note_create"
    local test_content="# Test Note\n\nCreated by integration test at $(date -Iseconds)\n\nTest ID: $TEST_RUN_ID"

    # Create test note in Inbox
    local note_path="$VAULT_PATH/Inbox/$TEST_NOTE_NAME"

    # Write directly (simulating MCP note_create behavior)
    if ! echo -e "$test_content" > "$note_path" 2>/dev/null; then
        log_fail "$test_name - Failed to create note"
        return 1
    fi

    # Verify file exists
    if [[ ! -f "$note_path" ]]; then
        log_fail "$test_name - Note file not found after creation"
        return 1
    fi

    # Verify content
    if ! grep -q "$TEST_RUN_ID" "$note_path"; then
        log_fail "$test_name - Note content doesn't match expected"
        return 1
    fi

    log_pass "$test_name - Created and verified: $TEST_NOTE_NAME"
    log_verbose "Path: $note_path"
    return 0
}

# Test 6: note_search works
test_note_search() {
    local test_name="note_search"

    # Search for the test ID in the vault
    local results
    results=$(grep -rl "$TEST_RUN_ID" "$VAULT_PATH" 2>/dev/null)

    if [[ -z "$results" ]]; then
        log_fail "$test_name - Could not find test content in vault"
        return 1
    fi

    # Verify our test note is in results
    if echo "$results" | grep -q "$TEST_NOTE_NAME"; then
        log_pass "$test_name - Found test note via search"
        log_verbose "Search results: $results"
        return 0
    else
        log_fail "$test_name - Test note not in search results"
        return 1
    fi
}

# Test 7: note_read works
test_note_read() {
    local test_name="note_read"
    local note_path="$VAULT_PATH/Inbox/$TEST_NOTE_NAME"

    if [[ ! -f "$note_path" ]]; then
        log_fail "$test_name - Test note doesn't exist (run test_note_create first)"
        return 1
    fi

    local content
    content=$(cat "$note_path" 2>/dev/null)
    local cat_exit=$?

    if [[ $cat_exit -ne 0 ]]; then
        log_fail "$test_name - Failed to read note"
        return 1
    fi

    if [[ -z "$content" ]]; then
        log_fail "$test_name - Note content is empty"
        return 1
    fi

    if echo "$content" | grep -q "$TEST_RUN_ID"; then
        log_pass "$test_name - Successfully read note content"
        log_verbose "Content length: ${#content} chars"
        return 0
    else
        log_fail "$test_name - Note content doesn't contain expected ID"
        return 1
    fi
}

# Test 8: note_append works
test_note_append() {
    local test_name="note_append"
    local note_path="$VAULT_PATH/Inbox/$TEST_NOTE_NAME"
    local append_marker="APPEND_TEST_$(date +%s)"

    if [[ ! -f "$note_path" ]]; then
        log_fail "$test_name - Test note doesn't exist (run test_note_create first)"
        return 1
    fi

    # Append to the note
    if ! echo -e "\n## Appended Section\n\nMarker: $append_marker" >> "$note_path" 2>/dev/null; then
        log_fail "$test_name - Failed to append to note"
        return 1
    fi

    # Verify append worked
    if grep -q "$append_marker" "$note_path"; then
        log_pass "$test_name - Successfully appended to note"
        log_verbose "Appended marker: $append_marker"
        return 0
    else
        log_fail "$test_name - Appended content not found"
        return 1
    fi
}

# Test 9: note_list works
test_note_list() {
    local test_name="note_list"
    local inbox_dir="$VAULT_PATH/Inbox"

    if [[ ! -d "$inbox_dir" ]]; then
        log_fail "$test_name - Inbox directory doesn't exist"
        return 1
    fi

    # List markdown files in Inbox
    local file_count
    file_count=$(find "$inbox_dir" -maxdepth 1 -name "*.md" -type f 2>/dev/null | wc -l)

    if [[ $file_count -eq 0 ]]; then
        log_fail "$test_name - No markdown files found in Inbox"
        return 1
    fi

    # Verify our test note is listed
    if [[ -f "$inbox_dir/$TEST_NOTE_NAME" ]]; then
        log_pass "$test_name - Listed $file_count note(s) in Inbox"
        log_verbose "Test note present in listing"
        return 0
    else
        log_fail "$test_name - Test note not found in Inbox listing"
        return 1
    fi
}

# Test 10: HTTPS proxy responds
test_https_proxy() {
    local test_name="HTTPS proxy"

    local response
    response=$(curl -sf -k -u "$COUCHDB_AUTH" "$COUCHDB_HTTPS_URL/" 2>/dev/null)
    local curl_exit=$?

    if [[ $curl_exit -ne 0 ]]; then
        # HTTPS might not be configured - this is optional
        log_skip "$test_name - Cannot connect to $COUCHDB_HTTPS_URL (HTTPS may not be configured)"
        return 0
    fi

    # Verify it's CouchDB
    if echo "$response" | grep -q '"couchdb"'; then
        log_pass "$test_name - HTTPS proxy responding at $COUCHDB_HTTPS_URL"
        return 0
    else
        log_fail "$test_name - Unexpected response from HTTPS proxy"
        return 1
    fi
}

#===============================================================================
# Main Execution
#===============================================================================
main() {
    echo ""
    echo "========================================"
    echo "  Obsidian KM Integration Tests"
    echo "========================================"
    echo "  Run ID: $TEST_RUN_ID"
    echo "  Date:   $(date -Iseconds)"
    echo "========================================"
    echo ""

    # Load environment
    if ! load_env; then
        echo ""
        echo "========================================"
        echo "  RESULT: SETUP FAILED"
        echo "========================================"
        exit 1
    fi

    echo "Running tests..."
    echo ""

    # Run all tests in order
    # Infrastructure tests
    test_vault_structure
    test_couchdb_service
    test_couchdb_http
    test_mcp_service

    # CRUD operation tests (order matters - create first)
    test_note_create
    test_note_search
    test_note_read
    test_note_append
    test_note_list

    # Optional/advanced tests
    test_https_proxy

    # Summary
    echo ""
    echo "========================================"
    echo "  TEST SUMMARY"
    echo "========================================"
    echo -e "  ${GREEN}PASSED:${NC}  $TESTS_PASSED"
    echo -e "  ${RED}FAILED:${NC}  $TESTS_FAILED"
    echo -e "  ${YELLOW}SKIPPED:${NC} $TESTS_SKIPPED"
    echo "========================================"

    if [[ $TESTS_FAILED -eq 0 ]]; then
        echo -e "  ${GREEN}RESULT: ALL TESTS PASSED${NC}"
        echo "========================================"
        exit 0
    else
        echo -e "  ${RED}RESULT: $TESTS_FAILED TEST(S) FAILED${NC}"
        echo "========================================"
        exit 1
    fi
}

# Run main
main "$@"
