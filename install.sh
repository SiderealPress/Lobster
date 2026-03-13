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
    PKG_MANAGER="apt"
    success "Ubuntu/Debian system detected"
elif command -v dnf &>/dev/null; then
    PKG_MANAGER="dnf"
    success "Amazon Linux/Fedora system detected"
else
    error "Unsupported Linux distribution. This installer requires apt-get or dnf."
    exit 1
fi

info "Detected package manager: $PKG_MANAGER"

# Check sudo access
if ! sudo -n true 2>/dev/null && ! sudo true; then
    error "This installer requires sudo access."
    exit 1
fi
success "Sudo access confirmed"

# Check internet connectivity
if ! curl -s --connect-timeout 5 https://github.com > /dev/null; then
    error "No internet connectivity. This installer requires internet access."
    exit 1
fi
success "Internet connectivity confirmed"

# Check for required tools (warn if missing, will be installed)
if ! command -v python3 &>/dev/null; then
    warn "Python3 not found. Will install."
fi

if ! command -v claude &>/dev/null; then
    warn "Claude Code not found. Will install."
fi

#===============================================================================
# Install System Dependencies
#===============================================================================

step "Installing system dependencies..."

if [ "$PKG_MANAGER" = "apt" ]; then
    # Add GitHub CLI repository
    info "Adding GitHub CLI apt repository..."
    if ! sudo apt-get install -y -qq gh 2>/dev/null; then
        # Add the official GitHub CLI repository
        sudo mkdir -p /etc/apt/keyrings
        curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | sudo dd of=/etc/apt/keyrings/githubcli-archive-keyring.gpg 2>/dev/null
        echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | sudo tee /etc/apt/sources.list.d/github-cli.list > /dev/null
        sudo apt-get update -qq
    fi
else
    # DNF (Amazon Linux / Fedora) - add GitHub CLI repository
    info "Adding GitHub CLI dnf repository..."
    sudo dnf install -y 'dnf-command(config-manager)' 2>/dev/null || true
    sudo dnf config-manager --add-repo https://cli.github.com/packages/rpm/gh-cli.repo 2>/dev/null || true
fi

# Core packages
DEBIAN_PKGS=(
    wget git jq gh python3 python3-pip python3-venv
    cron at expect tmux cmake
    ffmpeg ripgrep fd-find bat fzf mosh
)

DNF_PKGS=(
    wget git jq gh python3 python3-pip
    cronie at expect tmux cmake
    ffmpeg ripgrep fd-find bat fzf mosh
)

if [ "$PKG_MANAGER" = "apt" ]; then
    for pkg in "${DEBIAN_PKGS[@]}"; do
        info "Installing $pkg..."
        sudo apt-get install -y -qq "$pkg" 2>/dev/null || warn "Could not install $pkg"
    done
else
    for pkg in "${DNF_PKGS[@]}"; do
        info "Installing $pkg..."
        sudo dnf install -y "$pkg" 2>/dev/null || warn "Could not install $pkg"
    done
fi

success "Core system dependencies installed"

#===============================================================================
# Install Claude Code
#===============================================================================

step "Installing Claude Code..."

# Use the official installer
curl -fsSL https://claude.ai/install.sh | sh

# Ensure claude is in PATH for the rest of this script
export PATH="$HOME/.local/bin:$PATH"

info "Added ~/.local/bin to PATH in $HOME/.bashrc"
success "Claude Code installed"

#===============================================================================
# Check for existing Claude authentication
#===============================================================================

step "Checking existing Claude Code authentication..."

CLAUDE_CREDENTIALS="$HOME/.claude/.credentials.json"
if [ -f "$CLAUDE_CREDENTIALS" ]; then
    info "Credentials found, verifying token is still valid..."
    # Quick check: try to parse the credentials file
    if python3 -c "
import json, time
with open('$CLAUDE_CREDENTIALS') as f:
    creds = json.load(f)
expires = creds.get('expiresAt', 0)
# expiresAt is in milliseconds
if expires and expires / 1000 < time.time():
    raise SystemExit('expired')
" 2>/dev/null; then
        success "Existing Claude credentials are valid"
    else
        warn "OAuth credentials exist but token is expired or invalid."
        warn "You'll need to re-authenticate during the auth setup step."
    fi
fi

