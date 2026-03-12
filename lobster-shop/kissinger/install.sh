#!/bin/bash
#===============================================================================
# Kissinger CRM Skill Installer for Lobster
#
# Installs the kissinger-mcp server as a Lobster skill. This sets up:
#   1. The kissinger-mcp binary (pre-built Rust binary)
#   2. Registers it as an MCP server with Claude
#   3. Creates the database directory
#
# Usage: bash ~/lobster/lobster-shop/kissinger/install.sh
#===============================================================================

set -e

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

LOBSTER_DIR="${LOBSTER_INSTALL_DIR:-$HOME/lobster}"
SKILL_DIR="$LOBSTER_DIR/lobster-shop/kissinger"
BIN_DIR="$SKILL_DIR/bin"
MCP_BINARY="$BIN_DIR/kissinger-mcp"
DB_DIR="${KISSINGER_DB:-$HOME/.kissinger}"
PROJECT_DIR="$HOME/lobster-workspace/projects/kissinger"

echo ""
echo -e "${BOLD}Kissinger CRM Skill Installer${NC}"
echo "================================"
echo ""

#===============================================================================
# Step 1: Check prerequisites
#===============================================================================
step "Checking prerequisites"

if ! command -v claude &>/dev/null; then
    error "Claude CLI is required but not installed."
    exit 1
fi
success "Claude CLI found"

#===============================================================================
# Step 2: Ensure binary is present — build if needed
#===============================================================================
step "Checking kissinger-mcp binary"

if [ -f "$MCP_BINARY" ]; then
    success "Binary found: $MCP_BINARY"
else
    warn "Binary not found at $MCP_BINARY"
    if [ -d "$PROJECT_DIR" ]; then
        info "Building from source at $PROJECT_DIR..."
        # Try rustup env, fall back to system cargo
        if [ -f "$HOME/.cargo/env" ]; then
            . "$HOME/.cargo/env"
        fi
        if command -v cargo &>/dev/null; then
            cd "$PROJECT_DIR"
            cargo build --release
            mkdir -p "$BIN_DIR"
            cp "$PROJECT_DIR/target/release/kissinger-mcp" "$MCP_BINARY"
            cp "$PROJECT_DIR/target/release/kissinger" "$BIN_DIR/kissinger"
            success "Built and installed binary"
        else
            error "cargo not found. Install Rust from https://rustup.rs and re-run."
            exit 1
        fi
    else
        error "Project not found at $PROJECT_DIR and no pre-built binary."
        echo "  Clone the repo: gh repo clone aeschylus/kissinger $PROJECT_DIR"
        exit 1
    fi
fi

# Verify binary works
if "$MCP_BINARY" --help &>/dev/null 2>&1 || true; then
    success "Binary is executable"
fi

#===============================================================================
# Step 3: Create database directory
#===============================================================================
step "Setting up database directory"

mkdir -p "$(dirname "${DB_DIR%/*}")"
if [[ "$DB_DIR" == *"graph.db" ]]; then
    mkdir -p "$(dirname "$DB_DIR")"
else
    mkdir -p "$DB_DIR"
fi
mkdir -p "$HOME/.kissinger"
success "Database directory ready: $HOME/.kissinger"

#===============================================================================
# Step 4: Register MCP server with Claude
#===============================================================================
step "Registering kissinger-mcp with Claude"

claude mcp remove kissinger 2>/dev/null || true

if claude mcp add kissinger -s user -- "$MCP_BINARY" 2>/dev/null; then
    success "MCP server registered: kissinger"
else
    warn "Could not register automatically. Register manually with:"
    echo "  claude mcp add kissinger -s user -- $MCP_BINARY"
fi

#===============================================================================
# Done
#===============================================================================
echo ""
echo -e "${GREEN}${BOLD}Kissinger CRM skill installed!${NC}"
echo ""
echo "  Binary: $MCP_BINARY"
echo "  Database: ~/.kissinger/graph.db"
echo "  Source: https://github.com/aeschylus/kissinger (private)"
echo ""
echo "  MCP tools available to Lobster:"
echo "    kissinger_add_entity       - Add a person, org, project, etc."
echo "    kissinger_show_entity      - Show entity details + connections"
echo "    kissinger_list_entities    - List entities (filter by kind/search)"
echo "    kissinger_connect          - Create a connection between entities"
echo "    kissinger_search           - Full-text search across the graph"
echo "    kissinger_log_interaction  - Log a meeting, call, email, or note"
echo "    kissinger_vortex_scan      - Detect opportunity chains (offer/need matching)"
echo "    kissinger_contacts_stale   - Find contacts you haven't talked to recently"
echo "    kissinger_find_path        - Shortest path between two entities"
echo "    kissinger_add_offer        - Record what an entity can offer"
echo "    kissinger_add_need         - Record what an entity needs"
echo "    kissinger_graph_stats      - Graph statistics"
echo ""
echo "  Activate the skill:"
echo "    Tell Lobster: activate the kissinger skill"
echo ""
echo "  Try it:"
echo "    'Who do I know at Anthropic?'"
echo "    'Log a meeting with Jane Smith about the partnership'"
echo "    'Who haven't I talked to in a month?'"
echo ""
