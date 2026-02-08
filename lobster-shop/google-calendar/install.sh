#!/bin/bash
#===============================================================================
# Google Calendar Skill Installer for Lobster
#
# Usage: bash ~/lobster/lobster-shop/google-calendar/install.sh
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

info() { echo -e "${BLUE}[INFO]${NC} $1"; }
success() { echo -e "${GREEN}[OK]${NC} $1"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; }
step() { echo -e "\n${CYAN}${BOLD}--- $1${NC}"; }

# Paths
LOBSTER_DIR="${LOBSTER_INSTALL_DIR:-$HOME/lobster}"
SKILL_DIR="$LOBSTER_DIR/lobster-shop/google-calendar"
CONFIG_DIR="$LOBSTER_DIR/config/google-calendar"
SRC_DIR="$SKILL_DIR/src"

echo ""
echo -e "${BOLD}Google Calendar Skill Installer${NC}"
echo "================================"
echo ""

#===============================================================================
# Step 1: Check prerequisites
#===============================================================================
step "Checking prerequisites"

# Check Python
if ! command -v python3 &>/dev/null; then
    error "Python 3 is required but not installed."
    exit 1
fi
success "Python 3 found: $(python3 --version)"

# Check pip
if ! command -v pip3 &>/dev/null && ! python3 -m pip --version &>/dev/null; then
    error "pip is required but not installed. Run: sudo apt install python3-pip"
    exit 1
fi
success "pip found"

# Check Claude CLI
if ! command -v claude &>/dev/null; then
    error "Claude CLI is required but not installed."
    exit 1
fi
success "Claude CLI found"

#===============================================================================
# Step 2: Install Python dependencies
#===============================================================================
step "Installing Python dependencies"

pip3 install --quiet \
    "google-auth>=2.0" \
    "google-auth-oauthlib>=1.0" \
    "google-api-python-client>=2.0" \
    "python-dateutil>=2.8"

success "Python dependencies installed"

#===============================================================================
# Step 3: Create config directory
#===============================================================================
step "Setting up configuration"

mkdir -p "$CONFIG_DIR"
success "Config directory created: $CONFIG_DIR"

#===============================================================================
# Step 4: Check for Google credentials
#===============================================================================
step "Google Cloud credentials"

if [ -f "$CONFIG_DIR/credentials.json" ]; then
    success "Credentials file found at $CONFIG_DIR/credentials.json"
else
    echo ""
    warn "No credentials file found."
    echo ""
    echo "  You need to set up Google Cloud credentials:"
    echo ""
    echo "  1. Go to https://console.cloud.google.com"
    echo "  2. Create a project (or select existing)"
    echo "  3. Enable the 'Google Calendar API'"
    echo "     - APIs & Services > Library > search 'Google Calendar API' > Enable"
    echo "  4. Create OAuth credentials"
    echo "     - APIs & Services > Credentials > Create Credentials > OAuth Client ID"
    echo "     - Application type: Desktop application"
    echo "     - Download the JSON file"
    echo "  5. Save it as:"
    echo -e "     ${BOLD}$CONFIG_DIR/credentials.json${NC}"
    echo ""
    read -p "Press Enter when you've placed credentials.json, or Ctrl+C to do it later... "

    if [ -f "$CONFIG_DIR/credentials.json" ]; then
        success "Credentials file found!"
    else
        warn "Credentials file not found. You'll need to add it before using the skill."
        warn "Place it at: $CONFIG_DIR/credentials.json"
    fi
fi

#===============================================================================
# Step 5: Run OAuth flow (if credentials exist and no token yet)
#===============================================================================
if [ -f "$CONFIG_DIR/credentials.json" ] && [ ! -f "$CONFIG_DIR/token.json" ]; then
    step "Authorizing with Google Calendar"
    echo ""
    echo "  A browser window will open for you to authorize access."
    echo "  If you're on a headless server, you'll get a URL to open manually."
    echo ""

    if [ -f "$SRC_DIR/auth.py" ]; then
        python3 "$SRC_DIR/auth.py" --config-dir "$CONFIG_DIR" || {
            warn "Auth flow didn't complete. You can run it later:"
            echo "  python3 $SRC_DIR/auth.py --config-dir $CONFIG_DIR"
        }
    else
        warn "Auth script not yet available (skill code is being built)."
        warn "You'll be able to authorize once the skill code is installed."
    fi
fi

#===============================================================================
# Step 6: Register MCP server with Claude
#===============================================================================
step "Registering with Claude"

if [ -f "$SRC_DIR/calendar_server.py" ]; then
    claude mcp add google-calendar -- python3 "$SRC_DIR/calendar_server.py" 2>/dev/null || {
        warn "Could not register MCP server automatically."
        echo "  You can register manually later:"
        echo "  claude mcp add google-calendar -- python3 $SRC_DIR/calendar_server.py"
    }
    success "MCP server registered"
else
    warn "MCP server code not yet available (skill code is being built)."
    echo "  Once the code is ready, register with:"
    echo "  claude mcp add google-calendar -- python3 $SRC_DIR/calendar_server.py"
fi

#===============================================================================
# Done
#===============================================================================
echo ""
echo -e "${GREEN}${BOLD}Google Calendar skill setup complete!${NC}"
echo ""
if [ ! -f "$CONFIG_DIR/credentials.json" ]; then
    echo "  Next steps:"
    echo "  1. Add Google Cloud credentials to $CONFIG_DIR/credentials.json"
    echo "  2. Re-run this installer to complete authorization"
elif [ ! -f "$CONFIG_DIR/token.json" ]; then
    echo "  Next steps:"
    echo "  1. Complete the authorization flow"
    echo "  2. Restart Lobster: lobster restart"
else
    echo "  You're all set! Restart Lobster to activate:"
    echo "  lobster restart"
fi
echo ""