#===============================================================================
# Set up repository
#===============================================================================

# Determine install mode
INSTALL_MODE="git"

if [ -d "$INSTALL_DIR/.git" ]; then
    info "Existing git install detected"
elif [ -d "$SCRIPT_DIR/.git" ]; then
    # Running from within the repo (dev mode or bind-mount)
    info "Running from git repo at $SCRIPT_DIR"
    INSTALL_DIR="$SCRIPT_DIR"
fi

step "Setting up Lobster repository..."

if [ -d "$INSTALL_DIR/.git" ]; then
    info "Repository exists. Updating..."
    cd "$INSTALL_DIR"
    # Only pull if there are no local uncommitted changes
    if git diff --quiet && git diff --cached --quiet; then
        git fetch origin 2>/dev/null || warn "Could not fetch from remote (offline?)"
        git pull --ff-only 2>/dev/null || warn "Could not fast-forward (diverged history or offline)"
    else
        warn "Local changes detected — skipping git pull to preserve them"
    fi
    CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "unknown")
    success "Repository ready at $INSTALL_DIR (branch: $CURRENT_BRANCH)"
else
    info "Cloning repository to $INSTALL_DIR..."
    git clone --branch "$REPO_BRANCH" "$REPO_URL" "$INSTALL_DIR"
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
    "$WORKSPACE_DIR" \
    "$WORKSPACE_DIR/logs" \
    "$WORKSPACE_DIR/data" \
    "$WORKSPACE_DIR/memory/canonical" \
    "$WORKSPACE_DIR/memory/archive/digests" \
    "$MESSAGES_DIR/inbox" \
    "$MESSAGES_DIR/outbox" \
    "$MESSAGES_DIR/processing" \
    "$MESSAGES_DIR/processed" \
    "$MESSAGES_DIR/failed" \
    "$MESSAGES_DIR/audio" \
    "$MESSAGES_DIR/task-outputs" \
    "$MESSAGES_DIR/config"

# Create projects directory
mkdir -p "$PROJECTS_DIR"
info "  $PROJECTS_DIR - All Lobster-managed projects"

# Seed canonical memory templates
TEMPLATES_DIR="$INSTALL_DIR/memory/canonical-templates"
CANONICAL_DIR="$WORKSPACE_DIR/memory/canonical"

