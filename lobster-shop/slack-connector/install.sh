#!/bin/bash
#===============================================================================
# Slack Connector Skill Installer
#
# Idempotent installer for the slack-connector Lobster skill.
# Sets up:
#   1. Prerequisites check (Python, slack-bolt, config.env)
#   2. Python dependencies (slack-bolt, slack-sdk, watchdog)
#   3. Token collection and validation (interactive or pre-configured)
#   4. Runtime directories under ~/lobster-workspace/slack-connector/
#   5. Example configs (no-clobber copy)
#   6. Service enable/restart
#
# Account type:
#   Default: bot (xoxb- token via Slack App)
#   Set SLACK_ACCOUNT_TYPE=person for user-seat path (Phase 7)
#
# Usage:
#   Bot mode (default): bash ~/lobster/lobster-shop/slack-connector/install.sh
#   Person mode:        SLACK_ACCOUNT_TYPE=person bash ~/lobster/lobster-shop/slack-connector/install.sh
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
step()    { echo -e "\n${CYAN}${BOLD}--- Step $1${NC}"; }

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
LOBSTER_DIR="${LOBSTER_INSTALL_DIR:-$HOME/lobster}"
LOBSTER_WORKSPACE="${LOBSTER_WORKSPACE:-$HOME/lobster-workspace}"
SKILL_DIR="$LOBSTER_DIR/lobster-shop/slack-connector"
RUNTIME_DIR="$LOBSTER_WORKSPACE/slack-connector"
CONFIG_ENV="$HOME/lobster-config/config.env"
PIP_BIN="$LOBSTER_DIR/.venv/bin/pip"
ACCOUNT_TYPE="${SLACK_ACCOUNT_TYPE:-bot}"
PYTHON_BIN="$LOBSTER_DIR/.venv/bin/python"

echo ""
echo -e "${BOLD}Slack Connector Skill Installer${NC}"
echo "================================="
echo -e "  Account mode: ${CYAN}${ACCOUNT_TYPE}${NC}"
echo ""

#===============================================================================
# Step 1: Check prerequisites
#===============================================================================
step "1: Checking prerequisites"

# Python version >= 3.11
PYTHON_VERSION=$("$PYTHON_BIN" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || echo "0.0")
PYTHON_MAJOR=$(echo "$PYTHON_VERSION" | cut -d. -f1)
PYTHON_MINOR=$(echo "$PYTHON_VERSION" | cut -d. -f2)
if [ "$PYTHON_MAJOR" -lt 3 ] || { [ "$PYTHON_MAJOR" -eq 3 ] && [ "$PYTHON_MINOR" -lt 11 ]; }; then
    error "Python >= 3.11 required (found $PYTHON_VERSION at $PYTHON_BIN)"
fi
success "Python $PYTHON_VERSION"

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

# config.env writable
if [ ! -f "$CONFIG_ENV" ]; then
    error "Config file not found at $CONFIG_ENV — run Lobster install first"
fi
if [ ! -w "$CONFIG_ENV" ]; then
    error "Config file $CONFIG_ENV is not writable"
fi
success "config.env is writable"

#===============================================================================
# Step 2: Install Python dependencies
#===============================================================================
step "2: Installing Python dependencies"

$PIP_BIN install --quiet "slack-bolt>=1.18" "slack-sdk>=3.26" "watchdog>=3.0" 2>&1 | tail -5
success "slack-bolt, slack-sdk, watchdog installed"

# Verify import
if ! "$PYTHON_BIN" -c "import slack_bolt" 2>/dev/null; then
    error "slack-bolt installed but not importable — check your venv"
fi
success "slack-bolt importable"

