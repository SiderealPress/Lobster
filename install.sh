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
    # Note: $WORKSPACE_DIR/CLAUDE.md is a symlink to $INSTALL_DIR/CLAUDE.md;
    # write to the symlink target so the repo file is updated, not the symlink.
    if [ -f "$config_dir/CLAUDE.md" ]; then
        cp "$config_dir/CLAUDE.md" "$INSTALL_DIR/CLAUDE.md"
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

#===============================================================================
# Pre-flight checks
#===============================================================================

step "Running pre-flight checks..."

# Detect package manager
if command -v apt-get &>/dev/null; then
    info "Detected package manager: apt"
    PKG_MANAGER="apt"
    success "Ubuntu/Debian system detected"
elif command -v dnf &>/dev/null; then
    info "Detected package manager: dnf"
    PKG_MANAGER="dnf"
    success "Amazon Linux/Fedora system detected"
fi

# Check sudo access
if ! sudo -n true 2>/dev/null; then
    if ! sudo true 2>/dev/null; then
        error "Sudo access is required. Please run as a user with sudo privileges."
        exit 1
    fi
fi
success "Sudo access confirmed"

# Check internet connectivity
if curl -s --connect-timeout 5 https://github.com > /dev/null 2>&1; then
    success "Internet connectivity confirmed"
else
    error "No internet connection detected. Please check your network."
    exit 1
fi

# Check Python (informational)
if ! command -v python3 &>/dev/null; then
    warn "Python3 not found. Will install."
fi

# Check Claude Code (informational)
if command -v claude &>/dev/null; then
    success "Claude Code found"
else
    warn "Claude Code not found. Will install."
fi

#===============================================================================
# System Dependencies
#===============================================================================

step "Installing system dependencies..."

if [ "$PKG_MANAGER" = "apt" ]; then
    # Refresh package list
    sudo apt-get update -qq 2>/dev/null

    # Add GitHub CLI apt repository
    info "Adding GitHub CLI apt repository..."
    if ! pkg_installed gh; then
        type -p curl >/dev/null || install_pkg curl
        curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | sudo dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg 2>/dev/null
        chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg 2>/dev/null || true
        echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | sudo tee /etc/apt/sources.list.d/github-cli.list > /dev/null
        sudo apt-get update -qq 2>/dev/null
    fi

    # Install packages
    PACKAGES=(
        wget git jq gh python3 python3-pip python3-venv
        cron at expect tmux cmake
        ffmpeg ripgrep fd-find bat fzf mosh
    )
    for pkg in "${PACKAGES[@]}"; do
        if pkg_installed "$pkg"; then
            true  # already installed
        else
            info "Installing $pkg..."
            sudo apt-get install -y -qq "$pkg" 2>/dev/null || warn "Could not install $pkg (may be unavailable on this architecture)"
        fi
    done

else
    # DNF (Amazon Linux / Fedora)
    info "Adding GitHub CLI dnf repository..."
    sudo dnf config-manager --add-repo https://cli.github.com/packages/rpm/gh-cli.repo 2>/dev/null || true

    PACKAGES=(
        wget git jq gh python3 python3-pip
        cronie at expect tmux cmake
        ripgrep fd-find fzf
    )
    for pkg in "${PACKAGES[@]}"; do
        info "Installing $pkg..."
        sudo dnf install -y "$pkg" 2>/dev/null || warn "Could not install $pkg"
    done
fi

success "Core system dependencies installed"

#===============================================================================
# Install Claude Code
#===============================================================================

# Only install if not already present
if ! command -v claude &>/dev/null; then
    step "Installing Claude Code..."
    curl -fsSL https://claude.ai/install.sh | bash
    success "Claude Code installed"
fi

# Ensure claude is in PATH for the rest of this script
export PATH="$HOME/.local/bin:$PATH"
info "Added ~/.local/bin to PATH in $HOME/.bashrc"
success "Claude Code installed"

#===============================================================================
# Check for existing Claude Code session
#===============================================================================

step "Checking existing Claude Code authentication..."

CLAUDE_CREDENTIALS="$HOME/.claude/.credentials.json"
if [ -f "$CLAUDE_CREDENTIALS" ]; then
    info "Credentials found, verifying token is still valid..."
    if python3 -c "
import json, time, sys
with open('$CLAUDE_CREDENTIALS') as f:
    creds = json.load(f)
# Check token expiry (expiresAt is epoch ms)
expires_at = creds.get('expiresAt', 0)
if expires_at > 0 and expires_at / 1000 < time.time():
    print('Token expired', file=sys.stderr)
    sys.exit(1)
" 2>/dev/null; then
        success "Claude Code authenticated via OAuth (token verified)"
    else
        warn "OAuth credentials exist but token is expired or invalid."
        warn "You'll need to re-authenticate during the auth setup step."
    fi
fi

#===============================================================================
# Determine install mode
#===============================================================================

# Determine if this is a git-based install (vs tarball)
INSTALL_MODE="git"

if [ -d "$INSTALL_DIR/.git" ]; then
    info "Existing git install detected"
elif [ -d "$SCRIPT_DIR/.git" ]; then
    # Running from within the repo (e.g. during dev or bind-mount testing)
    info "Running from git repo: $SCRIPT_DIR"
    INSTALL_DIR="$SCRIPT_DIR"
fi

#===============================================================================
# Clone or update repo
#===============================================================================

step "Setting up Lobster repository..."