if [ -d "$TEMPLATES_DIR" ]; then
    for template in "$TEMPLATES_DIR"/*.md; do
        [ -f "$template" ] || continue
        basename_template="$(basename "$template")"
        dest="$CANONICAL_DIR/$basename_template"
        if [ ! -f "$dest" ]; then
            cp "$template" "$dest"
            info "  Seeded canonical template: $basename_template"
        fi
    done
fi

success "Directories created"

#===============================================================================
# Global environment store
#===============================================================================

step "Setting up global environment store..."

mkdir -p "$CONFIG_DIR"

GLOBAL_ENV="$CONFIG_DIR/global.env"
if [ ! -f "$GLOBAL_ENV" ]; then
    cat > "$GLOBAL_ENV" << 'GLOBALENV'
# Lobster Global Environment Store
# This file stores API tokens and other credentials.
# Add entries with: lobster env set KEY VALUE
# Read entries with: lobster env list
#
# Format: KEY=VALUE (one per line)
# Lines starting with # are comments.
GLOBALENV
fi

success "Global env store created: $GLOBAL_ENV"

# Add shell integration to load global.env on login
SHELL_INTEGRATION='
# Lobster global env store
if [ -f "$HOME/lobster-config/global.env" ]; then
    set -o allexport
    # shellcheck source=/dev/null
    source "$HOME/lobster-config/global.env"
    set +o allexport
fi'

for shell_rc in "$HOME/.bashrc" "$HOME/.profile"; do
    if [ -f "$shell_rc" ] && ! grep -q "lobster-config/global.env" "$shell_rc"; then
        echo "$SHELL_INTEGRATION" >> "$shell_rc"
        info "  Shell integration added to $shell_rc"
    fi
done

success "Global env store configured"
info "  File: $GLOBAL_ENV"
info "  Use 'lobster env set KEY VALUE' to store API tokens"
info "  Use 'lobster env list' to see stored keys"
info "  See docs/GLOBAL-ENV.md for full documentation"

#===============================================================================
# Scheduled Tasks Infrastructure
#===============================================================================

step "Setting up scheduled tasks infrastructure..."

# Make scripts executable
chmod +x "$INSTALL_DIR/scheduled-tasks/run-job.sh" 2>/dev/null || true
chmod +x "$INSTALL_DIR/scheduled-tasks/sync-crontab.sh" 2>/dev/null || true

# Create scheduled-jobs workspace
mkdir -p "$WORKSPACE_DIR/scheduled-jobs/"{tasks,logs}

JOBS_FILE="$WORKSPACE_DIR/scheduled-jobs/jobs.json"
if [ ! -f "$JOBS_FILE" ]; then
    echo '{"jobs": {}}' > "$JOBS_FILE"
fi

# Symlink scheduled-tasks tasks into workspace so MCP server finds them
TASKS_SRC="$INSTALL_DIR/scheduled-tasks/tasks"
TASKS_DST="$WORKSPACE_DIR/scheduled-jobs/tasks"

if [ -d "$TASKS_SRC" ] && [ ! -L "$TASKS_DST" ]; then
    # Copy any seed tasks
    for task_file in "$TASKS_SRC"/*.md; do
        [ -f "$task_file" ] || continue
        dst="$TASKS_DST/$(basename "$task_file")"
        if [ ! -f "$dst" ]; then
            cp "$task_file" "$dst"
        fi
    done
fi

# Generate sync-crontab.sh from template if it has placeholders
if grep -q '{{INSTALL_DIR}}' "$INSTALL_DIR/scheduled-tasks/sync-crontab.sh" 2>/dev/null; then
    # Replace placeholders in-place
    sed -i "s|{{INSTALL_DIR}}|$INSTALL_DIR|g" "$INSTALL_DIR/scheduled-tasks/sync-crontab.sh"
    sed -i "s|{{WORKSPACE_DIR}}|$WORKSPACE_DIR|g" "$INSTALL_DIR/scheduled-tasks/sync-crontab.sh"
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
# Health Check Setup
#===============================================================================

step "Setting up health monitoring..."

# Make scripts executable
chmod +x "$INSTALL_DIR/scripts/health-check-v3.sh" || true
chmod +x "$INSTALL_DIR/scripts/self-check-reminder.sh" || true

# Add health check to crontab (runs every 2 minutes)
HEALTH_MARKER="# LOBSTER-HEALTH"
({ crontab -l 2>/dev/null | grep -v "$HEALTH_MARKER" | grep -v "health-check" || true; }; \
 echo "*/2 * * * * $INSTALL_DIR/scripts/health-check-v3.sh >> $WORKSPACE_DIR/logs/health-check.log 2>&1 $HEALTH_MARKER") \
 | crontab -

success "Health monitoring configured (checks every 2 minutes)"

#===============================================================================
# Daily Dependency Health Check
#===============================================================================

step "Setting up daily dependency health check..."

DEPCHECK_MARKER="# LOBSTER-DEPCHECK"
({ crontab -l 2>/dev/null | grep -v "$DEPCHECK_MARKER" | grep -v "dependency-health-check" || true; }; \
 echo "0 6 * * * $INSTALL_DIR/scripts/dependency-health-check.sh >> $WORKSPACE_DIR/logs/dependency-health.log 2>&1 $DEPCHECK_MARKER") \
 | crontab -

success "Daily dependency health check configured (runs at 06:00 daily)"

#===============================================================================
# Nightly Consolidation
#===============================================================================

step "Setting up nightly consolidation..."

CONSOLIDATION_MARKER="# LOBSTER-CONSOLIDATION"
({ crontab -l 2>/dev/null | grep -v "$CONSOLIDATION_MARKER" | grep -v "nightly-consolidation" || true; }; \
 echo "0 3 * * * $INSTALL_DIR/scripts/nightly-consolidation.sh >> $WORKSPACE_DIR/logs/nightly-consolidation.log 2>&1 $CONSOLIDATION_MARKER") \
 | crontab -

success "Nightly consolidation configured (runs at 03:00 nightly)"

#===============================================================================
# Claude Code Settings (hooks, self-check)
#===============================================================================

step "Setting up self-check reminder system..."

# Ensure Claude Code settings directory exists
mkdir -p "$HOME/.claude"

CLAUDE_SETTINGS="$HOME/.claude/settings.json"

