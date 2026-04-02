#!/bin/bash
#===============================================================================
# migrate-to-credentials-json.sh
#
# Option B Auth Migration: transition from CLAUDE_CODE_OAUTH_TOKEN env var to
# ~/.claude/.credentials.json as the single canonical credential store.
#
# Why Option B?
#   When CLAUDE_CODE_OAUTH_TOKEN is set, Claude Code's refreshToken is always
#   null, disabling auto-refresh. Only .credentials.json carries a refresh token
#   and supports the full OAuth lifecycle. This migration removes the deprecated
#   env var and ensures credentials.json is present and healthy.
#
# Usage:
#   bash scripts/migrate-to-credentials-json.sh
#
# What it does:
#   1. Checks ~/.claude/.credentials.json for presence and refresh token
#   2. Removes CLAUDE_CODE_OAUTH_TOKEN from config.env and global.env if present
#   3. Prints a summary with next steps
#===============================================================================

set -uo pipefail

CREDS_FILE="$HOME/.claude/.credentials.json"
CONFIG_ENV="${LOBSTER_CONFIG_DIR:-$HOME/lobster-config}/config.env"
GLOBAL_ENV="${LOBSTER_CONFIG_DIR:-$HOME/lobster-config}/global.env"

RED='\033[0;31m'
YELLOW='\033[1;33m'
GREEN='\033[0;32m'
BOLD='\033[1m'
NC='\033[0m'

ok()   { echo -e "${GREEN}[OK]${NC}    $1"; }
warn() { echo -e "${YELLOW}[WARN]${NC}  $1"; }
err()  { echo -e "${RED}[ERROR]${NC} $1"; }
info() { echo -e "${BOLD}[INFO]${NC}  $1"; }

echo ""
echo -e "${BOLD}Lobster Option B Auth Migration${NC}"
echo "Migrating from CLAUDE_CODE_OAUTH_TOKEN to ~/.claude/.credentials.json"
echo "========================================================================"
echo ""

MIGRATION_NEEDED=false
NEEDS_AUTH=false

# ---------------------------------------------------------------------------
# Step 1: Check credentials file
# ---------------------------------------------------------------------------
info "Checking $CREDS_FILE ..."

if [[ ! -f "$CREDS_FILE" ]]; then
    err "credentials.json not found."
    echo ""
    echo "  Fix: run 'claude auth login' to authenticate and create the file."
    NEEDS_AUTH=true
else
    # Check for refresh token
    read -r has_access has_refresh expires_in < <(python3 -c "
import json, time
try:
    d = json.load(open('$CREDS_FILE'))
    oauth = d.get('claudeAiOauth', {})
    has_access = '1' if oauth.get('accessToken') else '0'
    has_refresh = '1' if oauth.get('refreshToken') else '0'
    ea = oauth.get('expiresAt', 0) / 1000
    remaining = int(ea - time.time())
    print(has_access, has_refresh, remaining)
except Exception as e:
    print('0', '0', '-1')
" 2>/dev/null)

    if [[ "${has_access:-0}" == "0" ]]; then
        err "credentials.json exists but contains no access token."
        echo "  Fix: run 'claude auth login' to re-authenticate."
        NEEDS_AUTH=true
    elif [[ "${has_refresh:-0}" == "0" ]]; then
        warn "credentials.json has an access token but NO refresh token."
        echo "  This means auto-refresh is disabled — token will expire without recovery."
        echo "  Fix: run 'claude auth login' to get a full credential set with refresh token."
        NEEDS_AUTH=true
    else
        local_hours=$(( ${expires_in:-0} / 3600 ))
        if [[ "${expires_in:-0}" -lt 0 ]]; then
            warn "credentials.json: access token EXPIRED (refresh_token present — Claude will auto-refresh on next API call)"
        else
            ok "credentials.json: access token valid (~${local_hours}h remaining), refresh_token present"
        fi
    fi
fi

echo ""

# ---------------------------------------------------------------------------
# Step 2: Remove CLAUDE_CODE_OAUTH_TOKEN from config files
# ---------------------------------------------------------------------------
info "Scanning for CLAUDE_CODE_OAUTH_TOKEN in config files..."

remove_token_from_file() {
    local file="$1"
    if [[ ! -f "$file" ]]; then
        return 0
    fi

    if grep -q "CLAUDE_CODE_OAUTH_TOKEN" "$file" 2>/dev/null; then
        warn "Found CLAUDE_CODE_OAUTH_TOKEN in $file — removing..."
        # Remove lines containing CLAUDE_CODE_OAUTH_TOKEN (including comment lines
        # directly above that were written by install.sh)
        local tmp
        tmp=$(mktemp)
        grep -v "CLAUDE_CODE_OAUTH_TOKEN" "$file" \
            | grep -v "# OAuth token from claude setup-token" \
            > "$tmp" && mv "$tmp" "$file" || rm -f "$tmp"
        ok "Removed CLAUDE_CODE_OAUTH_TOKEN from $file"
        MIGRATION_NEEDED=true
    else
        ok "No CLAUDE_CODE_OAUTH_TOKEN found in $file"
    fi
}

remove_token_from_file "$CONFIG_ENV"
remove_token_from_file "$GLOBAL_ENV"

# Also check if it's set in the current environment (can't unset parent env,
# but we can warn so the operator knows to restart the service).
if [[ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]]; then
    warn "CLAUDE_CODE_OAUTH_TOKEN is set in the current shell environment."
    echo "  This will override .credentials.json until the process is restarted."
    echo "  Fix: restart lobster-claude to pick up the updated config."
    MIGRATION_NEEDED=true
fi

echo ""

# ---------------------------------------------------------------------------
# Step 3: Summary
# ---------------------------------------------------------------------------
echo "========================================================================"
echo -e "${BOLD}Summary${NC}"
echo "========================================================================"
echo ""

if [[ "$NEEDS_AUTH" == "true" ]]; then
    err "Authentication action required."
    echo ""
    echo "  Run: claude auth login"
    echo ""
    echo "  This opens a browser OAuth flow that saves a full credential set"
    echo "  (including refresh_token) to ~/.claude/.credentials.json."
    echo ""
fi

if [[ "$MIGRATION_NEEDED" == "true" ]]; then
    warn "Config files were updated. Restart the Lobster service to apply:"
    echo ""
    echo "  systemctl restart lobster-claude"
    echo ""
fi

if [[ "$NEEDS_AUTH" == "false" && "$MIGRATION_NEEDED" == "false" ]]; then
    ok "Nothing to do — already using Option B (credentials.json with refresh token)."
    echo ""
fi

echo "Option B auth migration complete."
echo ""
