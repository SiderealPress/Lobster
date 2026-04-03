#!/bin/bash
#===============================================================================
# check-auth.sh (formerly migrate-to-credentials-json.sh)
#
# Auth health check for Lobster. Verifies that CLAUDE_CODE_OAUTH_TOKEN is set
# in config.env and that `claude auth status` reports loggedIn=true.
#
# Auth is managed via CLAUDE_CODE_OAUTH_TOKEN env var in config.env.
# There is no credentials file migration needed.
#
# Usage:
#   bash scripts/check-auth.sh
#===============================================================================

set -uo pipefail

CONFIG_ENV="${LOBSTER_CONFIG_DIR:-$HOME/lobster-config}/config.env"
export PATH="$HOME/.local/bin:/usr/local/bin:$PATH"

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
echo -e "${BOLD}Lobster Auth Check${NC}"
echo "Auth is managed via CLAUDE_CODE_OAUTH_TOKEN in config.env"
echo "========================================================================"
echo ""

# ---------------------------------------------------------------------------
# Step 1: Check CLAUDE_CODE_OAUTH_TOKEN in config.env
# ---------------------------------------------------------------------------
info "Checking CLAUDE_CODE_OAUTH_TOKEN in config.env..."

if [[ -f "$CONFIG_ENV" ]] && grep -q "^CLAUDE_CODE_OAUTH_TOKEN=" "$CONFIG_ENV" 2>/dev/null; then
    ok "CLAUDE_CODE_OAUTH_TOKEN is set in $CONFIG_ENV"
else
    err "CLAUDE_CODE_OAUTH_TOKEN not found in $CONFIG_ENV"
    echo ""
    echo "  Fix: add CLAUDE_CODE_OAUTH_TOKEN=<token> to $CONFIG_ENV"
    echo "  Then restart: systemctl restart lobster-claude"
    echo ""
fi

echo ""

# ---------------------------------------------------------------------------
# Step 2: Check claude auth status
# ---------------------------------------------------------------------------
info "Running: claude auth status --output-format json ..."

auth_json=$(env -u CLAUDECODE -u CLAUDE_CODE_ENTRYPOINT \
    claude auth status --output-format json 2>/dev/null)

logged_in=$(echo "$auth_json" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    print('true' if d.get('loggedIn') else 'false')
except:
    print('unknown')
" 2>/dev/null)

auth_method=$(echo "$auth_json" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    print(d.get('authMethod', 'unknown'))
except:
    print('unknown')
" 2>/dev/null)

if [[ "$logged_in" == "true" ]]; then
    ok "Auth OK: loggedIn=true, authMethod=$auth_method"
elif [[ "$logged_in" == "false" ]]; then
    err "Auth FAILED: loggedIn=false"
    echo ""
    echo "  Fix: update CLAUDE_CODE_OAUTH_TOKEN in $CONFIG_ENV"
    echo "  Then restart: systemctl restart lobster-claude"
    echo ""
else
    warn "Auth check inconclusive: could not parse claude auth status output"
    echo "  Raw output: $auth_json"
fi

echo ""
echo "========================================================================"
echo -e "${BOLD}Done${NC}"
echo ""