# Create or update Claude Code settings with hooks
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

# Self-check cron (every 3 minutes, checks if a self-check was scheduled)
SELFCHECK_MARKER="# LOBSTER-SELFCHECK"
({ crontab -l 2>/dev/null | grep -v "$SELFCHECK_MARKER" | grep -v "self-check-reminder" || true; }; \
 echo "*/3 * * * * $INSTALL_DIR/scripts/self-check-runner.sh >> $WORKSPACE_DIR/logs/self-check.log 2>&1 $SELFCHECK_MARKER") \
 | crontab - 2>/dev/null || true

success "Self-check cron configured (every 3min)"

# Add no-auto-memory hook if not present
HOOK_SCRIPT="$INSTALL_DIR/hooks/no-auto-memory.py"
if [ -f "$HOOK_SCRIPT" ]; then
    if ! python3 -c "
import json
with open('$CLAUDE_SETTINGS') as f:
    s = json.load(f)
hooks = s.get('hooks', {})
pre = hooks.get('PreToolUse', [])
exists = any(h.get('command', '').endswith('no-auto-memory.py') for h in pre if isinstance(h, dict))
exit(0 if exists else 1)
" 2>/dev/null; then
        python3 << ADDNOMEM
import json
with open('$CLAUDE_SETTINGS') as f:
    s = json.load(f)
s.setdefault('hooks', {}).setdefault('PreToolUse', []).append({
    "matcher": ".*",
    "hooks": [{"type": "command", "command": "python3 $HOOK_SCRIPT"}]
})
with open('$CLAUDE_SETTINGS', 'w') as f:
    json.dump(s, f, indent=2)
ADDNOMEM
        success "No-auto-memory hook added"
    else
        info "No-auto-memory hook already configured in Claude Code settings"
    fi
fi

# Add link-enforcement hook if not present
LINK_HOOK="$INSTALL_DIR/hooks/enforce-link-archiving.py"
if [ -f "$LINK_HOOK" ]; then
    if ! python3 -c "
import json
with open('$CLAUDE_SETTINGS') as f:
    s = json.load(f)
hooks = s.get('hooks', {})
post = hooks.get('PostToolUse', [])
exists = any(h.get('command', '').endswith('enforce-link-archiving.py') for h in post if isinstance(h, dict))
exit(0 if exists else 1)
" 2>/dev/null; then
        python3 << ADDLINK
import json
with open('$CLAUDE_SETTINGS') as f:
    s = json.load(f)
s.setdefault('hooks', {}).setdefault('PostToolUse', []).append({
    "matcher": ".*",
    "hooks": [{"type": "command", "command": "python3 $LINK_HOOK"}]
})
with open('$CLAUDE_SETTINGS', 'w') as f:
    json.dump(s, f, indent=2)
ADDLINK
        success "Link enforcement hook installed"
    else
        info "Link enforcement hook already configured"
    fi
fi

# Add require-subagent-type hook if not present
SUBAGENT_HOOK="$INSTALL_DIR/hooks/require-subagent-type.py"
if [ -f "$SUBAGENT_HOOK" ]; then
    if ! python3 -c "
import json
with open('$CLAUDE_SETTINGS') as f:
    s = json.load(f)
hooks = s.get('hooks', {})
pre = hooks.get('PreToolUse', [])
exists = any(h.get('command', '').endswith('require-subagent-type.py') for h in pre if isinstance(h, dict))
exit(0 if exists else 1)
" 2>/dev/null; then
        python3 << ADDSUBAGENT
import json
with open('$CLAUDE_SETTINGS') as f:
    s = json.load(f)
s.setdefault('hooks', {}).setdefault('PreToolUse', []).append({
    "matcher": "Task",
    "hooks": [{"type": "command", "command": "python3 $SUBAGENT_HOOK"}]
})
with open('$CLAUDE_SETTINGS', 'w') as f:
    json.dump(s, f, indent=2)
ADDSUBAGENT
        success "require-subagent-type hook installed"
    else
        info "require-subagent-type hook already configured in Claude Code settings"
    fi
fi

# Add restore-exec-bit hook if not present
RESTORE_HOOK="$INSTALL_DIR/hooks/restore-exec-bit.py"
if [ -f "$RESTORE_HOOK" ]; then
    if ! python3 -c "