if [ -d "$INSTALL_DIR/.git" ]; then
    info "Repository exists. Updating..."
    cd "$INSTALL_DIR"
    # Only pull if working tree is clean (avoid clobbering local changes)
    if git diff --quiet HEAD 2>/dev/null; then
        git fetch origin 2>/dev/null || warn "Could not fetch from remote"
        git pull --ff-only origin "$REPO_BRANCH" 2>/dev/null || \
            warn "Could not fast-forward — run 'git pull' manually if needed"
    else
        warn "Local changes detected — skipping git pull to preserve them"
    fi
    CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "unknown")
    success "Repository ready at $INSTALL_DIR (branch: $CURRENT_BRANCH)"
else
    info "Cloning $REPO_URL..."
    git clone --branch "$REPO_BRANCH" "$REPO_URL" "$INSTALL_DIR"
    cd "$INSTALL_DIR"
    success "Repository cloned"
fi

cd "$INSTALL_DIR"

#===============================================================================
# Configure git hooks
#===============================================================================

step "Configuring distributed git hooks..."
git config core.hooksPath .githooks 2>/dev/null || true
success "Git hooks configured (core.hooksPath -> .githooks)"

#===============================================================================
# Create directory structure
#===============================================================================

step "Creating directories..."

mkdir -p \
    "$WORKSPACE_DIR/logs" \
    "$WORKSPACE_DIR/data" \
    "$WORKSPACE_DIR/memory/canonical" \
    "$WORKSPACE_DIR/memory/archive/digests" \
    "$WORKSPACE_DIR/scheduled-jobs/tasks" \
    "$WORKSPACE_DIR/scheduled-jobs/logs" \
    "$MESSAGES_DIR/inbox" \
    "$MESSAGES_DIR/outbox" \
    "$MESSAGES_DIR/processing" \
    "$MESSAGES_DIR/processed" \
    "$MESSAGES_DIR/failed" \
    "$MESSAGES_DIR/audio" \
    "$MESSAGES_DIR/task-outputs" \
    "$MESSAGES_DIR/config"

# Projects directory
mkdir -p "$PROJECTS_DIR"
info "  $PROJECTS_DIR - All Lobster-managed projects"

# Seed canonical memory templates from repo
SEED_TEMPLATES_DIR="$INSTALL_DIR/memory/canonical-templates"
CANONICAL_DIR="$WORKSPACE_DIR/memory/canonical"

if [ -d "$SEED_TEMPLATES_DIR" ]; then
    for tmpl in "$SEED_TEMPLATES_DIR"/*.md; do
        [ -f "$tmpl" ] || continue
        dest_file="$CANONICAL_DIR/$(basename "$tmpl")"
        if [ ! -f "$dest_file" ]; then
            cp "$tmpl" "$dest_file"
            info "  Seeded canonical template: $(basename "$tmpl")"
        fi
    done
fi

success "Directories created"
info "  $PROJECTS_DIR - All Lobster-managed projects"

#===============================================================================
# Global environment store
#===============================================================================

step "Setting up global environment store..."

mkdir -p "$CONFIG_DIR"

GLOBAL_ENV="$CONFIG_DIR/global.env"
if [ ! -f "$GLOBAL_ENV" ]; then
    cat > "$GLOBAL_ENV" << 'GLOBALENV'
# Lobster Global Environment Store
#
# Use 'lobster env set KEY VALUE' to add entries.
# Use 'lobster env list' to see all stored keys.
# This file is sourced on shell login via ~/.bashrc / ~/.profile.
GLOBALENV
fi

success "Global env store created: $GLOBAL_ENV"

# Shell integration: load global.env on login
GLOBALENV_SNIPPET='
# Lobster global env store
if [ -f "$HOME/lobster-config/global.env" ]; then
    set -o allexport
    # shellcheck source=/dev/null
    source "$HOME/lobster-config/global.env"
    set +o allexport
fi'

for shell_rc in "$HOME/.bashrc" "$HOME/.profile"; do
    if [ -f "$shell_rc" ] && ! grep -q "lobster-config/global.env" "$shell_rc"; then
        echo "$GLOBALENV_SNIPPET" >> "$shell_rc"
        info "  Shell integration added to $shell_rc"
    fi
done

success "Global env store configured"
info "  File: $GLOBAL_ENV"
info "  Use 'lobster env set KEY VALUE' to store API tokens"
info "  Use 'lobster env list' to see stored keys"
info "  See docs/GLOBAL-ENV.md for full documentation"

#===============================================================================
# Scheduled tasks infrastructure
#===============================================================================

step "Setting up scheduled tasks infrastructure..."

# Executables
chmod +x "$INSTALL_DIR/scheduled-tasks/run-job.sh" 2>/dev/null || true
chmod +x "$INSTALL_DIR/scheduled-tasks/sync-crontab.sh" 2>/dev/null || true

# Initialize jobs registry
JOBS_FILE="$WORKSPACE_DIR/scheduled-jobs/jobs.json"
if [ ! -f "$JOBS_FILE" ]; then
    echo '{"jobs": {}}' > "$JOBS_FILE"
fi

# Seed tasks from repo
TASKS_SRC="$INSTALL_DIR/scheduled-tasks/tasks"
TASKS_DST="$WORKSPACE_DIR/scheduled-jobs/tasks"
if [ -d "$TASKS_SRC" ]; then
    for task_file in "$TASKS_SRC"/*.md; do
        [ -f "$task_file" ] || continue
        dst="$TASKS_DST/$(basename "$task_file")"
        if [ ! -f "$dst" ]; then
            cp "$task_file" "$dst"
        fi
    done
fi

# Enable cron service (name differs by distro)
if [ "$PKG_MANAGER" = "apt" ]; then
    sudo systemctl enable cron 2>/dev/null || true
    sudo systemctl start cron 2>/dev/null || true
else
    # Amazon Linux / Fedora uses crond
    sudo systemctl enable crond 2>/dev/null || true
    sudo systemctl start crond 2>/dev/null || true
fi

# Enable atd service (for self-check reminders via 'at' command)
sudo systemctl enable atd 2>/dev/null || true
sudo systemctl start atd 2>/dev/null || true

success "Scheduled tasks infrastructure ready"

#===============================================================================
# Health check
#===============================================================================

step "Setting up health monitoring..."

chmod +x "$INSTALL_DIR/scripts/health-check-v3.sh" 2>/dev/null || true
chmod +x "$INSTALL_DIR/scripts/self-check-reminder.sh" 2>/dev/null || true

HEALTH_MARKER="# LOBSTER-HEALTH"
({ crontab -l 2>/dev/null | grep -v "$HEALTH_MARKER" | grep -v "health-check" || true; } ; \
 echo "*/2 * * * * $INSTALL_DIR/scripts/health-check-v3.sh >> $WORKSPACE_DIR/logs/health-check.log 2>&1 $HEALTH_MARKER") \
 | crontab -

