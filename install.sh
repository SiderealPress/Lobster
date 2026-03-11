#!/bin/bash
#===============================================================================
# Lobster Bootstrap Installer
#
# Usage: bash <(curl -fsSL https://raw.githubusercontent.com/SiderealPress/lobster/main/install.sh)
#
# This script sets up a complete Lobster installation on a fresh VM:
# - Installs system dependencies (Ubuntu/Debian or Amazon Linux 2023/Fedora)
# - Clones the repo (if needed)
# - Walks through configuration
# - Sets up Python environment
# - Registers MCP servers with Claude
# - Installs and starts systemd services
#===============================================================================

set -euo pipefail

# Suppress needrestart interactive prompts on Ubuntu/Debian
# Without this, apt operations can hang waiting for user input
# when libraries used by running services are upgraded.
export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_MODE=a

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

# Logging functions
info() { echo -e "${BLUE}[INFO]${NC} $1"; }
success() { echo -e "${GREEN}[OK]${NC} $1"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; }
step() { echo -e "\n${CYAN}${BOLD}▶ $1${NC}"; }

# Parse install mode from arguments
DEV_MODE=false
NON_INTERACTIVE=false
for arg in "$@"; do
    case "$arg" in
        --dev) DEV_MODE=true ;;
        --non-interactive|--skip-config) NON_INTERACTIVE=true ;;
    esac
done

# Configuration - can be overridden by environment variables or config file
REPO_URL="${LOBSTER_REPO_URL:-https://github.com/SiderealPress/lobster.git}"
REPO_BRANCH="${LOBSTER_BRANCH:-main}"
INSTALL_DIR="${LOBSTER_INSTALL_DIR:-$HOME/lobster}"
WORKSPACE_DIR="${LOBSTER_WORKSPACE:-$HOME/lobster-workspace}"
PROJECTS_DIR="${LOBSTER_PROJECTS:-$WORKSPACE_DIR/projects}"
MESSAGES_DIR="${LOBSTER_MESSAGES:-$HOME/messages}"
GITHUB_REPO="SiderealPress/lobster"
GITHUB_API="https://api.github.com/repos/$GITHUB_REPO"

#===============================================================================
# Package Manager Detection
#===============================================================================

if command -v apt-get &>/dev/null; then
    PKG_MANAGER="apt"
elif command -v dnf &>/dev/null; then
    PKG_MANAGER="dnf"
else
    echo "Unsupported package manager. Install requires apt-get or dnf."
    exit 1
fi

# install_pkg <pkg-apt> [pkg-dnf]
# If only one argument is given, uses the same name for both managers.
install_pkg() {
    local pkg_apt="$1"
    local pkg_dnf="${2:-$1}"
    if [ "$PKG_MANAGER" = "apt" ]; then
        sudo apt-get install -y -qq "$pkg_apt"
    else
        sudo dnf install -y "$pkg_dnf"
    fi
}

# pkg_installed <name>  -- true when dpkg/rpm reports the package installed
pkg_installed() {
    local name="$1"
    if [ "$PKG_MANAGER" = "apt" ]; then
        dpkg -s "$name" &>/dev/null
    else
        rpm -q "$name" &>/dev/null
    fi
}

#===============================================================================
# Load Configuration
#===============================================================================

# Determine script directory for finding config relative to script location
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Configuration file path - check multiple locations
# Priority: 1) LOBSTER_CONFIG_FILE env var, 2) script directory, 3) install directory
CONFIG_FILE="${LOBSTER_CONFIG_FILE:-}"

if [ -z "$CONFIG_FILE" ]; then
    if [ -f "$SCRIPT_DIR/config/lobster.conf" ]; then
        CONFIG_FILE="$SCRIPT_DIR/config/lobster.conf"
    elif [ -f "$INSTALL_DIR/config/lobster.conf" ]; then
        CONFIG_FILE="$INSTALL_DIR/config/lobster.conf"
    fi
fi

if [ -n "$CONFIG_FILE" ] && [ -f "$CONFIG_FILE" ]; then
    # Source configuration file
    # shellcheck source=/dev/null
    source "$CONFIG_FILE"

    # Re-apply configuration variables (config file may have set LOBSTER_* vars)
    REPO_URL="${LOBSTER_REPO_URL:-$REPO_URL}"
    REPO_BRANCH="${LOBSTER_BRANCH:-$REPO_BRANCH}"
    INSTALL_DIR="${LOBSTER_INSTALL_DIR:-$INSTALL_DIR}"
    WORKSPACE_DIR="${LOBSTER_WORKSPACE:-$WORKSPACE_DIR}"
    PROJECTS_DIR="${LOBSTER_PROJECTS:-$WORKSPACE_DIR/projects}"
    MESSAGES_DIR="${LOBSTER_MESSAGES:-$MESSAGES_DIR}"
fi

# User configuration with fallbacks for non-interactive contexts
LOBSTER_USER="${LOBSTER_USER:-${USER:-$(whoami)}}"
LOBSTER_GROUP="${LOBSTER_GROUP:-${USER:-$(whoami)}}"
LOBSTER_HOME="${LOBSTER_HOME:-$HOME}"
CONFIG_DIR="${LOBSTER_CONFIG_DIR:-$HOME/lobster-config}"

#===============================================================================
# Template Processing
#===============================================================================