import json
with open('$CLAUDE_SETTINGS') as f:
    s = json.load(f)
hooks = s.get('hooks', {})
post = hooks.get('PostToolUse', [])
exists = any(h.get('command', '').endswith('restore-exec-bit.py') for h in post if isinstance(h, dict))
exit(0 if exists else 1)
" 2>/dev/null; then
        python3 << ADDRESTORE
import json
with open('$CLAUDE_SETTINGS') as f:
    s = json.load(f)
s.setdefault('hooks', {}).setdefault('PostToolUse', []).append({
    "matcher": ".*",
    "hooks": [{"type": "command", "command": "python3 $RESTORE_HOOK"}]
})
with open('$CLAUDE_SETTINGS', 'w') as f:
    json.dump(s, f, indent=2)
ADDRESTORE
        success "restore-exec-bit hook installed"
    else
        info "restore-exec-bit hook already configured in Claude Code settings"
    fi
fi

#===============================================================================
# Python Environment
#===============================================================================

step "Setting up Python environment..."

# Install uv
info "Installing uv (Python package manager)..."
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
success "uv installed"

# Create venv
UV="$HOME/.local/bin/uv"
VENV_DIR="$INSTALL_DIR/.venv"

if [ -d "$VENV_DIR" ]; then
    success "Python venv already exists"
else
    info "Creating Python virtual environment..."
    "$UV" venv "$VENV_DIR"
fi

# Install core packages
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

# Install fastembed for vector memory
info "Installing fastembed..."
"$UV" pip install --python "$VENV_DIR/bin/python" fastembed 2>/dev/null && \
    success "fastembed installed" || \
    warn "fastembed install failed (vector memory won't work)"

# Install sqlite-vec for vector storage
info "Installing sqlite-vec..."
"$UV" pip install --python "$VENV_DIR/bin/python" sqlite-vec 2>/dev/null
# Verify it loads correctly
if "$VENV_DIR/bin/python" -c "import sqlite_vec; import sqlite3; db = sqlite3.connect(':memory:'); db.enable_load_extension(True); sqlite_vec.load(db)" 2>/dev/null; then
    success "sqlite-vec installed and loads correctly"
else
    warn "sqlite-vec installed but may not load (architecture mismatch or missing deps)"
fi

success "Python environment ready"

#===============================================================================
# Configure Lobster
#===============================================================================

step "Configuring Lobster..."

# Create config directory and file
mkdir -p "$CONFIG_DIR"
CONFIG_ENV="$CONFIG_DIR/config.env"

if [ "$NON_INTERACTIVE" = true ]; then
    warn "Skipping Telegram configuration (non-interactive mode)."
    info "Run the installer again without --non-interactive to configure Telegram."

    # Write minimal config from environment variables if provided
    if [ ! -f "$CONFIG_ENV" ]; then
        cat > "$CONFIG_ENV" << CONFIGENV
# Lobster Configuration
# Generated by install.sh in non-interactive mode
TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN:-}
TELEGRAM_ALLOWED_USERS=${TELEGRAM_ALLOWED_USERS:-}
ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY:-}
CONFIGENV
    fi
else
    # Interactive configuration
    echo ""
    echo "Let's configure Lobster. You'll need:"
    echo "  1. A Telegram bot token (from @BotFather)"
    echo "  2. Your Telegram user ID"
    echo "  3. An Anthropic API key"
    echo ""

    if [ -f "$CONFIG_ENV" ]; then
        # shellcheck source=/dev/null
        source "$CONFIG_ENV" 2>/dev/null || true
    fi

    # Telegram bot token
    if [ -z "${TELEGRAM_BOT_TOKEN:-}" ]; then
        read -rp "Telegram bot token: " TELEGRAM_BOT_TOKEN
    else
        read -rp "Telegram bot token [${TELEGRAM_BOT_TOKEN:0:10}...]: " new_token
        TELEGRAM_BOT_TOKEN="${new_token:-$TELEGRAM_BOT_TOKEN}"
    fi

    # Telegram allowed users
    if [ -z "${TELEGRAM_ALLOWED_USERS:-}" ]; then
        read -rp "Your Telegram user ID(s) (comma-separated): " TELEGRAM_ALLOWED_USERS
    else
        read -rp "Telegram user ID(s) [$TELEGRAM_ALLOWED_USERS]: " new_users
        TELEGRAM_ALLOWED_USERS="${new_users:-$TELEGRAM_ALLOWED_USERS}"
    fi

    # Anthropic API key
    if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
        read -rp "Anthropic API key: " ANTHROPIC_API_KEY
    else
        read -rp "Anthropic API key [${ANTHROPIC_API_KEY:0:10}...]: " new_key
        ANTHROPIC_API_KEY="${new_key:-$ANTHROPIC_API_KEY}"
    fi

    # Write config
    cat > "$CONFIG_ENV" << CONFIGENV