success "Health monitoring configured (checks every 2 minutes)"

#===============================================================================
# Daily dependency health check
#===============================================================================

step "Setting up daily dependency health check..."

DEPCHECK_MARKER="# LOBSTER-DEPCHECK"
({ crontab -l 2>/dev/null | grep -v "$DEPCHECK_MARKER" | grep -v "dependency-health-check" || true; } ; \
 echo "0 6 * * * $INSTALL_DIR/scripts/dependency-health-check.sh >> $WORKSPACE_DIR/logs/dependency-health.log 2>&1 $DEPCHECK_MARKER") \
 | crontab -

success "Daily dependency health check configured (runs at 06:00 daily)"

#===============================================================================
# Nightly consolidation
#===============================================================================

step "Setting up nightly consolidation..."

CONSOLIDATION_MARKER="# LOBSTER-CONSOLIDATION"
({ crontab -l 2>/dev/null | grep -v "$CONSOLIDATION_MARKER" | grep -v "nightly-consolidation" || true; } ; \
 echo "0 3 * * * $INSTALL_DIR/scripts/nightly-consolidation.sh >> $WORKSPACE_DIR/logs/nightly-consolidation.log 2>&1 $CONSOLIDATION_MARKER") \
 | crontab -

success "Nightly consolidation configured (runs at 03:00 nightly)"

#===============================================================================
# Claude Code settings (hooks, self-check)
#===============================================================================

step "Setting up self-check reminder system..."

mkdir -p "$HOME/.claude"
CLAUDE_SETTINGS="$HOME/.claude/settings.json"

if [ ! -f "$CLAUDE_SETTINGS" ]; then
    cat > "$CLAUDE_SETTINGS" << 'SETTINGS'
{
  "hooks": {
    "PreToolUse": [],
    "PostToolUse": [],
    "Stop": []
  }
}
SETTINGS
    success "Claude Code settings created with hooks"
fi

# Self-check cron
SELFCHECK_MARKER="# LOBSTER-SELFCHECK"
({ crontab -l 2>/dev/null | grep -v "$SELFCHECK_MARKER" | grep -v "self-check-reminder" || true; } ; \
 echo "*/3 * * * * $INSTALL_DIR/scripts/self-check-runner.sh >> $WORKSPACE_DIR/logs/self-check.log 2>&1 $SELFCHECK_MARKER") \
 | crontab - 2>/dev/null || true

success "Self-check cron configured (every 3min)"

# Set up Claude Code PreToolUse hook to block writes to .claude/memory/
HOOK_SCRIPT="$INSTALL_DIR/hooks/no-auto-memory.py"
if [ -f "$HOOK_SCRIPT" ]; then
    if ! python3 -c "
import json
with open('$CLAUDE_SETTINGS') as f:
    s = json.load(f)
hooks = s.get('hooks', {})
pre = hooks.get('PreToolUse', [])
exists = any(
    isinstance(h, dict) and h.get('command', '').endswith('no-auto-memory.py')
    for entry in pre
    for h in (entry.get('hooks', []) if isinstance(entry, dict) else [])
)
sys.exit(0 if exists else 1)
" 2>/dev/null; then
        python3 - << ADDNOMEM
import json
with open('$CLAUDE_SETTINGS') as f:
    s = json.load(f)
s.setdefault('hooks', {}).setdefault('PreToolUse', []).append(
    {"matcher": ".*", "hooks": [{"type": "command", "command": "python3 $HOOK_SCRIPT"}]}
)
with open('$CLAUDE_SETTINGS', 'w') as f:
    json.dump(s, f, indent=2)
ADDNOMEM
        success "No-auto-memory hook added to Claude Code settings"
    else
        info "No-auto-memory hook already configured in Claude Code settings"
    fi
fi

# Set up Claude Code PreToolUse hook to enforce clickable links for completed work
LINK_HOOK="$INSTALL_DIR/hooks/enforce-link-archiving.py"
if [ -f "$LINK_HOOK" ]; then
    if ! python3 -c "
