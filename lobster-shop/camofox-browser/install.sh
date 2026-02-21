#!/bin/bash
#===============================================================================
# Camofox Browser Skill Installer for Lobster
#
# Installs the camofox-browser anti-detection browser as a Lobster skill.
# This sets up:
#   1. The camofox-browser Node.js server (from GitHub)
#   2. The Python MCP wrapper that exposes tools to Claude
#   3. A systemd user service to keep the server running
#
# Usage: bash ~/lobster/lobster-shop/camofox-browser/install.sh
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
SKILL_DIR="$LOBSTER_DIR/lobster-shop/camofox-browser"
SERVER_DIR="$SKILL_DIR/server"
SRC_DIR="$SKILL_DIR/src"
CONFIG_DIR="$LOBSTER_DIR/config/camofox-browser"
VENV_DIR="$LOBSTER_DIR/.venv"
PYTHON_PATH="$VENV_DIR/bin/python"

CAMOFOX_PORT="${CAMOFOX_PORT:-9377}"
CAMOFOX_REPO="https://github.com/jo-inc/camofox-browser.git"

echo ""
echo -e "${BOLD}Camofox Browser Skill Installer${NC}"
echo "================================="
echo ""
echo "This will install the Camoufox anti-detection browser for Lobster."
echo "It allows Lobster to browse the web without getting blocked."
echo ""

#===============================================================================
# Step 1: Check prerequisites
#===============================================================================
step "Checking prerequisites"

# Check Node.js
if ! command -v node &>/dev/null; then
    error "Node.js is required but not installed."
    echo "  Install with: curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash - && sudo apt install -y nodejs"
    exit 1
fi

NODE_VERSION=$(node --version | sed 's/v//' | cut -d. -f1)
if [ "$NODE_VERSION" -lt 18 ]; then
    error "Node.js >= 18 required, found $(node --version)"
    exit 1
fi
success "Node.js found: $(node --version)"

# Check npm
if ! command -v npm &>/dev/null; then
    error "npm is required but not installed."
    exit 1
fi
success "npm found: $(npm --version)"

# Check Python
if [ -f "$PYTHON_PATH" ]; then
    success "Lobster Python venv found: $PYTHON_PATH"
elif command -v python3 &>/dev/null; then
    PYTHON_PATH="python3"
    success "Python 3 found: $(python3 --version)"
else
    error "Python 3 is required but not installed."
    exit 1
fi

# Check Claude CLI
if ! command -v claude &>/dev/null; then
    error "Claude CLI is required but not installed."
    exit 1
fi
success "Claude CLI found"

#===============================================================================
# Step 2: Clone/update camofox-browser server
#===============================================================================
step "Setting up camofox-browser server"

if [ -d "$SERVER_DIR" ] && [ -f "$SERVER_DIR/server.js" ]; then
    info "Server directory already exists, pulling latest..."
    cd "$SERVER_DIR"
    git pull --ff-only 2>/dev/null || warn "Could not pull updates (not a git repo or conflict)"
else
    info "Cloning camofox-browser from GitHub..."
    rm -rf "$SERVER_DIR"
    git clone "$CAMOFOX_REPO" "$SERVER_DIR"
fi
success "Server source ready at $SERVER_DIR"

#===============================================================================
# Step 3: Install Node.js dependencies
#===============================================================================
step "Installing Node.js dependencies"

cd "$SERVER_DIR"
npm install --production 2>&1 | tail -5
success "Node.js dependencies installed"

info "Fetching Camoufox browser engine (this may take a minute on first run)..."
npx camoufox-js fetch 2>&1 | tail -3 || warn "Camoufox fetch had warnings (may be OK if already installed)"
success "Camoufox browser engine ready"

#===============================================================================
# Step 4: Install Python MCP wrapper dependencies
#===============================================================================
step "Installing Python MCP wrapper dependencies"

if [ -f "$VENV_DIR/bin/pip" ]; then
    "$VENV_DIR/bin/pip" install --quiet httpx 2>&1 || warn "httpx install had issues"
    success "Python dependencies installed in Lobster venv"
else
    pip3 install --quiet httpx 2>&1 || warn "httpx install had issues"
    success "Python dependencies installed"
fi

#===============================================================================
# Step 5: Create config directory
#===============================================================================
step "Setting up configuration"