# Generate a file from a template by substituting {{VARIABLE}} placeholders
# Arguments:
#   $1 - template file path
#   $2 - output file path
generate_from_template() {
    local template="$1"
    local output="$2"

    if [ ! -f "$template" ]; then
        error "Template not found: $template"
        return 1
    fi

    sed -e "s|{{USER}}|${LOBSTER_USER}|g" \
        -e "s|{{GROUP}}|${LOBSTER_GROUP}|g" \
        -e "s|{{HOME}}|${LOBSTER_HOME}|g" \
        -e "s|{{INSTALL_DIR}}|${INSTALL_DIR}|g" \
        -e "s|{{WORKSPACE_DIR}}|${WORKSPACE_DIR}|g" \
        -e "s|{{MESSAGES_DIR}}|${MESSAGES_DIR}|g" \
        -e "s|{{CONFIG_DIR}}|${CONFIG_DIR}|g" \
        -e "s|{{CLAUDE_WRAPPER}}|${CLAUDE_WRAPPER}|g" \
        "$template" > "$output"

    success "Generated: $output"
}

#===============================================================================
# Private Configuration Overlay
#===============================================================================

# Apply private configuration overlay from LOBSTER_CONFIG_DIR
# This function overlays customizations from a private config directory
# onto the public repo installation.
apply_private_overlay() {
    local config_dir="${LOBSTER_CONFIG_DIR:-}"

    if [ -z "$config_dir" ]; then
        step "No private config directory specified (LOBSTER_CONFIG_DIR)"
        return 0
    fi

    if [ ! -d "$config_dir" ]; then
        warn "Private config directory not found: $config_dir"
        return 0
    fi

    step "Applying private configuration overlay from: $config_dir"

    # Copy config.env if exists
    if [ -f "$config_dir/config.env" ]; then
        cp "$config_dir/config.env" "$CONFIG_DIR/config.env"
        success "Applied: config.env"
    fi

    # Overlay CLAUDE.md if exists (replaces default)
    if [ -f "$config_dir/CLAUDE.md" ]; then
        cp "$config_dir/CLAUDE.md" "$WORKSPACE_DIR/CLAUDE.md"
        success "Applied: CLAUDE.md"
    fi

    # Merge custom agents (additive)
    if [ -d "$config_dir/agents" ]; then
        mkdir -p "$INSTALL_DIR/.claude/agents"
        local agent_count=0
        for agent in "$config_dir/agents"/*.md; do
            [ -f "$agent" ] || continue
            cp "$agent" "$INSTALL_DIR/.claude/agents/"
            success "Applied agent: $(basename "$agent")"
            agent_count=$((agent_count + 1))
        done
        if [ "$agent_count" -eq 0 ]; then
            info "No agent files found in $config_dir/agents/"
        fi
    fi

    # Copy scheduled tasks (additive)
    if [ -d "$config_dir/scheduled-tasks" ]; then
        mkdir -p "$INSTALL_DIR/scheduled-tasks/tasks"
        local task_count=0
        for task in "$config_dir/scheduled-tasks"/*; do
            [ -e "$task" ] || continue
            cp -r "$task" "$INSTALL_DIR/scheduled-tasks/"
            success "Applied: scheduled-tasks/$(basename "$task")"
            task_count=$((task_count + 1))
        done
        if [ "$task_count" -eq 0 ]; then
            info "No scheduled task files found in $config_dir/scheduled-tasks/"
        fi
    fi

    success "Private overlay applied successfully"
}

#===============================================================================
# Hooks
#===============================================================================

# Run a hook script from the private config directory
# Arguments:
#   $1 - hook name (e.g., "post-install.sh", "post-update.sh")
run_hook() {
    local hook_name="$1"
    local config_dir="${LOBSTER_CONFIG_DIR:-}"
    local hook_path="$config_dir/hooks/$hook_name"

    if [ -z "$config_dir" ]; then
        return 0
    fi

    if [ ! -f "$hook_path" ]; then
        return 0
    fi

    if [ ! -x "$hook_path" ]; then
        warn "Hook exists but is not executable: $hook_path"
        warn "Make it executable with: chmod +x $hook_path"
        return 0
    fi

    step "Running hook: $hook_name"

    # Export useful variables for hooks
    export LOBSTER_INSTALL_DIR="$INSTALL_DIR"
    export LOBSTER_WORKSPACE_DIR="$WORKSPACE_DIR"
    export LOBSTER_PROJECTS_DIR="$PROJECTS_DIR"
    export LOBSTER_MESSAGES_DIR="$MESSAGES_DIR"

    "$hook_path"
    local exit_code=$?
    if [ $exit_code -eq 0 ]; then
        success "Hook completed: $hook_name"
    else
        warn "Hook failed: $hook_name (exit code: $exit_code)"
    fi
}

#===============================================================================
# Banner
#===============================================================================

echo -e "${BLUE}"
cat << 'BANNER'
╔═══════════════════════════════════════════════════════════════╗
║                                                               ║
║   ██╗      ██████╗ ██████╗ ███████╗████████╗███████╗██████╗   ║
║   ██║     ██╔═══██╗██╔══██╗██╔════╝╚══██╔══╝██╔════╝██╔══██╗  ║
║   ██║     ██║   ██║██████╔╝███████╗   ██║   █████╗  ██████╔╝  ║
║   ██║     ██║   ██║██╔══██╗╚════██║   ██║   ██╔══╝  ██╔══██╗  ║
║   ███████╗╚██████╔╝██████╔╝███████║   ██║   ███████╗██║  ██║  ║
║   ╚══════╝ ╚═════╝ ╚═════╝ ╚══════╝   ╚═╝   ╚══════╝╚═╝  ╚═╝  ║
║                                                               ║
║         Always-on Claude Code Message Processor               ║
║                                                               ║
╚═══════════════════════════════════════════════════════════════╝
BANNER
echo -e "${NC}"

# Show loaded configuration info
if [ -n "$CONFIG_FILE" ] && [ -f "$CONFIG_FILE" ]; then
    info "Loaded configuration from: $CONFIG_FILE"
fi
