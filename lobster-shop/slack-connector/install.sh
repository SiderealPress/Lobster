#!/bin/bash
#===============================================================================
# Slack Connector Skill Installer
#
# Idempotent installer for the slack-connector Lobster skill.
# Sets up:
#   1. Python dependencies (slack-bolt, slack-sdk, watchdog) in the Lobster venv
#   2. Runtime directories under ~/lobster-workspace/slack-connector/
#   3. Example configs (no-clobber copy)
#   4. Token validation
#   5. Service restart (if running)
#
# Usage: bash ~/lobster/lobster-shop/slack-connector/install.sh
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
# Paths
# ---------------------------------------------------------------------------
LOBSTER_DIR="${LOBSTER_INSTALL_DIR:-$HOME/lobster}"
LOBSTER_WORKSPACE="${LOBSTER_WORKSPACE:-$HOME/lobster-workspace}"
SKILL_DIR="$LOBSTER_DIR/lobster-shop/slack-connector"
RUNTIME_DIR="$LOBSTER_WORKSPACE/slack-connector"
CONFIG_ENV="$HOME/lobster-config/config.env"
PIP_BIN="$LOBSTER_DIR/.venv/bin/pip"

echo ""
echo -e "${BOLD}Slack Connector Skill Installer${NC}"
echo "================================="
echo ""

#===============================================================================
# Step 1: Check prerequisites
#===============================================================================
step "Checking prerequisites"

# Lobster venv pip
if [ ! -f "$PIP_BIN" ]; then
    error "Lobster venv not found at $LOBSTER_DIR/.venv/bin/pip — run Lobster install first"
fi
success "Lobster venv pip found"

# Skill directory
if [ ! -f "$SKILL_DIR/skill.toml" ]; then
    error "Skill directory not found at $SKILL_DIR — is the repo up to date?"
fi
success "Skill directory found"

#===============================================================================
# Step 2: Install Python dependencies
#===============================================================================
step "Installing Python dependencies"

$PIP_BIN install --quiet "slack-bolt>=1.18" "slack-sdk>=3.26" "watchdog>=3.0" 2>&1 | tail -5
success "slack-bolt, slack-sdk, watchdog installed"

#===============================================================================
# Step 3: Create runtime directories
#===============================================================================
step "Creating runtime directories"

readonly DIRS=(
    "$RUNTIME_DIR"
    "$RUNTIME_DIR/logs"
    "$RUNTIME_DIR/config"
    "$RUNTIME_DIR/config/rules"
    "$RUNTIME_DIR/index"
    "$RUNTIME_DIR/data"
)

for dir in "${DIRS[@]}"; do
    mkdir -p "$dir"
done
success "Runtime directories created at $RUNTIME_DIR/"

#===============================================================================
# Step 4: Copy example configs (no-clobber)
#===============================================================================
step "Copying example configs"

copy_no_clobber() {
    local src="$1"
    local dest="$2"
    if [ -f "$dest" ]; then
        info "$(basename "$dest") already exists — skipping"
    else
        cp "$src" "$dest"
        success "Copied $(basename "$dest")"
    fi
}

copy_no_clobber "$SKILL_DIR/config/channels.yaml.example" "$RUNTIME_DIR/config/channels.yaml"

#===============================================================================
# Step 5: Check for required tokens
#===============================================================================
step "Checking API tokens"

check_token() {
    local token_name="$1"
    if [ -f "$CONFIG_ENV" ] && grep -q "^${token_name}=" "$CONFIG_ENV"; then
        local token_value
        token_value=$(grep "^${token_name}=" "$CONFIG_ENV" | head -1 | cut -d'=' -f2-)
        if [ -n "$token_value" ] && [ "$token_value" != '""' ] && [ "$token_value" != "''" ]; then
            success "$token_name is set"
            return 0
        fi
    fi
    warn "$token_name is not set in $CONFIG_ENV"
    echo "  To set it, add this line to $CONFIG_ENV:"
    echo "    ${token_name}=xoxb-your-token-here"
    return 1
}

TOKENS_OK=true
check_token "LOBSTER_SLACK_BOT_TOKEN" || TOKENS_OK=false
check_token "LOBSTER_SLACK_APP_TOKEN" || TOKENS_OK=false

if [ "$TOKENS_OK" = false ]; then
    warn "Some tokens are missing — the skill will install but won't connect until tokens are configured"
fi

#===============================================================================
# Step 6: Restart lobster-slack-router if running
#===============================================================================
step "Checking lobster-slack-router service"

if systemctl is-active --quiet lobster-slack-router 2>/dev/null; then
    info "Restarting lobster-slack-router to pick up changes..."
    sudo systemctl restart lobster-slack-router
    success "lobster-slack-router restarted"
elif systemctl is-enabled --quiet lobster-slack-router 2>/dev/null; then
    info "lobster-slack-router is enabled but not running — starting..."
    sudo systemctl start lobster-slack-router
    success "lobster-slack-router started"
else
    info "lobster-slack-router service not found — skipping (will be configured in a later phase)"
fi

#===============================================================================
# Done
#===============================================================================
echo ""
echo -e "${GREEN}${BOLD}Slack Connector skill installed!${NC}"
echo ""
echo "  Runtime dir:  $RUNTIME_DIR/"
echo "  Config:       $RUNTIME_DIR/config/channels.yaml"
echo "  Logs:         $RUNTIME_DIR/logs/"
echo "  Trigger rules: $RUNTIME_DIR/config/rules/"
echo ""
if [ "$TOKENS_OK" = false ]; then
    echo -e "  ${YELLOW}⚠ Set missing tokens in $CONFIG_ENV and re-run this script${NC}"
    echo ""
fi
echo "  To activate:  /skill activate slack-connector"
echo ""
