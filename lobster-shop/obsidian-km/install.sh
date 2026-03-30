#!/bin/bash
#===============================================================================
# Obsidian Knowledge Management Skill Installer for Lobster
#
# Sets up the Obsidian KM skill that lets Lobster interact with an Obsidian
# vault — create notes, search content, and capture links.
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

info()    { echo -e "${BLUE}[INFO]${NC} $1"; }
success() { echo -e "${GREEN}[OK]${NC} $1"; }
warn()    { echo -e "${YELLOW}[WARN]${NC} $1"; }
error()   { echo -e "${RED}[ERROR]${NC} $1"; }
step()    { echo -e "\n${CYAN}${BOLD}--- $1${NC}"; }

# Paths
LOBSTER_DIR="${LOBSTER_INSTALL_DIR:-$HOME/lobster}"
SKILL_DIR="$LOBSTER_DIR/lobster-shop/obsidian-km"
SRC_DIR="$SKILL_DIR/src"
CONFIG_TEMPLATE="$SKILL_DIR/config/obsidian.env.template"
VENV_DIR="$LOBSTER_DIR/.venv"
PYTHON_PATH="$VENV_DIR/bin/python"
CONFIG_DIR="${LOBSTER_CONFIG_DIR:-$HOME/lobster-config}"
CONFIG_FILE="$CONFIG_DIR/obsidian.env"

echo ""
echo -e "${BOLD}Obsidian Knowledge Management Skill Installer${NC}"
echo "=============================================="
echo ""
echo "This installs the Obsidian KM skill for Lobster."
echo "It adds vault integration tools to Claude."
echo ""

#===============================================================================
# Step 1: Check prerequisites
#===============================================================================
step "Checking prerequisites"

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

# Check skill directory
if [ ! -f "$SRC_DIR/obsidian_km_server.py" ]; then
    error "Skill source not found at $SRC_DIR/obsidian_km_server.py"
    exit 1
fi
success "Skill source found"

#===============================================================================
# Step 2: Install Python dependencies
#===============================================================================
step "Installing Python dependencies (mcp)"

if [ -f "$VENV_DIR/bin/pip" ]; then
    "$VENV_DIR/bin/pip" install --quiet "mcp>=1.0" 2>&1 || warn "pip install had issues (may already be installed)"
    success "Python dependencies installed in Lobster venv"
else
    pip3 install --quiet "mcp>=1.0" 2>&1 || warn "pip3 install had issues"
    success "Python dependencies installed"
fi

#===============================================================================
# Step 3: Create configuration file
#===============================================================================
step "Setting up configuration"

mkdir -p "$CONFIG_DIR"

if [ -f "$CONFIG_FILE" ]; then
    success "Configuration file already exists: $CONFIG_FILE"
else
    if [ -f "$CONFIG_TEMPLATE" ]; then
        cp "$CONFIG_TEMPLATE" "$CONFIG_FILE"
        success "Created configuration from template: $CONFIG_FILE"
        echo ""
        echo "  Please edit $CONFIG_FILE to set:"
        echo "    OBSIDIAN_VAULT_PATH=/path/to/your/vault"
        echo ""
    else
        warn "Configuration template not found: $CONFIG_TEMPLATE"
        echo "  Create $CONFIG_FILE manually with:"
        echo "    OBSIDIAN_VAULT_PATH=/path/to/your/vault"
    fi
fi

# Check if vault path is configured
VAULT_PATH=""
if [ -f "$CONFIG_FILE" ]; then
    VAULT_PATH=$(grep "^OBSIDIAN_VAULT_PATH=" "$CONFIG_FILE" 2>/dev/null | cut -d= -f2- | tr -d '"' | tr -d "'" || true)
fi

if [ -n "$VAULT_PATH" ] && [ -d "$VAULT_PATH" ]; then
    success "Vault configured and accessible: $VAULT_PATH"
elif [ -n "$VAULT_PATH" ]; then
    warn "Vault path configured but directory not found: $VAULT_PATH"
else
    warn "OBSIDIAN_VAULT_PATH is not configured."
    echo ""
    echo "  To use this skill, edit $CONFIG_FILE and set:"
    echo "    OBSIDIAN_VAULT_PATH=/path/to/your/obsidian/vault"
    echo ""
fi

#===============================================================================
# Step 4: Register MCP server with Claude
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
# Step 5: Activate the skill
#===============================================================================
step "Activating the skill"

# Activate the skill via the skill manager if lobster is available
ACTIVATE_SCRIPT="$LOBSTER_DIR/src/mcp"
if [ -f "$ACTIVATE_SCRIPT/skill_manager.py" ]; then
    "$PYTHON_PATH" -c "
import sys; sys.path.insert(0, '$ACTIVATE_SCRIPT')
from skill_manager import activate_skill
result = activate_skill('obsidian-km')
print(result)
" 2>/dev/null && success "Skill activated in Lobster skill manager" || warn "Could not activate via skill manager (will work after restart)"
fi

#===============================================================================
# Done
#===============================================================================
echo ""
echo -e "${GREEN}${BOLD}Obsidian KM skill installed!${NC}"
echo ""
echo "  Configuration: $CONFIG_FILE"
echo ""
echo "  Tools available to Lobster:"
echo "    obsidian_create_note   - Create a note in the vault"
echo "    obsidian_search        - Search vault content"
echo "    obsidian_capture_link  - Archive a link to the vault"
echo "    obsidian_get_preferences - View current settings"
echo ""
echo "  Preferences (edit $CONFIG_FILE):"
echo "    OBSIDIAN_VAULT_PATH        - Path to your Obsidian vault (required)"
echo "    OBSIDIAN_DEFAULT_FOLDER    - Default folder for new notes (default: Inbox)"
echo "    OBSIDIAN_LINK_FOLDER       - Folder for captured links (default: Links)"
echo "    OBSIDIAN_AUTO_CAPTURE_LINKS - Auto-capture links (default: true)"
echo "    OBSIDIAN_DEFAULT_TAGS      - Default tags for notes (comma-separated)"
echo "    OBSIDIAN_MAX_SEARCH_RESULTS - Max search results (default: 10)"
echo ""
echo "  Restart Lobster to activate: lobster restart"
echo "    or: systemctl --user restart lobster-claude"
echo ""