import json, sys
with open('$CLAUDE_SETTINGS') as f:
    s = json.load(f)
hooks = s.get('hooks', {})
post = hooks.get('PostToolUse', [])
exists = any(
    isinstance(h, dict) and h.get('command', '').endswith('enforce-link-archiving.py')
    for entry in post
    for h in (entry.get('hooks', []) if isinstance(entry, dict) else [])
)
sys.exit(0 if exists else 1)
" 2>/dev/null; then
        python3 - << ADDLINK
import json
with open('$CLAUDE_SETTINGS') as f:
    s = json.load(f)
s.setdefault('hooks', {}).setdefault('PostToolUse', []).append(
    {"matcher": ".*", "hooks": [{"type": "command", "command": "python3 $LINK_HOOK"}]}
)
with open('$CLAUDE_SETTINGS', 'w') as f:
    json.dump(s, f, indent=2)
ADDLINK
        success "Link enforcement hook installed"
    else
        info "Link enforcement hook already configured in Claude Code settings"
    fi
fi

# Set up Claude Code PreToolUse hook to block generic Agent calls without subagent_type
SUBAGENT_HOOK="$INSTALL_DIR/hooks/require-subagent-type.py"
if [ -f "$SUBAGENT_HOOK" ]; then
    if ! python3 -c "
import json, sys
with open('$CLAUDE_SETTINGS') as f:
    s = json.load(f)
hooks = s.get('hooks', {})
pre = hooks.get('PreToolUse', [])
exists = any(
    isinstance(h, dict) and h.get('command', '').endswith('require-subagent-type.py')
    for entry in pre
    for h in (entry.get('hooks', []) if isinstance(entry, dict) else [])
)
sys.exit(0 if exists else 1)
" 2>/dev/null; then
        python3 - << ADDSUBAGENT
import json
with open('$CLAUDE_SETTINGS') as f:
    s = json.load(f)
s.setdefault('hooks', {}).setdefault('PreToolUse', []).append(
    {"matcher": "Task", "hooks": [{"type": "command", "command": "python3 $SUBAGENT_HOOK"}]}
)
with open('$CLAUDE_SETTINGS', 'w') as f:
    json.dump(s, f, indent=2)
ADDSUBAGENT
        success "require-subagent-type hook installed"
    else
        info "require-subagent-type hook already configured in Claude Code settings"
    fi
fi

# Set up Claude Code PostToolUse hook to restore execute bit after Edit/Write
RESTORE_HOOK="$INSTALL_DIR/hooks/restore-exec-bit.py"
if [ -f "$RESTORE_HOOK" ]; then
    if ! python3 -c "
import json, sys
with open('$CLAUDE_SETTINGS') as f:
    s = json.load(f)
hooks = s.get('hooks', {})
post = hooks.get('PostToolUse', [])
exists = any(
    isinstance(h, dict) and h.get('command', '').endswith('restore-exec-bit.py')
    for entry in post
    for h in (entry.get('hooks', []) if isinstance(entry, dict) else [])
)
sys.exit(0 if exists else 1)
" 2>/dev/null; then
        python3 - << ADDRESTORE
import json
with open('$CLAUDE_SETTINGS') as f:
    s = json.load(f)
s.setdefault('hooks', {}).setdefault('PostToolUse', []).append(
    {"matcher": ".*", "hooks": [{"type": "command", "command": "python3 $RESTORE_HOOK"}]}
)
with open('$CLAUDE_SETTINGS', 'w') as f:
    json.dump(s, f, indent=2)
ADDRESTORE
        success "restore-exec-bit hook installed"
    else
        info "restore-exec-bit hook already configured in Claude Code settings"
    fi
fi

#===============================================================================
# Python environment
#===============================================================================

step "Setting up Python environment..."

# Install uv
info "Installing uv (Python package manager)..."
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
success "uv installed"

UV="$HOME/.local/bin/uv"
VENV_DIR="$INSTALL_DIR/.venv"

if [ -d "$VENV_DIR" ]; then
    success "Python venv already exists"
else
    "$UV" venv "$VENV_DIR"
fi

# Core packages
"$UV" pip install --python "$VENV_DIR/bin/python" \
    mcp \
    python-telegram-bot \
    watchdog \
    python-dotenv \
    aiohttp \
    aiofiles \
    requests \
    slack-sdk \
    2>/dev/null

success "Core Python packages installed"

# fastembed (vector memory)
info "Installing fastembed..."
"$UV" pip install --python "$VENV_DIR/bin/python" fastembed 2>/dev/null \
    && success "fastembed installed" \
    || warn "fastembed install failed (vector memory won't work)"

# sqlite-vec
info "Installing sqlite-vec..."
"$UV" pip install --python "$VENV_DIR/bin/python" sqlite-vec 2>/dev/null
if "$VENV_DIR/bin/python" -c "
import sqlite_vec, sqlite3
db = sqlite3.connect(':memory:')
db.enable_load_extension(True)
sqlite_vec.load(db)
" 2>/dev/null; then
    success "sqlite-vec installed and loads correctly"
else
    warn "sqlite-vec installed but may not load (architecture mismatch or missing deps)"
fi

success "Python environment ready"

#===============================================================================
# Telegram configuration
#===============================================================================

step "Configuring Lobster..."

