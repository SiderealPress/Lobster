#!/bin/bash
#===============================================================================
# Image Generation Skill Installer for Lobster
#
# Sets up the image generation skill that lets Lobster generate images using
# Google AI Studio Imagen 3 and send them directly to Telegram.
#
# Usage: bash ~/lobster/lobster-shop/image-generation/install.sh
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
SKILL_DIR="$LOBSTER_DIR/lobster-shop/image-generation"
SRC_DIR="$SKILL_DIR/src"
VENV_DIR="$LOBSTER_DIR/.venv"
PYTHON_PATH="$VENV_DIR/bin/python"
CONFIG_DIR="${LOBSTER_CONFIG_DIR:-$HOME/lobster-config}"

echo ""
echo -e "${BOLD}Image Generation Skill Installer${NC}"
echo "=================================="
echo ""
echo "This installs the image generation skill for Lobster."
echo "It adds generate_image and send_image tools to Claude."
echo "Provider: Google AI Studio Imagen 3"
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
if [ ! -f "$SRC_DIR/image_gen_mcp_server.py" ]; then
    error "Skill source not found at $SRC_DIR/image_gen_mcp_server.py"
    exit 1
fi
success "Skill source found"

#===============================================================================
# Step 2: Install Python dependencies
#===============================================================================
step "Installing Python dependencies (httpx, mcp)"

if [ -f "$VENV_DIR/bin/pip" ]; then
    "$VENV_DIR/bin/pip" install --quiet "httpx>=0.24" "mcp>=1.0" 2>&1 || warn "pip install had issues (may already be installed)"
    success "Python dependencies installed in Lobster venv"
else
    pip3 install --quiet "httpx>=0.24" "mcp>=1.0" 2>&1 || warn "pip3 install had issues"
    success "Python dependencies installed"
fi

#===============================================================================
# Step 3: Check for API key
#===============================================================================
step "Checking API key configuration"

GOOGLE_KEY_VAL="${GOOGLE_AI_STUDIO_KEY:-}"

# Check config files if not in env
if [ -z "$GOOGLE_KEY_VAL" ] && [ -f "$CONFIG_DIR/config.env" ]; then
    GOOGLE_KEY_VAL=$(grep "^GOOGLE_AI_STUDIO_KEY=" "$CONFIG_DIR/config.env" 2>/dev/null | cut -d= -f2- | tr -d '"' | tr -d "'" || true)
fi

if [ -z "$GOOGLE_KEY_VAL" ] && [ -f "$HOME/lobster/config/config.env" ]; then
    GOOGLE_KEY_VAL=$(grep "^GOOGLE_AI_STUDIO_KEY=" "$HOME/lobster/config/config.env" 2>/dev/null | cut -d= -f2- | tr -d '"' | tr -d "'" || true)
fi

if [ -n "$GOOGLE_KEY_VAL" ]; then
    success "GOOGLE_AI_STUDIO_KEY found — will use Google Imagen 3"
else
    warn "GOOGLE_AI_STUDIO_KEY is not configured."
    echo ""
    echo "  To use this skill, add your Google AI Studio key:"
    echo "    GOOGLE_AI_STUDIO_KEY=your_key_here"
    echo ""
    echo "  Add to: $CONFIG_DIR/config.env"
    echo "  Get a key at: https://aistudio.google.com/apikey"
    echo ""
    echo "  The skill will be registered now but image generation will fail"
    echo "  until the API key is configured."
    echo ""
fi

#===============================================================================
# Step 4: Register MCP server with Claude
#===============================================================================
step "Registering MCP server with Claude"

# Remove old registration if it exists
claude mcp remove image-generation 2>/dev/null || true

# Register the Python MCP server
if claude mcp add image-generation -s user -- "$PYTHON_PATH" "$SRC_DIR/image_gen_mcp_server.py" 2>/dev/null; then
    success "MCP server registered: image-generation"
else
    warn "Could not register MCP server automatically."
    echo "  Register manually with:"
    echo "  claude mcp add image-generation -s user -- $PYTHON_PATH $SRC_DIR/image_gen_mcp_server.py"
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
result = activate_skill('image-generation')
print(result)
" 2>/dev/null && success "Skill activated in Lobster skill manager" || warn "Could not activate via skill manager (will work after restart)"
fi

#===============================================================================
# Done
#===============================================================================
echo ""
echo -e "${GREEN}${BOLD}Image Generation skill installed!${NC}"
echo ""
if [ -n "$GOOGLE_KEY_VAL" ]; then
    echo "  Provider: Google AI Studio Imagen 3"
else
    echo "  Provider: none configured (add GOOGLE_AI_STUDIO_KEY to $CONFIG_DIR/config.env)"
fi
echo ""
echo "  Tools available to Lobster:"
echo "    generate_image  - Generate an image from a text prompt"
echo "    send_image      - Send an image URL/file to Telegram as a photo"
echo ""
echo "  Example usage:"
echo "    generate_image(prompt='a red fox in a snowy forest, photorealistic', chat_id=<owner_chat_id>)"
echo ""
echo "  Commands the owner can send via Telegram:"
echo "    /image <prompt>    - Generate an image"
echo "    /imagine <prompt>  - Same as /image"
echo "    /img <prompt>      - Short alias"
echo ""
echo "  Restart Lobster to activate: lobster restart"
echo "    or: systemctl --user restart lobster-claude"
echo ""