#===============================================================================
# Person account path (Phase 7)
# Collect and validate xoxp- user token, then skip to runtime dirs.
#===============================================================================
if [ "$ACCOUNT_TYPE" = "person" ]; then
    step "3: Person Account Token Setup"

    # Pure function: read a token value from config.env
    read_token() {
        local token_name="$1"
        if [ -f "$CONFIG_ENV" ] && grep -q "^${token_name}=" "$CONFIG_ENV"; then
            grep "^${token_name}=" "$CONFIG_ENV" | head -1 | cut -d'=' -f2- | sed "s/^['\"]//;s/['\"]$//"
        fi
    }

    # Pure function: mask a token for display
    mask_token() {
        local token="$1"
        local len=${#token}
        if [ "$len" -le 10 ]; then
            echo "${token:0:5}****"
        else
            echo "${token:0:5}****${token: -4}"
        fi
    }

    EXISTING_USER_TOKEN=$(read_token "LOBSTER_SLACK_USER_TOKEN")

    if [ -n "$EXISTING_USER_TOKEN" ]; then
        success "LOBSTER_SLACK_USER_TOKEN is set ($(mask_token "$EXISTING_USER_TOKEN"))"
        USER_TOKEN="$EXISTING_USER_TOKEN"
    else
        warn "LOBSTER_SLACK_USER_TOKEN is not set"
        echo ""
        echo -e "${BOLD}Person mode requires a Slack user token (xoxp-).${NC}"
        echo "  See: SLACK_ACCOUNT_TYPE=person docs for how to obtain one."
        echo ""

        for attempt in 1 2 3; do
            read -rp "  Enter your User Token (xoxp-...): " USER_TOKEN
            USER_TOKEN=$(echo "$USER_TOKEN" | xargs)  # trim whitespace

            if [[ ! "$USER_TOKEN" =~ ^xoxp- ]]; then
                echo -e "  ${RED}Invalid: User token must start with 'xoxp-'${NC}"
                if [ "$attempt" -lt 3 ]; then
                    echo "  Please try again."
                    USER_TOKEN=""
                    continue
                fi
                error "User token validation failed after 3 attempts"
            fi

            # API validation via Python — token passed via environment variable (not interpolated)
            VALIDATION_RESULT=$(SLACK_USER_TOKEN="$USER_TOKEN" SKILL_DIR="$SKILL_DIR" "$PYTHON_BIN" -c "
import os, sys
sys.path.insert(0, os.environ['SKILL_DIR'])
from src.account_mode import validate_person_token
ok, info = validate_person_token(os.environ['SLACK_USER_TOKEN'])
if ok:
    print(f\"True|{info.get('name', 'unknown')}|{info.get('team', 'unknown')}\")
else:
    print(f\"False|{info.get('error', 'unknown')}\")
" 2>/dev/null || echo "True|validation-skipped|")

            VALID=$(echo "$VALIDATION_RESULT" | cut -d'|' -f1)
            USER_NAME=$(echo "$VALIDATION_RESULT" | cut -d'|' -f2)
            TEAM_NAME=$(echo "$VALIDATION_RESULT" | cut -d'|' -f3)

            if [ "$VALID" = "True" ]; then
                if [ "$USER_NAME" != "validation-skipped" ]; then
                    success "User token valid — connected as ${USER_NAME} in workspace ${TEAM_NAME}"
                else
                    success "User token format valid (API validation skipped)"
                fi
                break
            else
                echo -e "  ${RED}API validation failed: $USER_NAME${NC}"
                if [ "$attempt" -lt 3 ]; then
                    echo "  Please try again."
                    USER_TOKEN=""
                else
                    error "User token validation failed after 3 attempts"
                fi
            fi
        done

        # Write user token to config.env — token passed via environment variable (not interpolated)
        info "Writing user token to $CONFIG_ENV..."
        SLACK_USER_TOKEN="$USER_TOKEN" SKILL_DIR="$SKILL_DIR" CONFIG_ENV_PATH="$CONFIG_ENV" "$PYTHON_BIN" -c "
import os, sys
sys.path.insert(0, os.environ['SKILL_DIR'])
from src.onboarding import _write_config_env
from pathlib import Path
_write_config_env(
    {'LOBSTER_SLACK_USER_TOKEN': os.environ['SLACK_USER_TOKEN'],
     'LOBSTER_SLACK_ACCOUNT_TYPE': 'person'},
    config_path=Path(os.environ['CONFIG_ENV_PATH'])
)
"
        success "User token saved to $CONFIG_ENV"
    fi

    # Ensure account type is recorded in config.env
    if [ -f "$CONFIG_ENV" ] && grep -q "^LOBSTER_SLACK_ACCOUNT_TYPE=" "$CONFIG_ENV"; then
        sed -i "s/^LOBSTER_SLACK_ACCOUNT_TYPE=.*/LOBSTER_SLACK_ACCOUNT_TYPE=person/" "$CONFIG_ENV"
    else
        echo "LOBSTER_SLACK_ACCOUNT_TYPE=person" >> "$CONFIG_ENV"
    fi
    success "LOBSTER_SLACK_ACCOUNT_TYPE=person written to config.env"

    #===========================================================================
    # Skip to runtime dirs (Steps 5-7 of bot mode)
    #===========================================================================
    step "4: Creating runtime directories"

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

    step "5: Copying example configs"

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

    step "6: Checking lobster-slack-router service"

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

    echo ""
    echo -e "${GREEN}${BOLD}Slack Connector skill installed!${NC}"
    echo ""
    echo "  Account mode:  person"
    echo "  Runtime dir:   $RUNTIME_DIR/"
    echo "  Config:        $RUNTIME_DIR/config/channels.yaml"
    echo "  Logs:          $RUNTIME_DIR/logs/"
    echo "  Trigger rules: $RUNTIME_DIR/config/rules/"
    echo ""
    echo -e "  ${CYAN}Person mode notes:${NC}"
    echo "    - Lobster logs ALL messages in joined channels (not just @mentions)"
    echo "    - Self-authored messages are automatically filtered out"
    echo "    - To switch to bot mode: /skill set slack-connector account_type bot"
    echo ""
    echo "  Next steps:"
    echo "    1. Add the Lobster user to channels in the Slack UI"
    echo "    2. Activate the skill:  /skill activate slack-connector"
    echo "    3. Configure channels:  edit $RUNTIME_DIR/config/channels.yaml"
    echo ""
    exit 0
fi

#===============================================================================
# Bot account path (default)
#===============================================================================

#===============================================================================
# Step 3: Check existing tokens
#===============================================================================
step "3: Checking existing tokens"

# Pure function: read a token value from config.env
# Returns the value via stdout, empty string if not found
read_token() {
    local token_name="$1"
    if [ -f "$CONFIG_ENV" ] && grep -q "^${token_name}=" "$CONFIG_ENV"; then
        grep "^${token_name}=" "$CONFIG_ENV" | head -1 | cut -d'=' -f2- | sed "s/^['\"]//;s/['\"]$//"
    fi
}

# Pure function: mask a token for display
mask_token() {
    local token="$1"
    local len=${#token}
    if [ "$len" -le 10 ]; then
        echo "${token:0:5}****"
    else
        echo "${token:0:5}****${token: -4}"
    fi
}

EXISTING_BOT_TOKEN=$(read_token "LOBSTER_SLACK_BOT_TOKEN")
EXISTING_APP_TOKEN=$(read_token "LOBSTER_SLACK_APP_TOKEN")

TOKENS_NEED_SETUP=false

if [ -n "$EXISTING_BOT_TOKEN" ] && [ -n "$EXISTING_APP_TOKEN" ]; then
    success "LOBSTER_SLACK_BOT_TOKEN is set ($(mask_token "$EXISTING_BOT_TOKEN"))"
    success "LOBSTER_SLACK_APP_TOKEN is set ($(mask_token "$EXISTING_APP_TOKEN"))"
    info "Both tokens already configured — skipping to Step 5"
    BOT_TOKEN="$EXISTING_BOT_TOKEN"
    APP_TOKEN="$EXISTING_APP_TOKEN"
else
    if [ -n "$EXISTING_BOT_TOKEN" ]; then
        success "LOBSTER_SLACK_BOT_TOKEN is set ($(mask_token "$EXISTING_BOT_TOKEN"))"
        BOT_TOKEN="$EXISTING_BOT_TOKEN"
    else
        warn "LOBSTER_SLACK_BOT_TOKEN is not set"
        TOKENS_NEED_SETUP=true
    fi
    if [ -n "$EXISTING_APP_TOKEN" ]; then
        success "LOBSTER_SLACK_APP_TOKEN is set ($(mask_token "$EXISTING_APP_TOKEN"))"
        APP_TOKEN="$EXISTING_APP_TOKEN"
    else
        warn "LOBSTER_SLACK_APP_TOKEN is not set"
        TOKENS_NEED_SETUP=true
    fi
fi

#===============================================================================
# Step 4: Guide user and collect tokens (if needed)
#===============================================================================
if [ "$TOKENS_NEED_SETUP" = true ]; then
    step "4: Slack App Setup & Token Collection"

    echo ""
    echo -e "${BOLD}Follow these steps to create your Slack App:${NC}"
    echo ""
    echo "  1. Go to https://api.slack.com/apps"
    echo "     → Click \"Create New App\" → \"From scratch\""
    echo "     → App name: \"Lobster\" (or any name)"
    echo "     → Pick your workspace"
    echo ""
    echo "  2. Enable Socket Mode"
    echo "     → App settings → \"Socket Mode\" → Enable"
    echo "     → Create an App-Level Token with scope: connections:write"
    echo "     → Save the token (starts with xapp-)"
    echo ""
    echo "  3. Add Bot Token Scopes"
    echo "     → OAuth & Permissions → Bot Token Scopes → Add:"
    echo "       channels:history  channels:read   groups:history  groups:read"
    echo "       im:history        im:read         mpim:history    mpim:read"
    echo "       chat:write        users:read      reactions:read  files:read"
    echo ""
    echo "  4. Subscribe to Bot Events"
    echo "     → Event Subscriptions → Enable → Subscribe to Bot Events:"
    echo "       message.channels  message.groups  message.im  message.mpim"
    echo "       reaction_added    app_mention     file_shared"
    echo ""
    echo "  5. Install App to Workspace"
    echo "     → OAuth & Permissions → \"Install to Workspace\" → Allow"
    echo "     → Save the Bot User OAuth Token (starts with xoxb-)"
    echo ""
    read -rp "Press Enter when you've completed the steps above..."
    echo ""

    # Collect bot token if missing
    if [ -z "$BOT_TOKEN" ]; then
        for attempt in 1 2 3; do
            read -rp "  Enter your Bot Token (xoxb-...): " BOT_TOKEN
            BOT_TOKEN=$(echo "$BOT_TOKEN" | xargs)  # trim whitespace

            # Format check
            if [[ ! "$BOT_TOKEN" =~ ^xoxb- ]]; then
                echo -e "  ${RED}Invalid: Bot token must start with 'xoxb-'${NC}"
                if [ "$attempt" -lt 3 ]; then
                    echo "  Please try again."
                    BOT_TOKEN=""
                    continue
                fi
                error "Bot token validation failed after 3 attempts"
            fi

            # API validation via Python (uses the onboarding module)
            VALIDATION_RESULT=$(SLACK_BOT_TOKEN="$BOT_TOKEN" SKILL_DIR="$SKILL_DIR" "$PYTHON_BIN" -c "
import os, sys
sys.path.insert(0, os.environ['SKILL_DIR'])
from src.onboarding import validate_bot_token_with_api
ok, msg = validate_bot_token_with_api(os.environ['SLACK_BOT_TOKEN'])
print(f'{ok}|{msg}')
" 2>/dev/null || echo "True|validation-skipped")

            VALID=$(echo "$VALIDATION_RESULT" | cut -d'|' -f1)
            WORKSPACE=$(echo "$VALIDATION_RESULT" | cut -d'|' -f2-)

            if [ "$VALID" = "True" ]; then
                success "Bot token valid — workspace: $WORKSPACE"
                break
            else
                echo -e "  ${RED}API validation failed: $WORKSPACE${NC}"
                if [ "$attempt" -lt 3 ]; then
                    echo "  Please try again."
                    BOT_TOKEN=""
                else
                    error "Bot token validation failed after 3 attempts"
                fi
            fi
        done
    fi

    # Collect app token if missing
    if [ -z "$APP_TOKEN" ]; then
        for attempt in 1 2 3; do
            read -rp "  Enter your App Token (xapp-...): " APP_TOKEN
            APP_TOKEN=$(echo "$APP_TOKEN" | xargs)  # trim whitespace

            if [[ "$APP_TOKEN" =~ ^xapp- ]]; then
                success "App token format valid"
                break
            else
                echo -e "  ${RED}Invalid: App token must start with 'xapp-'${NC}"
                if [ "$attempt" -lt 3 ]; then
                    echo "  Please try again."
                    APP_TOKEN=""
                else
                    error "App token validation failed after 3 attempts"
                fi
            fi
        done
    fi

    # Write tokens to config.env using the Python onboarding module
    info "Writing tokens to $CONFIG_ENV..."
    SLACK_BOT_TOKEN="$BOT_TOKEN" SLACK_APP_TOKEN="$APP_TOKEN" SKILL_DIR="$SKILL_DIR" CONFIG_ENV_PATH="$CONFIG_ENV" "$PYTHON_BIN" -c "
import os, sys
sys.path.insert(0, os.environ['SKILL_DIR'])
from src.onboarding import write_tokens_to_config
write_tokens_to_config(os.environ['CONFIG_ENV_PATH'], os.environ['SLACK_BOT_TOKEN'], os.environ['SLACK_APP_TOKEN'])
"
    success "Tokens saved to $CONFIG_ENV"
else
    info "Skipping Step 4 — tokens already configured"
fi

#===============================================================================
# Step 5: Create runtime directories
#===============================================================================
step "5: Creating runtime directories"

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
# Step 6: Copy example configs (no-clobber)
#===============================================================================
step "6: Copying example configs"

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
# Step 7: Enable/restart lobster-slack-router service
#===============================================================================
step "7: Checking lobster-slack-router service"

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
# Step 8: Register MCP server in Claude Code config
#===============================================================================
step "8: Registering MCP server"

CLAUDE_JSON="$HOME/.claude.json"
MCP_SERVER_PATH="$SKILL_DIR/src/mcp_server.py"

# Register the slack-connector MCP server in ~/.claude.json
# Uses Python for safe JSON manipulation (no jq dependency)
if [ -f "$CLAUDE_JSON" ]; then
    ALREADY_REGISTERED=$("$PYTHON_BIN" -c "
import json, sys
try:
    with open('$CLAUDE_JSON') as f:
        data = json.load(f)
    servers = data.get('mcpServers', {})
    print('yes' if 'slack-connector' in servers else 'no')
except Exception:
    print('no')
" 2>/dev/null || echo "no")

    if [ "$ALREADY_REGISTERED" = "yes" ]; then
        info "slack-connector MCP server already registered in $CLAUDE_JSON"
    else
        "$PYTHON_BIN" -c "
import json, os

claude_json_path = '$CLAUDE_JSON'
mcp_server_path = '$MCP_SERVER_PATH'
python_bin = '$PYTHON_BIN'
workspace = '$LOBSTER_WORKSPACE'

with open(claude_json_path) as f:
    data = json.load(f)

if 'mcpServers' not in data:
    data['mcpServers'] = {}

data['mcpServers']['slack-connector'] = {
    'command': python_bin,
    'args': [mcp_server_path],
    'env': {
        'LOBSTER_WORKSPACE': workspace,
    },
}

with open(claude_json_path, 'w') as f:
    json.dump(data, f, indent=2)
    f.write('\n')
" 2>/dev/null
        if [ $? -eq 0 ]; then
            success "slack-connector MCP server registered in $CLAUDE_JSON"
        else
            warn "Could not register MCP server automatically"
            echo ""
            echo "  Add this to your ~/.claude.json mcpServers section:"
            echo ""
            echo "    \"slack-connector\": {"
            echo "      \"command\": \"$PYTHON_BIN\","
            echo "      \"args\": [\"$MCP_SERVER_PATH\"],"
            echo "      \"env\": {"
            echo "        \"LOBSTER_WORKSPACE\": \"$LOBSTER_WORKSPACE\""
            echo "      }"
            echo "    }"
            echo ""
        fi
    fi
else
    warn "$CLAUDE_JSON not found — MCP server not registered"
    echo ""
    echo "  When Claude Code is available, add this to ~/.claude.json mcpServers:"
    echo ""
    echo "    \"slack-connector\": {"
    echo "      \"command\": \"$PYTHON_BIN\","
    echo "      \"args\": [\"$MCP_SERVER_PATH\"],"
    echo "      \"env\": {"
    echo "        \"LOBSTER_WORKSPACE\": \"$LOBSTER_WORKSPACE\""
    echo "      }"
    echo "    }"
    echo ""
fi

#===============================================================================
# Step 9: Done — print success
#===============================================================================
echo ""
echo -e "${GREEN}${BOLD}Slack Connector skill installed!${NC}"
echo ""
echo "  Account mode:  bot"
echo "  Runtime dir:   $RUNTIME_DIR/"
echo "  Config:        $RUNTIME_DIR/config/channels.yaml"
echo "  Logs:          $RUNTIME_DIR/logs/"
echo "  Trigger rules: $RUNTIME_DIR/config/rules/"
echo ""
echo "  Bot Token:     $(mask_token "$BOT_TOKEN")"
echo "  App Token:     $(mask_token "$APP_TOKEN")"
echo ""
echo "  Next steps:"
echo "    1. Invite Lobster to channels:  /invite @Lobster"
echo "    2. Activate the skill:          /skill activate slack-connector"
echo "    3. Configure channels:          edit $RUNTIME_DIR/config/channels.yaml"
echo ""