mkdir -p "$CONFIG_DIR"
CONFIG_ENV="$CONFIG_DIR/config.env"

if [ "$NON_INTERACTIVE" = true ]; then
    warn "Skipping Telegram configuration (non-interactive mode)."
    info "Run the installer again without --non-interactive to configure Telegram."

    if [ ! -f "$CONFIG_ENV" ]; then
        cat > "$CONFIG_ENV" << CONFIGENV
# Lobster Configuration
# Generated by install.sh --non-interactive
TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN:-}
TELEGRAM_ALLOWED_USERS=${TELEGRAM_ALLOWED_USERS:-}
ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY:-}
CONFIGENV
    fi
else
    echo ""
    echo "Please have the following ready:"
    echo "  - Telegram bot token (from @BotFather)"
    echo "  - Your Telegram user ID"
    echo "  - An Anthropic API key (from console.anthropic.com)"
    echo ""

    if [ -f "$CONFIG_ENV" ]; then
        # shellcheck source=/dev/null
        source "$CONFIG_ENV" 2>/dev/null || true
    fi

    if [ -z "${TELEGRAM_BOT_TOKEN:-}" ]; then
        read -rp "Telegram bot token: " TELEGRAM_BOT_TOKEN
    else
        read -rp "Telegram bot token [${TELEGRAM_BOT_TOKEN:0:10}...]: " input
        TELEGRAM_BOT_TOKEN="${input:-$TELEGRAM_BOT_TOKEN}"
    fi

    if [ -z "${TELEGRAM_ALLOWED_USERS:-}" ]; then
        read -rp "Your Telegram user ID(s) (comma-separated): " TELEGRAM_ALLOWED_USERS
    else
        read -rp "Telegram user ID(s) [$TELEGRAM_ALLOWED_USERS]: " input
        TELEGRAM_ALLOWED_USERS="${input:-$TELEGRAM_ALLOWED_USERS}"
    fi

    if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
        read -rp "Anthropic API key: " ANTHROPIC_API_KEY
    else
        read -rp "Anthropic API key [${ANTHROPIC_API_KEY:0:10}...]: " input
        ANTHROPIC_API_KEY="${input:-$ANTHROPIC_API_KEY}"
    fi

    cat > "$CONFIG_ENV" << CONFIGENV
# Lobster Configuration
TELEGRAM_BOT_TOKEN=$TELEGRAM_BOT_TOKEN
TELEGRAM_ALLOWED_USERS=$TELEGRAM_ALLOWED_USERS
ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY
CONFIGENV
    success "Configuration saved to $CONFIG_ENV"
fi

#===============================================================================
# GitHub integration
#===============================================================================

step "GitHub Integration (Optional)..."

if [ "$NON_INTERACTIVE" = true ]; then
    info "Skipping GitHub integration (non-interactive mode)."
    if [ -n "${GITHUB_TOKEN:-}" ]; then
        grep -q "GITHUB_TOKEN" "$CONFIG_ENV" 2>/dev/null || echo "GITHUB_TOKEN=$GITHUB_TOKEN" >> "$CONFIG_ENV"
        grep -q "GH_TOKEN" "$CONFIG_ENV" 2>/dev/null || echo "GH_TOKEN=$GITHUB_TOKEN" >> "$CONFIG_ENV"
    fi
else
    echo ""
    read -rp "Do you have a GitHub personal access token? [y/N]: " has_github
    if [[ $has_github =~ ^[Yy]$ ]]; then
        if [ -z "${GITHUB_TOKEN:-}" ]; then
            read -rp "GitHub personal access token: " GITHUB_TOKEN
        else
            read -rp "GitHub personal access token [${GITHUB_TOKEN:0:10}...]: " input
            GITHUB_TOKEN="${input:-$GITHUB_TOKEN}"
        fi
        grep -q "GITHUB_TOKEN" "$CONFIG_ENV" || {
            echo "GITHUB_TOKEN=$GITHUB_TOKEN" >> "$CONFIG_ENV"
            echo "GH_TOKEN=$GITHUB_TOKEN" >> "$CONFIG_ENV"
        }
        success "GitHub token saved"
    fi
fi

#===============================================================================
# GitHub CLI auth
#===============================================================================

step "Checking GitHub CLI authentication..."

if [ -n "${GITHUB_TOKEN:-}" ]; then
    echo "$GITHUB_TOKEN" | gh auth login --with-token 2>/dev/null \
        && success "GitHub CLI authenticated" \
        || warn "GitHub CLI auth failed — authenticate later with: gh auth login"
else
    info "Skipped. Authenticate later with: gh auth login"
fi

#===============================================================================
# whisper.cpp (voice transcription)
#===============================================================================

step "Voice Transcription Setup (whisper.cpp)..."

if command -v ffmpeg &>/dev/null; then
    success "ffmpeg is available"
else
    warn "ffmpeg not found. Voice transcription requires ffmpeg."
fi

WHISPER_DIR="$WORKSPACE_DIR/whisper.cpp"

if [ -x "$WHISPER_DIR/build/bin/whisper-cli" ]; then
    success "whisper.cpp already built"
else
    step "Building whisper.cpp (this may take a few minutes)..."

    if [ ! -d "$WHISPER_DIR" ]; then
        info "Cloning whisper.cpp..."
        git clone --depth 1 https://github.com/ggerganov/whisper.cpp.git "$WHISPER_DIR"
    fi

    cd "$WHISPER_DIR"
    cmake -B build -DCMAKE_BUILD_TYPE=Release -DWHISPER_BUILD_TESTS=OFF 2>/dev/null
    cmake --build build --config Release -j"$(nproc)" 2>/dev/null
    success "whisper.cpp built successfully"
    cd "$INSTALL_DIR"