# Lobster Configuration
TELEGRAM_BOT_TOKEN=$TELEGRAM_BOT_TOKEN
TELEGRAM_ALLOWED_USERS=$TELEGRAM_ALLOWED_USERS
ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY
CONFIGENV
    success "Configuration saved to $CONFIG_ENV"
fi

#===============================================================================
# GitHub Integration (Optional)
#===============================================================================

step "GitHub Integration (Optional)..."

if [ "$NON_INTERACTIVE" = true ]; then
    info "Skipping GitHub integration (non-interactive mode)."
    # Set up GitHub token from environment if provided
    if [ -n "${GITHUB_TOKEN:-}" ]; then
        if ! grep -q "GITHUB_TOKEN" "$CONFIG_ENV" 2>/dev/null; then
            echo "GITHUB_TOKEN=$GITHUB_TOKEN" >> "$CONFIG_ENV"
        fi
        if ! grep -q "GH_TOKEN" "$CONFIG_ENV" 2>/dev/null; then
            echo "GH_TOKEN=$GITHUB_TOKEN" >> "$CONFIG_ENV"
        fi
    fi
else
    echo ""
    echo "GitHub integration enables PR reviews, issue management, and code browsing."
    read -rp "Do you have a GitHub personal access token? [y/N]: " has_github

    if [[ $has_github =~ ^[Yy]$ ]]; then
        if [ -z "${GITHUB_TOKEN:-}" ]; then
            read -rp "GitHub personal access token: " GITHUB_TOKEN
        else
            read -rp "GitHub personal access token [${GITHUB_TOKEN:0:10}...]: " new_token
            GITHUB_TOKEN="${new_token:-$GITHUB_TOKEN}"
        fi

        if ! grep -q "GITHUB_TOKEN" "$CONFIG_ENV"; then
            echo "GITHUB_TOKEN=$GITHUB_TOKEN" >> "$CONFIG_ENV"
            echo "GH_TOKEN=$GITHUB_TOKEN" >> "$CONFIG_ENV"
        fi
        success "GitHub token saved"
    fi
fi

#===============================================================================
# GitHub CLI Authentication
#===============================================================================

step "Checking GitHub CLI authentication..."

if [ -n "${GITHUB_TOKEN:-}" ]; then
    echo "$GITHUB_TOKEN" | gh auth login --with-token 2>/dev/null && \
        success "GitHub CLI authenticated" || \
        warn "GitHub CLI auth failed — authenticate later with: gh auth login"
else
    info "Skipped. Authenticate later with: gh auth login"
fi

#===============================================================================
# Voice Transcription Setup (whisper.cpp)
#===============================================================================

step "Voice Transcription Setup (whisper.cpp)..."

if ! command -v ffmpeg &>/dev/null; then
    warn "ffmpeg not found. Voice transcription requires ffmpeg."
else
    success "ffmpeg is available"
fi

WHISPER_DIR="$WORKSPACE_DIR/whisper.cpp"

if [ -d "$WHISPER_DIR/build/bin" ] && [ -x "$WHISPER_DIR/build/bin/whisper-cli" ]; then
    success "whisper.cpp already built"
else
    step "Building whisper.cpp (this may take a few minutes)..."
    mkdir -p "$WORKSPACE_DIR"
    cd "$WORKSPACE_DIR"

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

# Download whisper model
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

# Verify whisper-cli is accessible
WHISPER_CLI="$WHISPER_DIR/build/bin/whisper-cli"
if [ -x "$WHISPER_CLI" ]; then
    info "Verifying whisper.cpp transcription pipeline..."
    success "whisper-cli binary verified"
else
    warn "whisper-cli binary not found at $WHISPER_CLI"
fi