mkdir -p "$CONFIG_DIR"

# Write default config
cat > "$CONFIG_DIR/config.env" << EOF
# Camofox Browser Configuration
# Managed by: lobster-shop/camofox-browser
CAMOFOX_PORT=$CAMOFOX_PORT
CAMOFOX_URL=http://localhost:$CAMOFOX_PORT
CAMOFOX_USER_ID=lobster
CAMOFOX_SESSION_KEY=default
EOF

success "Config created at $CONFIG_DIR/config.env"

#===============================================================================
# Step 6: Create systemd user service
#===============================================================================
step "Setting up systemd service"

mkdir -p "$HOME/.config/systemd/user"

cat > "$HOME/.config/systemd/user/camofox-browser.service" << EOF
[Unit]
Description=Camofox Anti-Detection Browser Server
After=network.target

[Service]
Type=simple
WorkingDirectory=$SERVER_DIR
Environment=CAMOFOX_PORT=$CAMOFOX_PORT
Environment=NODE_ENV=production
ExecStart=$(which node) $SERVER_DIR/server.js
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload 2>/dev/null || true
systemctl --user enable camofox-browser 2>/dev/null || true
success "Systemd service created: camofox-browser"

#===============================================================================
# Step 7: Start the server
#===============================================================================
step "Starting camofox-browser server"

# Check if something is already running on the port
if curl -s "http://localhost:$CAMOFOX_PORT/health" >/dev/null 2>&1; then
    success "Camofox server already running on port $CAMOFOX_PORT"
else
    systemctl --user start camofox-browser 2>/dev/null || {
        warn "systemd start failed, trying direct start..."
        cd "$SERVER_DIR"
        CAMOFOX_PORT=$CAMOFOX_PORT nohup node server.js > /tmp/camofox-browser.log 2>&1 &
        CAMOFOX_PID=$!
        echo "$CAMOFOX_PID" > "$CONFIG_DIR/server.pid"

        # Wait for server to come up
        for i in $(seq 1 30); do
            sleep 1
            if curl -s "http://localhost:$CAMOFOX_PORT/health" >/dev/null 2>&1; then
                break
            fi
        done
    }

    if curl -s "http://localhost:$CAMOFOX_PORT/health" >/dev/null 2>&1; then
        success "Camofox server started on port $CAMOFOX_PORT"
    else
        warn "Server may still be starting up (Camoufox engine takes ~10s to initialize)"
        echo "  Check status: curl http://localhost:$CAMOFOX_PORT/health"
        echo "  View logs: journalctl --user -u camofox-browser -f"
    fi
fi

#===============================================================================
# Step 8: Register MCP server with Claude
#===============================================================================
step "Registering MCP server with Claude"

# Remove old registration if it exists
claude mcp remove camofox-browser 2>/dev/null || true

# Register the Python MCP wrapper
if claude mcp add camofox-browser -s user -- "$PYTHON_PATH" "$SRC_DIR/camofox_mcp_server.py" 2>/dev/null; then
    success "MCP server registered: camofox-browser"
else
    warn "Could not register MCP server automatically."
    echo "  Register manually with:"
    echo "  claude mcp add camofox-browser -s user -- $PYTHON_PATH $SRC_DIR/camofox_mcp_server.py"
fi

#===============================================================================
# Done
#===============================================================================
echo ""
echo -e "${GREEN}${BOLD}Camofox Browser skill installed!${NC}"
echo ""
echo "  Server: http://localhost:$CAMOFOX_PORT"
echo "  Config: $CONFIG_DIR/config.env"
echo "  Logs:   journalctl --user -u camofox-browser -f"
echo ""
echo "  Tools available to Lobster:"
echo "    camofox_create_tab    - Open a new browser tab"
echo "    camofox_snapshot      - Get page snapshot with element refs"
echo "    camofox_click         - Click elements"
echo "    camofox_type          - Type text into fields"
echo "    camofox_navigate      - Navigate or search (13 search macros)"
echo "    camofox_scroll        - Scroll pages"
echo "    camofox_screenshot    - Take screenshots"
echo "    camofox_close_tab     - Close tabs"
echo "    camofox_list_tabs     - List open tabs"
echo ""
echo "  Try it: Ask Lobster to 'search Google for the latest AI news'"
echo ""
echo "  To restart Lobster and activate: lobster restart"
echo ""