fi

WHISPER_MODEL="$WHISPER_DIR/models/ggml-small.bin"
if [ -f "$WHISPER_MODEL" ]; then
    success "Whisper small model already downloaded"
else
    step "Downloading whisper small model (~465MB)..."
    cd "$WHISPER_DIR"
    bash models/download-ggml-model.sh small
    success "Whisper small model downloaded"
    cd "$INSTALL_DIR"
fi

WHISPER_CLI="$WHISPER_DIR/build/bin/whisper-cli"
if [ -x "$WHISPER_CLI" ]; then
    info "Verifying whisper.cpp transcription pipeline..."
    success "whisper-cli binary verified"
else
    warn "whisper-cli binary not found at $WHISPER_CLI"
fi

#===============================================================================
# Claude Code authentication
#===============================================================================

step "Setting up Claude authentication..."

if [ -f "$CONFIG_DIR/config.env" ]; then
    set -o allexport
    # shellcheck source=/dev/null
    source "$CONFIG_DIR/config.env"
    set +o allexport
fi

if [ -n "${ANTHROPIC_API_KEY:-}" ]; then
    grep -q "ANTHROPIC_API_KEY" "$CONFIG_DIR/global.env" 2>/dev/null \
        || echo "ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY" >> "$CONFIG_DIR/global.env"
    success "Using ANTHROPIC_API_KEY from environment"
elif [ -f "$HOME/.claude/.credentials.json" ]; then
    success "Using existing Claude OAuth credentials"
else
    if [ "$NON_INTERACTIVE" = true ]; then
        warn "No ANTHROPIC_API_KEY set and no OAuth credentials found."
        warn "Claude Code authentication required. Set ANTHROPIC_API_KEY or run 'claude auth login' after install."
    else
        echo ""
        echo "Claude Code authentication options:"
        echo "  1. API key (recommended for headless servers)"
        echo "  2. OAuth browser login"
        echo ""
        read -rp "Do you have an Anthropic API key? [y/N]: " has_key
        if [[ $has_key =~ ^[Yy]$ ]]; then
            read -rp "Anthropic API key: " ANTHROPIC_API_KEY
            echo "ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY" >> "$CONFIG_DIR/global.env"
            echo "ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY" >> "$CONFIG_DIR/config.env"
            success "API key saved"
        else
            info "Starting OAuth browser flow..."
            echo ""
            echo "Claude Code will generate an authentication URL."
            echo "Open it in your browser to complete authentication."
            echo ""
            claude auth login
        fi
    fi
fi

# Ensure launcher scripts are executable
chmod +x "$INSTALL_DIR/scripts/start-claude.sh" 2>/dev/null || true
chmod +x "$INSTALL_DIR/scripts/claude-persistent.sh" 2>/dev/null || true
chmod +x "$INSTALL_DIR/scripts/claude-wrapper.exp" 2>/dev/null || true
success "Claude launchers ready (start-claude.sh, claude-persistent.sh, claude-wrapper.exp)"

#===============================================================================
# MCP server: GitHub
#===============================================================================

step "Configuring GitHub MCP server..."

if command -v claude &>/dev/null; then
    # Load config to get GITHUB_TOKEN
    if [ -f "$CONFIG_DIR/config.env" ]; then
        set -o allexport
        # shellcheck source=/dev/null
        source "$CONFIG_DIR/config.env"
        set +o allexport
    fi

    GITHUB_TOKEN_VAL="${GITHUB_TOKEN:-${GH_TOKEN:-}}"

    if [ -n "$GITHUB_TOKEN_VAL" ]; then
        # Register GitHub MCP server
        claude mcp remove github 2>/dev/null || true
        if claude mcp add-json github '{"command":"npx","args":["-y","@modelcontextprotocol/server-github"],"env":{"GITHUB_PERSONAL_ACCESS_TOKEN":"'"$GITHUB_TOKEN_VAL"'"}}' 2>/dev/null; then
            success "GitHub MCP server registered"
            # Mark as configured (without storing the token in config.env plain text)
            grep -q "GITHUB_PAT_CONFIGURED" "$CONFIG_ENV" 2>/dev/null \
                || echo "GITHUB_PAT_CONFIGURED=true" >> "$CONFIG_ENV"
        else
            warn "GitHub MCP registration failed. Register manually with:"
            warn "  claude mcp add-json github '{\"command\":\"npx\",...}'"
        fi
    else
        warn "No GITHUB_TOKEN set — skipping GitHub MCP registration"
        warn "Set GITHUB_TOKEN and re-run, or register manually with: claude mcp add-json github ..."
    fi
else
    warn "Claude Code not found. Configure GitHub MCP manually after install."
fi

#===============================================================================
# Generate service files from templates
#===============================================================================

step "Generating systemd service files from templates..."

TEMPLATES_DIR="$INSTALL_DIR/services/templates"
SERVICES_DIR="$INSTALL_DIR/services"