#===============================================================================
# Claude Authentication
#===============================================================================

step "Setting up Claude authentication..."

# Load config
if [ -f "$CONFIG_DIR/config.env" ]; then
    # shellcheck source=/dev/null
    set -o allexport
    source "$CONFIG_DIR/config.env"
    set +o allexport
fi

if [ -n "${ANTHROPIC_API_KEY:-}" ]; then
    # Save to global env store
    if ! grep -q "ANTHROPIC_API_KEY" "$CONFIG_DIR/global.env" 2>/dev/null; then
        echo "ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY" >> "$CONFIG_DIR/global.env"
    fi
    success "Using ANTHROPIC_API_KEY from environment"
elif [ -f "$CLAUDE_CREDENTIALS" ]; then
    success "Using existing Claude OAuth credentials"
else
    if [ "$NON_INTERACTIVE" = true ]; then
        warn "No ANTHROPIC_API_KEY set and no OAuth credentials found."
        warn "Claude Code authentication required. Set ANTHROPIC_API_KEY or run 'claude auth login' after install."
    else
        echo ""
        echo "Claude Code needs to be authenticated. You have two options:"
        echo "  1. Use an Anthropic API key (simpler for headless servers)"
        echo "  2. Use OAuth browser-based login"
        echo ""
        read -rp "Do you have an Anthropic API key? [y/N]: " has_api_key

        if [[ $has_api_key =~ ^[Yy]$ ]]; then
            read -rp "Anthropic API key: " ANTHROPIC_API_KEY
            echo "ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY" >> "$CONFIG_DIR/global.env"
            echo "ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY" >> "$CONFIG_DIR/config.env"
            success "API key saved"
        else
            info "Starting OAuth browser flow..."
            claude auth login
        fi
    fi
fi

# Generate launcher scripts
chmod +x "$INSTALL_DIR/scripts/start-claude.sh" 2>/dev/null || true
chmod +x "$INSTALL_DIR/scripts/claude-persistent.sh" 2>/dev/null || true
chmod +x "$INSTALL_DIR/scripts/claude-wrapper.exp" 2>/dev/null || true
success "Claude launchers ready (start-claude.sh, claude-persistent.sh, claude-wrapper.exp)"

#===============================================================================
# Generate service files from templates
#===============================================================================

step "Generating systemd service files from templates..."

TEMPLATES_DIR="$INSTALL_DIR/services/templates"
SERVICES_DIR="$INSTALL_DIR/services"

