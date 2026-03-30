#!/bin/bash
#===============================================================================
# Obsidian KM Skill Installer for Lobster
#
# Installs the Obsidian KM skill for managing notes in an Obsidian vault.
# This sets up:
#   1. Python dependencies (python-frontmatter)
#   2. The MCP server (obsidian_km_server.py)
#   3. A systemd user service
#   4. Registers the MCP server with Claude
#
# Usage: bash ~/lobster/lobster-shop/obsidian-km/install.sh
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
SKILL_DIR="$LOBSTER_DIR/lobster-shop/obsidian-km"
SRC_DIR="$SKILL_DIR/src"
SERVICES_DIR="$SKILL_DIR/services"
VENV_DIR="$LOBSTER_DIR/.venv"
PYTHON_PATH="$VENV_DIR/bin/python"
LOG_DIR="$HOME/logs"
VAULT_DIR="${OBSIDIAN_VAULT_PATH:-$HOME/obsidian-vault}"

echo ""
echo -e "${BOLD}Obsidian KM Skill Installer${NC}"
echo "============================"
echo ""
echo "This will install the Obsidian KM skill for Lobster."
echo "It allows Lobster to create, read, search, and manage notes."
echo ""

#===============================================================================
# Step 1: Check prerequisites
#===============================================================================
step "Checking prerequisites"

# Check Python venv
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

# Check ripgrep (optional but recommended)
if command -v rg &>/dev/null; then
    success "ripgrep found: $(rg --version | head -1)"
else
    warn "ripgrep not found - search will use slower Python fallback"
    info "Install with: sudo apt install ripgrep"
fi

#===============================================================================
# Step 2: Install Python dependencies
#===============================================================================
step "Installing Python dependencies"

if [ -f "$VENV_DIR/bin/pip" ]; then
    "$VENV_DIR/bin/pip" install --quiet python-frontmatter 2>&1 || warn "python-frontmatter install had issues"
    success "Python dependencies installed in Lobster venv"
else
    pip3 install --quiet python-frontmatter 2>&1 || warn "python-frontmatter install had issues"
    success "Python dependencies installed"
fi

#===============================================================================
# Step 3: Create vault and log directories
#===============================================================================
step "Setting up directories"

mkdir -p "$VAULT_DIR"
mkdir -p "$VAULT_DIR/Inbox"
mkdir -p "$VAULT_DIR/Projects"
mkdir -p "$VAULT_DIR/Daily"
success "Vault directory ready: $VAULT_DIR"

mkdir -p "$LOG_DIR"
success "Log directory ready: $LOG_DIR"

#===============================================================================
# Step 4: Create systemd user service
#===============================================================================
step "Setting up systemd service"

mkdir -p "$HOME/.config/systemd/user"

cat > "$HOME/.config/systemd/user/obsidian-km-mcp.service" << EOF
[Unit]
Description=Obsidian KM MCP Server for Lobster
Documentation=https://github.com/SiderealPress/Lobster
After=network.target

[Service]
Type=simple
WorkingDirectory=$SKILL_DIR
Environment=OBSIDIAN_VAULT_PATH=$VAULT_DIR
Environment=OBSIDIAN_KM_LOG_PATH=$LOG_DIR/obsidian-km-mcp.log
ExecStart=$PYTHON_PATH $SRC_DIR/obsidian_km_server.py
Restart=on-failure
RestartSec=5
StandardOutput=null
StandardError=journal

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload 2>/dev/null || true
systemctl --user enable obsidian-km-mcp 2>/dev/null || true
success "Systemd service created: obsidian-km-mcp"

#===============================================================================
# Step 5: Start the service
#===============================================================================
step "Starting obsidian-km-mcp service"

if systemctl --user start obsidian-km-mcp 2>/dev/null; then
    sleep 1
    if systemctl --user is-active --quiet obsidian-km-mcp; then
        success "Service started successfully"
    else
        warn "Service started but may not be active"
        info "Check status: systemctl --user status obsidian-km-mcp"
    fi
else
    warn "Could not start via systemctl (may not have user session)"
    info "The service will start automatically after login"
fi

#===============================================================================
# Step 6: Register MCP server with Claude
#===============================================================================
step "Registering MCP server with Claude"

# Remove old registration if it exists
claude mcp remove obsidian-km 2>/dev/null || true

# Register the Python MCP server
if claude mcp add obsidian-km -s user -- "$PYTHON_PATH" "$SRC_DIR/obsidian_km_server.py" 2>/dev/null; then
    success "MCP server registered: obsidian-km"
else
    warn "Could not register MCP server automatically."
    echo "  Register manually with:"
    echo "  claude mcp add obsidian-km -s user -- $PYTHON_PATH $SRC_DIR/obsidian_km_server.py"
fi

#===============================================================================
# Step 7: Activate the skill in Lobster's skill manager
#===============================================================================
step "Activating skill in Lobster"

ACTIVATE_SCRIPT="
import sys
sys.path.insert(0, '$LOBSTER_DIR/src')
try:
    from mcp.skill_manager import activate_skill
    result = activate_skill('obsidian-km', mode='always')
    print(result)
except ImportError:
    print('Skill manager not available - skill will need manual activation')
"

if "$PYTHON_PATH" -c "$ACTIVATE_SCRIPT" 2>/dev/null; then
    success "Skill activated: obsidian-km (mode: always)"
else
    warn "Could not auto-activate skill. This is OK if Lobster is not fully installed."
fi

#===============================================================================
# Done
#===============================================================================
echo ""
echo -e "${GREEN}${BOLD}Obsidian KM skill installed!${NC}"
echo ""
echo "  Vault:   $VAULT_DIR"
echo "  Logs:    $LOG_DIR/obsidian-km-mcp.log"
echo "  Service: systemctl --user status obsidian-km-mcp"
echo ""
echo "  Tools available to Lobster:"
echo "    note_create   - Create a new note with optional tags"
echo "    note_read     - Read a note by title or path"
echo "    note_search   - Full-text search (ripgrep-powered)"
echo "    note_append   - Append content to an existing note"
echo "    note_list     - List notes with filters"
echo ""
echo "  Try it: Ask Lobster to 'create a note about our meeting'"
echo ""
echo "  To restart Lobster and activate: lobster restart"
echo ""