if [ -d "$TEMPLATES_DIR" ]; then
    for tmpl in "$TEMPLATES_DIR"/*.service; do
        [ -f "$tmpl" ] || continue
        svc_name="$(basename "$tmpl")"
        generate_from_template "$tmpl" "$SERVICES_DIR/$svc_name"
    done
else
    # Fallback: use pre-generated service files already in services/
    warn "Service templates directory not found at $TEMPLATES_DIR"
    warn "Using pre-generated service files from $SERVICES_DIR"
fi

#===============================================================================
# Install services
#===============================================================================

step "Installing systemd services..."

sudo cp "$INSTALL_DIR/services/lobster-router.service" /etc/systemd/system/ 2>/dev/null \
    || warn "Could not install lobster-router.service"
sudo cp "$INSTALL_DIR/services/lobster-claude.service" /etc/systemd/system/ 2>/dev/null \
    || warn "Could not install lobster-claude.service"

# Optional services
for optional_svc in lobster-slack-router lobster-mcp lobster-observability; do
    svc_file="$INSTALL_DIR/services/${optional_svc}.service"
    if [ -f "$svc_file" ]; then
        sudo cp "$svc_file" /etc/systemd/system/
        info "${optional_svc} service installed (enable manually with: sudo systemctl enable $optional_svc)"
    fi
done

# Reload systemd if available (not available inside Docker containers without systemd)
if [ -d /run/systemd/system ]; then
    sudo systemctl daemon-reload 2>/dev/null || warn "systemctl daemon-reload failed (expected in Docker -- services are installed but not activated)"
else
    warn "systemd not running -- service files installed but not activated (expected in Docker/containers)"
fi

success "Services installed"

#===============================================================================
# Pre-seed ~/.claude.json to skip first-launch TUI
#
# Claude Code v2.1.45+ shows an interactive TUI on first launch (theme picker
# + security notice) that blocks forever on headless instances.
#===============================================================================

step "Pre-seeding ~/.claude.json to skip first-launch TUI..."

CLAUDE_JSON="$HOME/.claude.json"
CLAUDE_VERSION=$(claude --version 2>/dev/null | head -1 | grep -oP '^[\d.]+' || echo "2.1.45")

if [ -f "$CLAUDE_JSON" ] && grep -q '"hasCompletedOnboarding": true' "$CLAUDE_JSON"; then
    info "~/.claude.json already has hasCompletedOnboarding: true — skipping"
else
    cat > "$CLAUDE_JSON" << CLAUDEJSON
{
  "numStartups": 1,
  "installMethod": "native",
  "hasCompletedOnboarding": true,
  "lastOnboardingVersion": "$CLAUDE_VERSION",
  "hasSeenTasksHint": true
}
CLAUDEJSON
    success "~/.claude.json pre-seeded (version $CLAUDE_VERSION) — first-launch TUI will be skipped"
fi

#===============================================================================
# Register MCP server (lobster-inbox)
#===============================================================================

step "Registering MCP server with Claude..."

PYTHON_PATH="$INSTALL_DIR/.venv/bin/python"
claude mcp remove lobster-inbox 2>/dev/null || true

if claude mcp add lobster-inbox -s user -- "$PYTHON_PATH" "$INSTALL_DIR/src/mcp/inbox_server.py" 2>/dev/null; then
    success "MCP server registered"
else
    warn "MCP server registration may have failed. Check with: claude mcp list"
fi

#===============================================================================
# CLI
#===============================================================================

step "Installing lobster CLI..."

sudo rm -f /usr/local/bin/lobster
sudo cp "$INSTALL_DIR/src/cli" /usr/local/bin/lobster
sudo chmod +x /usr/local/bin/lobster

success "CLI installed"

#===============================================================================
# Claude Code Discovery Symlinks
#
# Claude Code (CC) discovers files relative to its CWD. Since CC runs with
# CWD=$WORKSPACE_DIR, we create symlinks there pointing into the repo so CC
# finds the real CLAUDE.md and agent definitions without moving the workspace
# or requiring a migration.
#
# Discovery paths CC reads from CWD:
#   CLAUDE.md          - system prompt (also traverses parent dirs up to $HOME)
#   .claude/agents/    - subagent definitions (CWD-based only, no traversal)
#   .claude/settings.json - per-project CC settings (if present in CWD)
#
# The symlinks are idempotent: safe to run on fresh installs and upgrades.
#===============================================================================

step "Setting up Claude Code discovery symlinks..."

# CLAUDE.md
CLAUDE_MD_LINK="$WORKSPACE_DIR/CLAUDE.md"
CLAUDE_MD_TARGET="$INSTALL_DIR/CLAUDE.md"
[ -L "$CLAUDE_MD_LINK" ] && rm "$CLAUDE_MD_LINK"
ln -sf "$CLAUDE_MD_TARGET" "$CLAUDE_MD_LINK"
success "CLAUDE.md symlink: $CLAUDE_MD_LINK -> $CLAUDE_MD_TARGET"

# .claude/
WORKSPACE_CLAUDE="$WORKSPACE_DIR/.claude"
REPO_CLAUDE="$INSTALL_DIR/.claude"
mkdir -p "$REPO_CLAUDE/agents"

if [ -L "$WORKSPACE_CLAUDE" ]; then
    rm "$WORKSPACE_CLAUDE"
elif [ -d "$WORKSPACE_CLAUDE" ]; then
    if [ -n "$(ls -A "$WORKSPACE_CLAUDE" 2>/dev/null)" ]; then
        warn "Existing $WORKSPACE_CLAUDE has content — merging agents..."
        if [ -d "$WORKSPACE_CLAUDE/agents" ]; then
            for agent in "$WORKSPACE_CLAUDE/agents"/*.md; do
                [ -f "$agent" ] || continue
                cp -n "$agent" "$REPO_CLAUDE/agents/" 2>/dev/null || true
            done
        fi
    fi
    rm -rf "$WORKSPACE_CLAUDE"
fi
ln -sf "$REPO_CLAUDE" "$WORKSPACE_CLAUDE"
success ".claude symlink: $WORKSPACE_CLAUDE -> $REPO_CLAUDE"

success "Claude Code discovery symlinks configured"

#===============================================================================
# Private config overlay
#===============================================================================

apply_private_overlay

#===============================================================================
# Post-install hook
#===============================================================================

run_hook "post-install.sh"

#===============================================================================
# Start services
#===============================================================================

step "Starting services..."

if [ -f "$CONFIG_DIR/config.env" ]; then
    set -o allexport
    # shellcheck source=/dev/null
    source "$CONFIG_DIR/config.env"
    set +o allexport
fi

if [ -d /run/systemd/system ] && command -v systemctl &>/dev/null; then
    if [ "$NON_INTERACTIVE" = true ]; then
        sudo systemctl enable lobster-router lobster-claude 2>/dev/null || true
        sudo systemctl start lobster-router 2>/dev/null || true
        sleep 2
        sudo systemctl start lobster-claude 2>/dev/null || true
        sleep 3

        echo ""
        if systemctl is-active --quiet lobster-router 2>/dev/null; then
            success "Telegram bot: running"
        else
            warn "Telegram bot: check with 'lobster status'"
        fi
        if systemctl is-active --quiet lobster-claude 2>/dev/null; then
            success "Claude Code: running"
        else
            warn "Claude Code: check with 'lobster status'"
        fi

        # Start observability if service file installed
        if [ -f "/etc/systemd/system/lobster-observability.service" ]; then
            sudo systemctl enable lobster-observability 2>/dev/null || true
            sudo systemctl start lobster-observability 2>/dev/null || true
            sleep 1
            if systemctl is-active --quiet lobster-observability 2>/dev/null; then
                success "Dashboard server running on port 9100"
            else
                warn "Dashboard server: failed to start (check $WORKSPACE_DIR/logs/dashboard-server.log)"
            fi
        fi
    else
        echo ""
        read -rp "Start Lobster services now? [Y/n]: " REPLY
        if [[ ! $REPLY =~ ^[Nn]$ ]]; then
            sudo systemctl enable lobster-router lobster-claude
            sudo systemctl start lobster-router
            sleep 2
            sudo systemctl start lobster-claude

            sleep 3

            echo ""
            if systemctl is-active --quiet lobster-router; then
                success "Telegram bot: running"
            else
                warn "Telegram bot: not running. Check: sudo journalctl -u lobster-router"
            fi
            if systemctl is-active --quiet lobster-claude; then
                success "Claude Code: running"
            else
                warn "Claude Code: not running. Check: sudo journalctl -u lobster-claude"
            fi

            if [ -f "/etc/systemd/system/lobster-observability.service" ]; then
                sudo systemctl enable lobster-observability 2>/dev/null || true
                sudo systemctl start lobster-observability 2>/dev/null || true
                sleep 1
                if systemctl is-active --quiet lobster-observability 2>/dev/null; then
                    success "Dashboard server running on port 9100"
                else
                    warn "Dashboard server: failed to start (check $WORKSPACE_DIR/logs/dashboard-server.log)"
                fi
            fi
        fi
    fi
else
    info "Services not started. Start manually with: lobster start"
fi

#===============================================================================
# Done
#===============================================================================

echo ""
echo -e "${GREEN}"
cat << 'DONE'
╔═══════════════════════════════════════════════════════════════╗
║                                                               ║
║              LOBSTER INSTALLATION COMPLETE!                  ║
║                                                               ║
╚═══════════════════════════════════════════════════════════════╝
DONE
echo -e "${NC}"

echo "Test it by sending a message to your Telegram bot!"
echo ""
echo -e "${BOLD}Commands:${NC}"
echo "  lobster status    Check service status"
echo "  lobster logs      View logs"
echo "  lobster inbox     Check pending messages"
echo "  lobster start     Start all services"
echo "  lobster stop      Stop all services"
echo "  lobster env list  List stored API tokens"
echo "  lobster help      Show all commands"
echo ""
echo -e "${BOLD}Directories:${NC}"
echo "  $INSTALL_DIR        Lobster code"
echo "  $CONFIG_DIR          Configuration"
echo "  $CONFIG_DIR/global.env  Global API token store"
echo "  $WORKSPACE_DIR      Claude workspace"
echo "  $PROJECTS_DIR  Projects"
echo "  $MESSAGES_DIR       Message queues"
echo ""
if [ "$INSTALL_MODE" = "tarball" ]; then
    echo -e "${BOLD}Install mode:${NC} tarball (upgrade with: lobster upgrade)"
else
    echo -e "${BOLD}Install mode:${NC} git (upgrade with: git pull or lobster upgrade)"
fi
if [ "$NON_INTERACTIVE" = true ]; then
    echo ""
    echo -e "${YELLOW}Installed in non-interactive mode.${NC}"
    echo "Some steps were skipped. To complete setup, run the installer interactively:"
    echo "  bash $INSTALL_DIR/install.sh"
fi
echo ""