if [ -d "$TEMPLATES_DIR" ]; then
    for template in "$TEMPLATES_DIR"/*.service; do
        [ -f "$template" ] || continue
        service_name="$(basename "$template")"
        output="$SERVICES_DIR/$service_name"
        generate_from_template "$template" "$output"
    done
else
    warn "No service templates directory found at $TEMPLATES_DIR"
    warn "Service files may need to be generated manually"
fi

#===============================================================================
# Install Services
#===============================================================================

step "Installing systemd services..."

sudo cp "$INSTALL_DIR/services/lobster-router.service" /etc/systemd/system/
sudo cp "$INSTALL_DIR/services/lobster-claude.service" /etc/systemd/system/

# Install Slack router service if generated
if [ -f "$INSTALL_DIR/services/lobster-slack-router.service" ]; then
    sudo cp "$INSTALL_DIR/services/lobster-slack-router.service" /etc/systemd/system/
    info "Slack router service installed (enable manually with: sudo systemctl enable lobster-slack-router)"
fi

# Install MCP HTTP bridge service if generated
if [ -f "$INSTALL_DIR/services/lobster-mcp.service" ]; then
    sudo cp "$INSTALL_DIR/services/lobster-mcp.service" /etc/systemd/system/
    info "MCP HTTP bridge service installed (enable manually with: sudo systemctl enable lobster-mcp)"
fi

# Install observability service if generated
if [ -f "$INSTALL_DIR/services/lobster-observability.service" ]; then
    sudo cp "$INSTALL_DIR/services/lobster-observability.service" /etc/systemd/system/
    info "Observability server service installed (enable manually with: sudo systemctl enable lobster-observability)"
fi

# Reload systemd if available (not available inside Docker containers without systemd)
if [ -d /run/systemd/system ]; then
    sudo systemctl daemon-reload 2>/dev/null || warn "systemctl daemon-reload failed (expected in Docker — services are installed but not activated)"
else
    warn "systemd not running — service files installed but not activated (expected in Docker/containers)"
fi

success "Services installed"

#===============================================================================
# Pre-seed ~/.claude.json
#
# Claude Code v2.1.45+ shows an interactive TUI on first launch (theme picker
# + security notice) that blocks forever on headless instances. Setting
# hasCompletedOnboarding: true bypasses this entirely.
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
# Register MCP Server
#===============================================================================

step "Registering MCP server with Claude..."

# Remove existing registration if present
claude mcp remove lobster-inbox 2>/dev/null || true

# Add new registration
PYTHON_PATH="$INSTALL_DIR/.venv/bin/python"
if claude mcp add lobster-inbox -s user -- "$PYTHON_PATH" "$INSTALL_DIR/src/mcp/inbox_server.py" 2>/dev/null; then
    success "MCP server registered"
else
    warn "MCP server registration may have failed. Check with: claude mcp list"
fi

#===============================================================================
# Install CLI
#===============================================================================

step "Installing lobster CLI..."

# Remove any existing symlink or file
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

# CLAUDE.md symlink (workspace -> repo)
CLAUDE_MD_LINK="$WORKSPACE_DIR/CLAUDE.md"
CLAUDE_MD_TARGET="$INSTALL_DIR/CLAUDE.md"

if [ -L "$CLAUDE_MD_LINK" ]; then
    rm "$CLAUDE_MD_LINK"
fi
ln -s "$CLAUDE_MD_TARGET" "$CLAUDE_MD_LINK"
success "CLAUDE.md symlink: $CLAUDE_MD_LINK -> $CLAUDE_MD_TARGET"

# .claude/ directory symlink (workspace -> repo's .claude/)
WORKSPACE_CLAUDE_LINK="$WORKSPACE_DIR/.claude"
REPO_CLAUDE_DIR="$INSTALL_DIR/.claude"

mkdir -p "$REPO_CLAUDE_DIR/agents"

if [ -L "$WORKSPACE_CLAUDE_LINK" ]; then
    rm "$WORKSPACE_CLAUDE_LINK"
elif [ -d "$WORKSPACE_CLAUDE_LINK" ]; then
    # Existing directory — move any custom content and replace with symlink
    if [ "$(ls -A "$WORKSPACE_CLAUDE_LINK" 2>/dev/null)" ]; then
        warn "Existing $WORKSPACE_CLAUDE_LINK directory has content — merging agents..."
        if [ -d "$WORKSPACE_CLAUDE_LINK/agents" ]; then
            for agent in "$WORKSPACE_CLAUDE_LINK/agents"/*.md; do
                [ -f "$agent" ] || continue
                cp -n "$agent" "$REPO_CLAUDE_DIR/agents/" 2>/dev/null || true
            done
        fi
    fi
    rm -rf "$WORKSPACE_CLAUDE_LINK"
fi
ln -s "$REPO_CLAUDE_DIR" "$WORKSPACE_CLAUDE_LINK"
success ".claude symlink: $WORKSPACE_CLAUDE_LINK -> $REPO_CLAUDE_DIR"

#===============================================================================
# Apply private config overlay (if configured)
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

# Source the config so lobster start can read it
if [ -f "$CONFIG_DIR/config.env" ]; then
    # shellcheck source=/dev/null
    set -o allexport
    source "$CONFIG_DIR/config.env"
    set +o allexport
fi

if [ -d /run/systemd/system ] && command -v systemctl &>/dev/null; then
    # Systemd is available
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
    else
        echo ""
        read -rp "Start Lobster services now? [Y/n]: " start_services
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

            # Check dashboard if observability service exists
            if [ -f "/etc/systemd/system/lobster-observability.service" ]; then
                sudo systemctl enable lobster-observability 2>/dev/null || true
                sudo systemctl start lobster-observability 2>/dev/null || true
                sleep 1
                if systemctl is-active --quiet lobster-observability 2>/dev/null; then
                    DASHBOARD_PORT=$(grep "port" "$WORKSPACE_DIR/services/lobster-observability.service" 2>/dev/null | grep -oP '\d{4,5}' | head -1 || echo "9100")
                    success "Dashboard server: running on port $DASHBOARD_PORT"
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
