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
USER_CONFIG_DIR="${LOBSTER_USER_CONFIG:-$HOME/lobster-user-config}"

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
        -e "s|{{USER_CONFIG_DIR}}|${USER_CONFIG_DIR}|g" \
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

# Show loaded configuration info
if [ -n "$CONFIG_FILE" ] && [ -f "$CONFIG_FILE" ]; then
    info "Loaded configuration from: $CONFIG_FILE"
fi

#===============================================================================
# Pre-flight Checks
#===============================================================================

step "Running pre-flight checks..."

# Report detected package manager
info "Detected package manager: $PKG_MANAGER"
if [ "$PKG_MANAGER" = "apt" ]; then
    success "Ubuntu/Debian system detected"
else
    success "dnf-based system detected (Amazon Linux 2023 / Fedora)"
fi

# Smart root handling: create a lobster user and re-exec as them
if [ "$(id -u)" = "0" ]; then
    warn "Running as root — will create a 'lobster' user and re-exec as them."
    if ! id lobster &>/dev/null; then
        info "Creating 'lobster' user with passwordless sudo..."
        useradd -m -s /bin/bash lobster
        echo "lobster ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/lobster
        chmod 0440 /etc/sudoers.d/lobster
        # Copy SSH authorized_keys so user can SSH in directly next time
        if [ -f /root/.ssh/authorized_keys ]; then
            LOBSTER_HOME=$(getent passwd lobster | cut -d: -f6)
            mkdir -p "$LOBSTER_HOME/.ssh"
            cp /root/.ssh/authorized_keys "$LOBSTER_HOME/.ssh/authorized_keys"
            chown -R lobster:lobster "$LOBSTER_HOME/.ssh"
            chmod 700 "$LOBSTER_HOME/.ssh"
            chmod 600 "$LOBSTER_HOME/.ssh/authorized_keys"
        fi
        success "User 'lobster' created."
    else
        success "User 'lobster' already exists."
    fi
    echo ""
    info "Next time, SSH directly as: lobster@$(hostname)"
    echo ""
    exec sudo -u lobster bash "$0" "$@"
fi

# Check if running interactively
if [ ! -t 0 ] && [ "$NON_INTERACTIVE" = false ]; then
    error "This script requires interactive input."
    echo ""
    echo "Please run it like this instead:"
    echo -e "  ${CYAN}bash <(curl -fsSL https://raw.githubusercontent.com/SiderealPress/lobster/main/install.sh)${NC}"
    echo ""
    echo "Or download and run:"
    echo -e "  ${CYAN}curl -fsSL https://raw.githubusercontent.com/SiderealPress/lobster/main/install.sh -o install.sh${NC}"
    echo -e "  ${CYAN}bash install.sh${NC}"
    echo ""
    echo "Or for automated installs (skips interactive prompts):"
    echo -e "  ${CYAN}bash install.sh --non-interactive${NC}"
    exit 1
fi

# Check sudo
if ! sudo true 2>/dev/null; then
    error "This script requires sudo access"
    exit 1
fi
success "Sudo access confirmed"

# Check internet
if ! curl -s --connect-timeout 5 https://api.github.com >/dev/null; then
    error "No internet connection"
    exit 1
fi
success "Internet connectivity confirmed"

# Check Python
if ! command -v python3 &>/dev/null; then
    warn "Python3 not found. Will install."
    NEED_PYTHON=true
else
    PYTHON_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
    PYTHON_MAJOR=$(echo "$PYTHON_VERSION" | cut -d. -f1)
    PYTHON_MINOR=$(echo "$PYTHON_VERSION" | cut -d. -f2)
    if [ "$PYTHON_MAJOR" -lt 3 ] || { [ "$PYTHON_MAJOR" -eq 3 ] && [ "$PYTHON_MINOR" -lt 9 ]; }; then
        warn "Python $PYTHON_VERSION found, but 3.9+ recommended"
    else
        success "Python $PYTHON_VERSION found"
    fi
fi

# Check Claude Code
if command -v claude &>/dev/null; then
    success "Claude Code found"
    CLAUDE_INSTALLED=true
else
    warn "Claude Code not found. Will install."
    CLAUDE_INSTALLED=false
fi

#===============================================================================
# Install System Dependencies
#===============================================================================

step "Installing system dependencies..."

if [ "$PKG_MANAGER" = "apt" ]; then
    sudo apt-get update -qq

    # Install GitHub CLI (gh) repository if not already present
    if ! dpkg -s gh &>/dev/null; then
        info "Adding GitHub CLI apt repository..."
        curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | sudo dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg 2>/dev/null
        echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | sudo tee /etc/apt/sources.list.d/github-cli.list >/dev/null
        sudo apt-get update -qq
    fi

    PACKAGES=(
        curl
        wget
        git
        jq
        gh
        python3
        python3-pip
        python3-venv
        cron
        at
        expect
        bsdutils
        tmux
        build-essential
        cmake
        ffmpeg
        ripgrep
        fd-find
        bat
        fzf
        mosh
    )

    for pkg in "${PACKAGES[@]}"; do
        if ! dpkg -s "$pkg" &>/dev/null; then
            info "Installing $pkg..."
            sudo apt-get install -y -qq "$pkg"
        fi
    done
else
    # dnf (Amazon Linux 2023 / Fedora)
    DNF_PACKAGES=(
        curl
        wget
        git
        jq
        gh
        python3
        python3-pip
        cronie
        at
        expect
        tmux
        gcc-c++
        cmake
        make
        mosh
    )

    for pkg in "${DNF_PACKAGES[@]}"; do
        if ! rpm -q "$pkg" &>/dev/null; then
            info "Installing $pkg..."
            sudo dnf install -y "$pkg"
        fi
    done
fi

success "Core system dependencies installed"

#===============================================================================
# Install Modern CLI Tools (ripgrep, fd, bat, fzf) on dnf systems
#
# Ubuntu/Debian provides these in apt. On Amazon Linux 2023 / Fedora they are
# not in the default repos, so we download pre-built binaries from GitHub.
#===============================================================================

if [ "$PKG_MANAGER" = "dnf" ]; then
    step "Installing modern CLI tools from GitHub releases (dnf fallback)..."

    ARCH=$(uname -m)
    TOOLS_BIN_DIR="$HOME/.local/bin"
    mkdir -p "$TOOLS_BIN_DIR"

    # install_github_binary <owner/repo> <binary-name> <asset-grep-pattern>
    # Downloads the latest GitHub release asset whose URL matches <asset-grep-pattern>,
    # extracts the named binary, and places it in TOOLS_BIN_DIR.
    install_github_binary() {
        local repo="$1"
        local binary="$2"
        local asset_pattern="$3"

        if command -v "$binary" &>/dev/null; then
            success "$binary already installed"
            return 0
        fi

        info "Fetching latest $binary from github.com/$repo ..."
        local api_url="https://api.github.com/repos/${repo}/releases/latest"
        local asset_url
        asset_url=$(curl -fsSL "$api_url" | jq -r ".assets[].browser_download_url" | grep "$asset_pattern" | head -1)

        if [ -z "$asset_url" ]; then
            warn "Could not find $binary release asset matching '$asset_pattern'. Skipping."
            return 0
        fi

        local tmp_dir
        tmp_dir=$(mktemp -d)
        local archive="$tmp_dir/$(basename "$asset_url")"
        curl -fsSL "$asset_url" -o "$archive"

        if [[ "$archive" == *.tar.gz || "$archive" == *.tgz ]]; then
            tar -xzf "$archive" -C "$tmp_dir"
        elif [[ "$archive" == *.zip ]]; then
            unzip -q "$archive" -d "$tmp_dir"
        fi

        # Find the binary anywhere in the extracted tree
        local bin_path
        bin_path=$(find "$tmp_dir" -type f -name "$binary" | head -1)
        if [ -n "$bin_path" ]; then
            cp "$bin_path" "$TOOLS_BIN_DIR/$binary"
            chmod +x "$TOOLS_BIN_DIR/$binary"
            success "$binary installed to $TOOLS_BIN_DIR/$binary"
        else
            warn "$binary binary not found in extracted archive. Skipping."
        fi

        rm -rf "$tmp_dir"
    }

    case "$ARCH" in
        x86_64)  RG_ARCH="x86_64-unknown-linux-musl" ;;
        aarch64) RG_ARCH="aarch64-unknown-linux-gnu" ;;
        *)       RG_ARCH="x86_64-unknown-linux-musl" ;;
    esac
    install_github_binary "BurntSushi/ripgrep" "rg" "${RG_ARCH}"

    case "$ARCH" in
        x86_64)  FD_ARCH="x86_64-unknown-linux-musl" ;;
        aarch64) FD_ARCH="aarch64-unknown-linux-gnu" ;;
        *)       FD_ARCH="x86_64-unknown-linux-musl" ;;
    esac
    install_github_binary "sharkdp/fd" "fd" "${FD_ARCH}"

    case "$ARCH" in
        x86_64)  BAT_ARCH="x86_64-unknown-linux-musl" ;;
        aarch64) BAT_ARCH="aarch64-unknown-linux-gnu" ;;
        *)       BAT_ARCH="x86_64-unknown-linux-musl" ;;
    esac
    install_github_binary "sharkdp/bat" "bat" "${BAT_ARCH}"

    case "$ARCH" in
        x86_64)  FZF_ARCH="linux_amd64" ;;
        aarch64) FZF_ARCH="linux_arm64" ;;
        *)       FZF_ARCH="linux_amd64" ;;
    esac
    install_github_binary "junegunn/fzf" "fzf" "${FZF_ARCH}"

    # Ensure ~/.local/bin is on PATH for this session and future shells
    if [[ ":$PATH:" != *":$TOOLS_BIN_DIR:"* ]]; then
        export PATH="$TOOLS_BIN_DIR:$PATH"
        for rc in "$HOME/.bashrc" "$HOME/.bash_profile" "$HOME/.profile"; do
            if [ -f "$rc" ] && ! grep -q "$TOOLS_BIN_DIR" "$rc"; then
                echo "export PATH=\"$TOOLS_BIN_DIR:\$PATH\"" >> "$rc"
            fi
        done
    fi

    success "Modern CLI tools installed"
fi

#===============================================================================
# Install Claude Code
#===============================================================================

if [ "$CLAUDE_INSTALLED" = false ]; then
    step "Installing Claude Code..."

    curl -fsSL https://claude.ai/install.sh | bash

    # Add to PATH for current session
    export PATH="$HOME/.local/bin:$PATH"

    # Persist ~/.local/bin to PATH in shell config files
    PATH_LINE="export PATH=\"\$HOME/.local/bin:\$PATH\""
    for rc in "$HOME/.bashrc" "$HOME/.zshrc"; do
        if [ -f "$rc" ] && ! grep -q '\.local/bin' "$rc"; then
            echo "" >> "$rc"
            echo "# Added by Lobster installer" >> "$rc"
            echo "$PATH_LINE" >> "$rc"
            info "Added ~/.local/bin to PATH in $rc"
        fi
    done

    if command -v claude &>/dev/null; then
        success "Claude Code installed"
    else
        error "Claude Code installation failed"
        exit 1
    fi
fi

# Check if Claude Code already has a valid OAuth session
step "Checking existing Claude Code authentication..."

EXISTING_OAUTH=false
if claude auth status &>/dev/null 2>&1; then
    # auth status only checks if credentials exist, not if they're valid.
    # Verify the token actually works by making a real API call.
    info "Credentials found, verifying token is still valid..."
    if claude --print -p "ping" --max-turns 1 &>/dev/null 2>&1; then
        success "Claude Code authenticated via OAuth (token verified)"
        EXISTING_OAUTH=true
    else
        warn "OAuth credentials exist but token is expired or invalid."
        warn "You'll need to re-authenticate during the auth setup step."
    fi
elif [ -n "${ANTHROPIC_API_KEY:-}" ]; then
    success "ANTHROPIC_API_KEY found in environment"
fi
# Full auth flow runs later after Telegram config (see "Authentication Method" section)

#===============================================================================
# Install Lobster Code
#===============================================================================

# Detect install mode: --dev flag, existing .git, or tarball
if [ -d "$INSTALL_DIR/.git" ]; then
    INSTALL_MODE="git"
    info "Existing git install detected"
elif $DEV_MODE; then
    INSTALL_MODE="git"
    info "Developer mode requested"
else
    INSTALL_MODE="tarball"
    info "Tarball install mode"
fi

if [ "$INSTALL_MODE" = "git" ]; then
    step "Setting up Lobster repository..."

    if [ -d "$INSTALL_DIR/.git" ]; then
        info "Repository exists. Updating..."
        cd "$INSTALL_DIR"
        git fetch --quiet
        git checkout --quiet "$REPO_BRANCH"
        git pull --quiet origin "$REPO_BRANCH"
    else
        info "Cloning repository from $REPO_URL (branch: $REPO_BRANCH)..."
        git clone --quiet --branch "$REPO_BRANCH" "$REPO_URL" "$INSTALL_DIR"
        cd "$INSTALL_DIR"
    fi

    success "Repository ready at $INSTALL_DIR (branch: $REPO_BRANCH)"

    step "Configuring distributed git hooks..."
    cd "$INSTALL_DIR"
    git config --local core.hooksPath .githooks
    chmod +x .githooks/pre-push .githooks/post-checkout .githooks/pre-commit .githooks/post-merge .githooks/post-rewrite 2>/dev/null || true
    success "Git hooks configured (core.hooksPath -> .githooks)"

else
    step "Downloading latest Lobster release..."

    if [ -d "$INSTALL_DIR" ] && [ -f "$INSTALL_DIR/VERSION" ]; then
        info "Existing tarball install found (v$(cat "$INSTALL_DIR/VERSION")). Updating..."
    fi

    # Fetch latest release from GitHub API; fall back to git clone on any error
    RELEASE_JSON=$(curl -fsSL "$GITHUB_API/releases/latest" 2>/dev/null) || true
    LATEST_TAG=$(echo "$RELEASE_JSON" | jq -r '.tag_name // empty' 2>/dev/null || true)

    if [ -z "$LATEST_TAG" ]; then
        info "No release found, falling back to git clone..."
        git clone --quiet --branch "$REPO_BRANCH" "$REPO_URL" "$INSTALL_DIR"
        cd "$INSTALL_DIR"
        success "Repository ready at $INSTALL_DIR (git fallback)"
    else
        LATEST_VERSION="${LATEST_TAG#v}"
        info "Latest release: $LATEST_TAG"

        # Find tarball asset
        TARBALL_URL=$(echo "$RELEASE_JSON" | jq -r '.assets[] | select(.name | test("lobster.*\\.tar\\.gz")) | .browser_download_url' | head -1)
        if [ -z "$TARBALL_URL" ]; then
            TARBALL_URL=$(echo "$RELEASE_JSON" | jq -r '.tarball_url // empty')
        fi

        if [ -z "$TARBALL_URL" ]; then
            error "No tarball found in release. Falling back to git clone..."
            git clone --quiet --branch "$REPO_BRANCH" "$REPO_URL" "$INSTALL_DIR"
            cd "$INSTALL_DIR"
        else
            # Download tarball
            TMP_DIR=$(mktemp -d)
            TARBALL_FILE="$TMP_DIR/lobster.tar.gz"
            info "Downloading $TARBALL_URL..."
            curl -fsSL -o "$TARBALL_FILE" "$TARBALL_URL" || {
                error "Failed to download tarball"
                rm -rf "$TMP_DIR"
                exit 1
            }
            success "Downloaded $(du -h "$TARBALL_FILE" | cut -f1)"

            # Verify checksum if available
            CHECKSUM_URL=$(echo "$RELEASE_JSON" | jq -r '.assets[] | select(.name | test("checksums|sha256")) | .browser_download_url' | head -1)
            if [ -n "$CHECKSUM_URL" ]; then
                info "Verifying checksum..."
                EXPECTED=$(curl -fsSL "$CHECKSUM_URL" 2>/dev/null | head -1 | awk '{print $1}')
                ACTUAL=$(sha256sum "$TARBALL_FILE" | awk '{print $1}')
                if [ -n "$EXPECTED" ] && [ "$EXPECTED" != "$ACTUAL" ]; then
                    error "Checksum mismatch!"
                    rm -rf "$TMP_DIR"
                    exit 1
                fi
                success "Checksum verified"
            fi

            # Extract
            EXTRACT_DIR="$TMP_DIR/extracted"
            mkdir -p "$EXTRACT_DIR"
            tar xzf "$TARBALL_FILE" -C "$EXTRACT_DIR"

            # Find extracted directory
            NEW_INSTALL=$(find "$EXTRACT_DIR" -maxdepth 1 -mindepth 1 -type d | head -1)
            [ -z "$NEW_INSTALL" ] && NEW_INSTALL="$EXTRACT_DIR"

            # Preserve .venv if upgrading
            if [ -d "$INSTALL_DIR/.venv" ]; then
                mv "$INSTALL_DIR/.venv" "$NEW_INSTALL/.venv"
            fi
            if [ -d "$INSTALL_DIR/.state" ]; then
                mv "$INSTALL_DIR/.state" "$NEW_INSTALL/.state"
            fi

            # Swap
            if [ -d "$INSTALL_DIR" ]; then
                BACKUP="$HOME/lobster.bak"
                [ -d "$BACKUP" ] && rm -rf "$BACKUP"
                mv "$INSTALL_DIR" "$BACKUP"
            fi
            mv "$NEW_INSTALL" "$INSTALL_DIR"

            # Make scripts executable
            chmod +x "$INSTALL_DIR/scripts/"*.sh 2>/dev/null || true
            chmod +x "$INSTALL_DIR/install.sh" 2>/dev/null || true

            rm -rf "$TMP_DIR"
            cd "$INSTALL_DIR"
            success "Lobster v$LATEST_VERSION installed at $INSTALL_DIR (no .git/)"
        fi
    fi
fi

#===============================================================================
# Create Directories
#===============================================================================

step "Creating directories..."

mkdir -p "$WORKSPACE_DIR"/{logs,data,scheduled-jobs/{logs,tasks}}
mkdir -p "$MESSAGES_DIR"/{inbox,outbox,processed,processing,failed,config,audio,task-outputs}
mkdir -p "$CONFIG_DIR"
mkdir -p "$PROJECTS_DIR"
mkdir -p "$USER_CONFIG_DIR/memory"/{canonical/{people,projects},archive/digests}
mkdir -p "$USER_CONFIG_DIR/agents/subagents"

# Legacy: also create ~/projects/ for backward compatibility
mkdir -p "$HOME/projects"/{personal,business}

# Seed canonical templates (only files that don't already exist; skip examples)
TEMPLATES_DIR="$INSTALL_DIR/memory/canonical-templates"
if [ -d "$TEMPLATES_DIR" ]; then
    for tmpl in "$TEMPLATES_DIR"/*.md; do
        [ -f "$tmpl" ] || continue
        base=$(basename "$tmpl")
        dest="$USER_CONFIG_DIR/memory/canonical/$base"
        if [ ! -f "$dest" ]; then
            cp "$tmpl" "$dest"
            info "  Seeded canonical template: $base"
        fi
    done
fi

# Create stub user-config agent files if they don't exist
for stub_file in "base.bootup.md" "base.context.md" "dispatcher.bootup.md" "subagent.bootup.md"; do
    stub_dest="$USER_CONFIG_DIR/agents/$stub_file"
    if [ ! -f "$stub_dest" ]; then
        touch "$stub_dest"
        info "  Created stub: agents/$stub_file"
    fi
done

success "Directories created"
info "  $PROJECTS_DIR - All Lobster-managed projects"

#===============================================================================
# Global Environment Store
#===============================================================================

step "Setting up global environment store..."

GLOBAL_ENV_FILE="$CONFIG_DIR/global.env"

if [ ! -f "$GLOBAL_ENV_FILE" ]; then
    cat > "$GLOBAL_ENV_FILE" << 'GLOBALENV'
# Lobster Global Environment Store
# Machine-wide API tokens and credentials shared across services and tools.
# Format: KEY=value  (no export keyword needed)
# Use: lobster env set KEY VALUE   to add or update entries
# Use: lobster env list             to see all stored keys

# === Cloud Providers ===
# HETZNER_API_TOKEN=
# DO_TOKEN=
# CLOUDFLARE_API_TOKEN=

# === AI / LLM Services ===
# ANTHROPIC_API_KEY=
# OPENAI_API_KEY=

# === Code / DevOps ===
# GITHUB_TOKEN=
# VERCEL_TOKEN=

# === Communication Services ===
# TWILIO_ACCOUNT_SID=
# TWILIO_AUTH_TOKEN=

# === Add your own below ===
GLOBALENV
    chmod 600 "$GLOBAL_ENV_FILE" || true
    success "Global env store created: $GLOBAL_ENV_FILE"
else
    info "Global env store already exists: $GLOBAL_ENV_FILE"
fi

# Add shell integration: source global.env on login so tokens are available
# to any script or CLI tool in the user's shell sessions.
for _rc in "$HOME/.bashrc" "$HOME/.zshrc" "$HOME/.profile"; do
    if [ -f "$_rc" ] && ! grep -q "Lobster global env store" "$_rc"; then
        {
            echo ""
            echo "# Lobster global env store"
            echo "[ -f \"$GLOBAL_ENV_FILE\" ] && set -a && . \"$GLOBAL_ENV_FILE\" && set +a"
        } >> "$_rc"
        info "  Shell integration added to $_rc"
    fi
done

success "Global env store configured"
info "  File: $GLOBAL_ENV_FILE"
info "  Use 'lobster env set KEY VALUE' to store API tokens"
info "  Use 'lobster env list' to see stored keys"
info "  See docs/GLOBAL-ENV.md for full documentation"

#===============================================================================
# Scheduled Tasks Setup
#===============================================================================

step "Setting up scheduled tasks infrastructure..."

# Create jobs.json if it doesn't exist (in workspace, not repo)
JOBS_FILE="$WORKSPACE_DIR/scheduled-jobs/jobs.json"
if [ ! -f "$JOBS_FILE" ]; then
    echo '{"jobs": {}}' > "$JOBS_FILE"
fi

# Create run-job.sh
cat > "$INSTALL_DIR/scheduled-tasks/run-job.sh" << 'RUNJOB'
#!/bin/bash
# Lobster Scheduled Task Executor
# Runs a scheduled job in a fresh Claude instance

set -e

# Ensure Claude is in PATH (cron doesn't inherit user PATH)
export PATH="$HOME/.local/bin:$PATH"

JOB_NAME="$1"

if [ -z "$JOB_NAME" ]; then
    echo "Usage: $0 <job-name>"
    exit 1
fi

REPO_DIR="${LOBSTER_INSTALL_DIR:-$HOME/lobster}"
WORKSPACE="${LOBSTER_WORKSPACE:-$HOME/lobster-workspace}"
TASK_FILE="$WORKSPACE/scheduled-jobs/tasks/${JOB_NAME}.md"
OUTPUT_DIR="$HOME/messages/task-outputs"
LOG_DIR="$WORKSPACE/scheduled-jobs/logs"
TIMESTAMP=$(date +%Y%m%d-%H%M%S)
JOBS_FILE="$WORKSPACE/scheduled-jobs/jobs.json"

mkdir -p "$OUTPUT_DIR" "$LOG_DIR"

if [ ! -f "$TASK_FILE" ]; then
    echo "Error: Task file not found: $TASK_FILE"
    exit 1
fi

TASK_CONTENT=$(cat "$TASK_FILE")
LOG_FILE="$LOG_DIR/${JOB_NAME}-${TIMESTAMP}.log"

START_TIME=$(date +%s)
START_ISO=$(date -Iseconds)

echo "[$START_ISO] Starting job: $JOB_NAME" | tee "$LOG_FILE"

claude -p "$TASK_CONTENT

---

IMPORTANT: You are running as a scheduled task. When you complete your task:
1. Call write_task_output() with your results summary
2. Keep output concise - the main Lobster instance will review this later
3. Exit after writing output - do not start a loop" \
    --dangerously-skip-permissions \
    --max-turns 15 \
    2>&1 | tee -a "$LOG_FILE"

EXIT_CODE=$?

END_TIME=$(date +%s)
END_ISO=$(date -Iseconds)
DURATION=$((END_TIME - START_TIME))

echo "" | tee -a "$LOG_FILE"
echo "[$END_ISO] Job completed in ${DURATION}s with exit code: $EXIT_CODE" | tee -a "$LOG_FILE"

if [ -f "$JOBS_FILE" ]; then
    # Use jq if available, otherwise use Python
    if command -v jq &> /dev/null; then
        STATUS="success"
        [ $EXIT_CODE -ne 0 ] && STATUS="failed"

        TMP_FILE=$(mktemp)
        jq --arg name "$JOB_NAME" \
           --arg last_run "$END_ISO" \
           --arg status "$STATUS" \
           '.jobs[$name].last_run = $last_run | .jobs[$name].last_status = $status' \
           "$JOBS_FILE" > "$TMP_FILE" && mv "$TMP_FILE" "$JOBS_FILE"
    else
        python3 -c "
import json
import sys
with open('$JOBS_FILE', 'r') as f:
    data = json.load(f)
if '$JOB_NAME' in data.get('jobs', {}):
    data['jobs']['$JOB_NAME']['last_run'] = '$END_ISO'
    data['jobs']['$JOB_NAME']['last_status'] = 'success' if $EXIT_CODE == 0 else 'failed'
    with open('$JOBS_FILE', 'w') as f:
        json.dump(data, f, indent=2)
"
    fi
fi

exit $EXIT_CODE
RUNJOB
chmod +x "$INSTALL_DIR/scheduled-tasks/run-job.sh" || true

# Create sync-crontab.sh
cat > "$INSTALL_DIR/scheduled-tasks/sync-crontab.sh" << 'SYNCCRON'
#!/bin/bash
# Lobster Crontab Synchronizer

set -e

WORKSPACE="${LOBSTER_WORKSPACE:-$HOME/lobster-workspace}"
REPO_DIR="${LOBSTER_INSTALL_DIR:-$HOME/lobster}"
JOBS_FILE="$WORKSPACE/scheduled-jobs/jobs.json"
RUNNER="$REPO_DIR/scheduled-tasks/run-job.sh"

if ! command -v crontab &> /dev/null; then
    echo "Warning: crontab not found. Install cron to enable scheduled tasks."
    exit 0
fi

if [ ! -f "$JOBS_FILE" ]; then
    echo "Error: Jobs file not found: $JOBS_FILE"
    exit 1
fi

MARKER="# LOBSTER-SCHEDULED"
EXISTING=$(crontab -l 2>/dev/null | grep -v "$MARKER" | grep -v "$RUNNER" || true)

if command -v jq &> /dev/null; then
    CRON_ENTRIES=$(jq -r --arg runner "$RUNNER" --arg marker "$MARKER" '
        .jobs | to_entries[] |
        select(.value.enabled == true) |
        "\(.value.schedule) \($runner) \(.key) \($marker)"
    ' "$JOBS_FILE" 2>/dev/null || echo "")
else
    CRON_ENTRIES=""
fi

{
    if [ -n "$EXISTING" ]; then
        echo "$EXISTING"
    fi
    if [ -n "$CRON_ENTRIES" ]; then
        echo "$CRON_ENTRIES"
    fi
} | crontab -

echo "Crontab synchronized:"
crontab -l 2>/dev/null | grep "$MARKER" || echo "(no lobster jobs)"
SYNCCRON
chmod +x "$INSTALL_DIR/scheduled-tasks/sync-crontab.sh" || true

# Enable cron service (name differs by distro)
if [ "$PKG_MANAGER" = "apt" ]; then
    sudo systemctl enable cron 2>/dev/null || true
    sudo systemctl start cron 2>/dev/null || true
else
    # Amazon Linux / Fedora uses crond
    sudo systemctl enable crond 2>/dev/null || true
    sudo systemctl start crond 2>/dev/null || true
fi

success "Scheduled tasks infrastructure ready"

#===============================================================================
# Health Check Setup
#===============================================================================

step "Setting up health monitoring..."

# Make scripts executable
chmod +x "$INSTALL_DIR/scripts/health-check-v3.sh" || true

# Add health check to crontab (runs every 2 minutes)
HEALTH_MARKER="# LOBSTER-HEALTH"
({ crontab -l 2>/dev/null | grep -v "$HEALTH_MARKER" | grep -v "health-check" || true; }; \
 echo "*/2 * * * * $INSTALL_DIR/scripts/health-check-v3.sh $HEALTH_MARKER") | crontab -

success "Health monitoring configured (checks every 2 minutes)"

#===============================================================================
# Daily Dependency Health Check
#===============================================================================

step "Setting up daily dependency health check..."

chmod +x "$INSTALL_DIR/scripts/daily-health-check.sh" || true

# Add daily health check to crontab (runs at 06:00 every day)
DAILY_MARKER="# LOBSTER-DAILY-HEALTH"
({ crontab -l 2>/dev/null | grep -v "$DAILY_MARKER" | grep -v "daily-health-check" || true; }; \
 echo "0 6 * * * $INSTALL_DIR/scripts/daily-health-check.sh $DAILY_MARKER") | crontab -

success "Daily dependency health check configured (runs at 06:00 daily)"

#===============================================================================
# Nightly Consolidation
#===============================================================================

step "Setting up nightly consolidation..."

chmod +x "$INSTALL_DIR/scripts/nightly-consolidation.sh" || true

# Add nightly consolidation to crontab (runs at 03:00 every night)
CONSOLIDATION_MARKER="# LOBSTER-NIGHTLY-CONSOLIDATION"
({ crontab -l 2>/dev/null | grep -v "$CONSOLIDATION_MARKER" | grep -v "nightly-consolidation" || true; }; \
 echo "0 3 * * * $INSTALL_DIR/scripts/nightly-consolidation.sh $CONSOLIDATION_MARKER") | crontab -

success "Nightly consolidation configured (runs at 03:00 nightly)"

#===============================================================================
# Self-Check Reminder System
#===============================================================================

step "Setting up self-check reminder system..."

# The self-check system ensures Lobster checks on background agent completion.
# Uses cron-based periodic-self-check.sh (runs every 3 min).
# The PostToolUse hook (schedule-self-check.sh) has been retired; subagent
# results are now delivered directly to the inbox via write_result.

# Make self-check scripts executable
chmod +x "$INSTALL_DIR/scripts/periodic-self-check.sh" || true

# Create state directory for rate limiting
mkdir -p "$INSTALL_DIR/.state"

# Add periodic self-check to crontab (runs every 3 minutes)
SELFCHECK_MARKER="# LOBSTER-SELF-CHECK"
({ crontab -l 2>/dev/null | grep -v "$SELFCHECK_MARKER" | grep -v "periodic-self-check" || true; }; \
 echo "*/3 * * * * $INSTALL_DIR/scripts/periodic-self-check.sh $SELFCHECK_MARKER") | crontab -

CLAUDE_SETTINGS_DIR="$HOME/.claude"
CLAUDE_SETTINGS="$CLAUDE_SETTINGS_DIR/settings.json"
mkdir -p "$CLAUDE_SETTINGS_DIR"

if [ ! -f "$CLAUDE_SETTINGS" ]; then
    # Create settings.json with PreToolUse and PostToolUse hooks
    cat > "$CLAUDE_SETTINGS" << HOOKEOF
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "Edit|Write",
        "hooks": [
          {
            "type": "command",
            "command": "python3 $INSTALL_DIR/hooks/restore-exec-bit.py",
            "timeout": 5
          }
        ]
      }
    ],
    "PreToolUse": [
      {
        "matcher": "Write|Edit",
        "hooks": [
          {
            "type": "command",
            "command": "python3 $INSTALL_DIR/hooks/no-auto-memory.py",
            "timeout": 5
          }
        ]
      }
    ]
  }
}
HOOKEOF
    success "Claude Code settings created with hooks"
fi

success "Self-check cron configured (every 3min)"

# Set up Claude Code PreToolUse hook to block writes to .claude/memory/
if [ -f "$CLAUDE_SETTINGS" ]; then
    if ! jq -e '.hooks.PreToolUse[]? | select(.matcher == "Write|Edit")' "$CLAUDE_SETTINGS" > /dev/null 2>&1; then
        TMP_SETTINGS=$(mktemp)
        jq '.hooks.PreToolUse = (.hooks.PreToolUse // []) + [{
            "matcher": "Write|Edit",
            "hooks": [{
                "type": "command",
                "command": "python3 '"$INSTALL_DIR"'/hooks/no-auto-memory.py",
                "timeout": 5
            }]
        }]' "$CLAUDE_SETTINGS" > "$TMP_SETTINGS" && mv "$TMP_SETTINGS" "$CLAUDE_SETTINGS"
        success "No-auto-memory hook added to Claude Code settings"
    else
        info "No-auto-memory hook already configured in Claude Code settings"
    fi
else
    info "Skipping no-auto-memory hook (settings.json not yet created)"
fi

# Set up Claude Code PreToolUse hook to enforce clickable links for completed work
chmod +x "$INSTALL_DIR/hooks/link-checker.py" || true
if [ -f "$CLAUDE_SETTINGS" ]; then
    if ! jq -e '.hooks.PreToolUse[]? | select(.matcher == "mcp__lobster-inbox__send_reply")' "$CLAUDE_SETTINGS" > /dev/null 2>&1; then
        TMP_SETTINGS=$(mktemp)
        jq '.hooks.PreToolUse = (.hooks.PreToolUse // []) + [{
            "matcher": "mcp__lobster-inbox__send_reply",
            "hooks": [{
                "type": "command",
                "command": "python3 '"$INSTALL_DIR"'/hooks/link-checker.py",
                "timeout": 5
            }]
        }]' "$CLAUDE_SETTINGS" > "$TMP_SETTINGS" && mv "$TMP_SETTINGS" "$CLAUDE_SETTINGS"
        success "Link enforcement hook installed"
    else
        info "Link enforcement hook already configured in Claude Code settings"
    fi
else
    info "Skipping link enforcement hook (settings.json not yet created)"
fi

# Set up Claude Code PreToolUse hook to block generic Agent calls without subagent_type
chmod +x "$INSTALL_DIR/hooks/require-subagent-type.py" || true
if [ -f "$CLAUDE_SETTINGS" ]; then
    if ! jq -e '.hooks.PreToolUse[]? | select(.matcher == "Agent")' "$CLAUDE_SETTINGS" > /dev/null 2>&1; then
        TMP_SETTINGS=$(mktemp)
        jq '.hooks.PreToolUse = (.hooks.PreToolUse // []) + [{
            "matcher": "Agent",
            "hooks": [{
                "type": "command",
                "command": "python3 '"$INSTALL_DIR"'/hooks/require-subagent-type.py",
                "timeout": 5
            }]
        }]' "$CLAUDE_SETTINGS" > "$TMP_SETTINGS" && mv "$TMP_SETTINGS" "$CLAUDE_SETTINGS"
        success "require-subagent-type hook installed"
    else
        info "require-subagent-type hook already configured in Claude Code settings"
    fi
else
    info "Skipping require-subagent-type hook (settings.json not yet created)"
fi

# Set up Claude Code PostToolUse hook to restore execute bit after Edit/Write
chmod +x "$INSTALL_DIR/hooks/restore-exec-bit.py" || true
if [ -f "$CLAUDE_SETTINGS" ]; then
    if ! jq -e '.hooks.PostToolUse[]? | select(.matcher == "Edit|Write")' "$CLAUDE_SETTINGS" > /dev/null 2>&1; then
        TMP_SETTINGS=$(mktemp)
        jq '.hooks.PostToolUse = (.hooks.PostToolUse // []) + [{
            "matcher": "Edit|Write",
            "hooks": [{
                "type": "command",
                "command": "python3 '"$INSTALL_DIR"'/hooks/restore-exec-bit.py",
                "timeout": 5
            }]
        }]' "$CLAUDE_SETTINGS" > "$TMP_SETTINGS" && mv "$TMP_SETTINGS" "$CLAUDE_SETTINGS"
        success "restore-exec-bit hook installed"
    else
        info "restore-exec-bit hook already configured in Claude Code settings"
    fi
else
    info "Skipping restore-exec-bit hook (settings.json not yet created)"
fi

# Set up Claude Code PreToolUse hook to block tool use after compaction without context reload
chmod +x "$INSTALL_DIR/hooks/post-compact-gate.py" || true
if [ -f "$CLAUDE_SETTINGS" ]; then
    if ! jq -e '.hooks.PreToolUse[]? | select(.matcher == "")' "$CLAUDE_SETTINGS" > /dev/null 2>&1; then
        TMP_SETTINGS=$(mktemp)
        jq '.hooks.PreToolUse = (.hooks.PreToolUse // []) + [{
            "matcher": "",
            "hooks": [{
                "type": "command",
                "command": "python3 '"$INSTALL_DIR"'/hooks/post-compact-gate.py",
                "timeout": 5
            }]
        }]' "$CLAUDE_SETTINGS" > "$TMP_SETTINGS" && mv "$TMP_SETTINGS" "$CLAUDE_SETTINGS"
        success "post-compact-gate hook installed"
    else
        info "post-compact-gate hook already configured in Claude Code settings"
    fi
else
    info "Skipping post-compact-gate hook (settings.json not yet created)"
fi

# Set up Claude Code SessionStart hook to set compact flag on context compaction
chmod +x "$INSTALL_DIR/hooks/on-compact.py" || true
if [ -f "$CLAUDE_SETTINGS" ]; then
    if ! jq -e '.hooks.SessionStart[]? | select(.matcher == "compact")' "$CLAUDE_SETTINGS" > /dev/null 2>&1; then
        TMP_SETTINGS=$(mktemp)
        jq '.hooks.SessionStart = (.hooks.SessionStart // []) + [{
            "matcher": "compact",
            "hooks": [{
                "type": "command",
                "command": "python3 '"$INSTALL_DIR"'/hooks/on-compact.py",
                "timeout": 5
            }]
        }]' "$CLAUDE_SETTINGS" > "$TMP_SETTINGS" && mv "$TMP_SETTINGS" "$CLAUDE_SETTINGS"
        success "on-compact hook installed"
    else
        info "on-compact hook already configured in Claude Code settings"
    fi
else
    info "Skipping on-compact hook (settings.json not yet created)"
fi

#===============================================================================
# Python Environment
#===============================================================================

step "Setting up Python environment..."

cd "$INSTALL_DIR"

# Install uv if not present (faster, more reliable Python package manager)
if ! command -v uv &>/dev/null; then
    info "Installing uv (Python package manager)..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
    if command -v uv &>/dev/null; then
        success "uv installed"
    else
        error "uv installation failed"
        exit 1
    fi
else
    success "uv already installed"
fi

if [ ! -d ".venv" ] || [ ! -f ".venv/bin/python" ]; then
    info "Creating Python virtual environment..."
    uv venv .venv
else
    success "Python venv already exists"
fi

# Activate venv for uv pip commands
export VIRTUAL_ENV="$INSTALL_DIR/.venv"
export PATH="$INSTALL_DIR/.venv/bin:$PATH"

uv pip install --quiet --upgrade pip
uv pip install --quiet mcp python-telegram-bot watchdog python-dotenv slack-bolt psutil websockets
success "Core Python packages installed"

#-------------------------------------------------------------------------------
# fastembed
#-------------------------------------------------------------------------------
info "Installing fastembed..."
if uv pip install --quiet fastembed; then
    success "fastembed installed"
else
    warn "fastembed install failed. Vector embedding features may be unavailable."
fi

#-------------------------------------------------------------------------------
# sqlite-vec  (known aarch64 ELFCLASS32 bug in older releases; try alpha first)
#-------------------------------------------------------------------------------
info "Installing sqlite-vec..."
SQLITE_VEC_OK=false

# Try stable release first
if uv pip install --quiet sqlite-vec 2>/dev/null; then
    # Verify it actually loads (aarch64 bug produces an import error)
    if "$INSTALL_DIR/.venv/bin/python" -c "import sqlite3, sqlite_vec; c=sqlite3.connect(':memory:'); c.enable_load_extension(True); sqlite_vec.load(c)" 2>/dev/null; then
        success "sqlite-vec installed and loads correctly"
        SQLITE_VEC_OK=true
    else
        warn "sqlite-vec installed but fails to load (likely aarch64 ELFCLASS32 bug). Trying alpha..."
        uv pip uninstall sqlite-vec 2>/dev/null || true
    fi
fi

if [ "$SQLITE_VEC_OK" = false ]; then
    # Try known-good alpha that contains the aarch64 fix
    if uv pip install --quiet "sqlite-vec==0.1.7a2" 2>/dev/null; then
        if "$INSTALL_DIR/.venv/bin/python" -c "import sqlite3, sqlite_vec; c=sqlite3.connect(':memory:'); c.enable_load_extension(True); sqlite_vec.load(c)" 2>/dev/null; then
            success "sqlite-vec 0.1.7a2 (alpha) installed and loads correctly"
            SQLITE_VEC_OK=true
        else
            warn "sqlite-vec alpha also fails to load. Will attempt to compile from source."
            uv pip uninstall sqlite-vec 2>/dev/null || true
        fi
    fi
fi

if [ "$SQLITE_VEC_OK" = false ]; then
    warn "Attempting to build sqlite-vec from source (last resort)..."
    _SQLITE_VEC_SRC_DIR="$(mktemp -d)"
    if git clone --quiet --depth 1 https://github.com/asg017/sqlite-vec.git "$_SQLITE_VEC_SRC_DIR" 2>/dev/null; then
        cd "$_SQLITE_VEC_SRC_DIR"
        if make loadable python 2>/dev/null && uv pip install --quiet -e . 2>/dev/null; then
            if "$INSTALL_DIR/.venv/bin/python" -c "import sqlite3, sqlite_vec; c=sqlite3.connect(':memory:'); c.enable_load_extension(True); sqlite_vec.load(c)" 2>/dev/null; then
                success "sqlite-vec built from source and loads correctly"
                SQLITE_VEC_OK=true
            else
                warn "sqlite-vec source build also fails to load. Vector search will be unavailable."
            fi
        else
            warn "sqlite-vec source build failed. Vector search will be unavailable."
        fi
        cd "$INSTALL_DIR"
    fi
    rm -rf "$_SQLITE_VEC_SRC_DIR"
fi

# Unset VIRTUAL_ENV — we don't need the venv active for the rest of the script
unset VIRTUAL_ENV

success "Python environment ready"

#===============================================================================
# Configuration
#===============================================================================

step "Configuring Lobster..."

CONFIG_FILE="$CONFIG_DIR/config.env"
CONFIG_EXAMPLE="$INSTALL_DIR/config/config.env.example"

# Check if already configured
if [ -f "$CONFIG_FILE" ]; then
    source "$CONFIG_FILE"
    if [ -n "$TELEGRAM_BOT_TOKEN" ] && [ "$TELEGRAM_BOT_TOKEN" != "your_bot_token_here" ]; then
        info "Existing configuration found"
        NEED_CONFIG=false
        if [ "$NON_INTERACTIVE" = false ]; then
            echo ""
            echo "Current config:"
            echo "  Bot Token: ${TELEGRAM_BOT_TOKEN:0:10}...${TELEGRAM_BOT_TOKEN: -5}"
            echo "  Allowed Users: $TELEGRAM_ALLOWED_USERS"
            echo ""
            read -p "Keep existing configuration? [Y/n] " -n 1 -r
            echo
            if [[ $REPLY =~ ^[Nn]$ ]]; then
                NEED_CONFIG=true
            fi
        fi
    else
        NEED_CONFIG=true
    fi
else
    NEED_CONFIG=true
fi

if [ "$NEED_CONFIG" = true ] && [ "$NON_INTERACTIVE" = true ]; then
    warn "Skipping Telegram configuration (non-interactive mode)."
    info "Run the installer again without --non-interactive to configure Telegram."
    # Write a placeholder config so downstream steps don't fail
    if [ ! -f "$CONFIG_FILE" ]; then
        mkdir -p "$(dirname "$CONFIG_FILE")"
        cat > "$CONFIG_FILE" << EOF
# Lobster Configuration
# Generated by installer on $(date) (non-interactive - needs configuration)

# Telegram Bot (UNCONFIGURED - run installer interactively to set up)
TELEGRAM_BOT_TOKEN=your_bot_token_here
TELEGRAM_ALLOWED_USERS=
EOF
    fi
    NEED_CONFIG=false
fi

if [ "$NEED_CONFIG" = true ]; then
    echo ""
    echo -e "${BOLD}Telegram Bot Setup${NC}"
    echo ""
    echo "You need a Telegram bot token and your user ID."
    echo ""
    echo "To get a bot token:"
    echo "  1. Open Telegram and message @BotFather"
    echo "  2. Send /newbot and follow the prompts"
    echo "  3. Copy the token (looks like: 123456789:ABCdefGHI...)"
    echo ""
    echo "To get your numeric user ID (NOT your @username):"
    echo "  1. Message @userinfobot on Telegram"
    echo "  2. It will reply with your numeric ID (e.g. 123456789)"
    echo ""
    echo -e "  ${YELLOW}Important: Your user ID is a number like 123456789${NC}"
    echo -e "  ${YELLOW}           It is NOT your @username${NC}"
    echo ""

    # Get bot token
    while true; do
        read -p "Enter your Telegram bot token: " BOT_TOKEN
        if [[ "$BOT_TOKEN" =~ ^[0-9]+:[A-Za-z0-9_-]+$ ]]; then
            break
        else
            warn "Invalid token format. Should be like: 123456789:ABCdefGHI..."
        fi
    done

    # Get user ID
    while true; do
        read -p "Enter your Telegram numeric user ID: " USER_ID
        if [[ "$USER_ID" =~ ^[0-9]+$ ]]; then
            break
        elif [[ "$USER_ID" =~ ^@ ]]; then
            warn "That's your @username. You need your numeric ID."
            echo "    Message @userinfobot on Telegram to get it."
        else
            warn "Invalid user ID. Must be a number like: 123456789"
        fi
    done

    # Write config (Telegram only; auth method is configured in the next section)
    cat > "$CONFIG_FILE" << EOF
# Lobster Configuration
# Generated by installer on $(date)

# Telegram Bot
TELEGRAM_BOT_TOKEN=$BOT_TOKEN
TELEGRAM_ALLOWED_USERS=$USER_ID
EOF

    success "Telegram configuration saved"
fi

#===============================================================================
# Generate LOBSTER_INTERNAL_SECRET (required for Google Calendar token refresh)
#===============================================================================

if [ -f "$CONFIG_FILE" ]; then
    source "$CONFIG_FILE"
    if [ -z "${LOBSTER_INTERNAL_SECRET:-}" ]; then
        step "Generating LOBSTER_INTERNAL_SECRET..."
        INTERNAL_SECRET=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
        echo "" >> "$CONFIG_FILE"
        echo "# Shared secret for myownlobster.ai token bridge (Google Calendar refresh)" >> "$CONFIG_FILE"
        echo "# This must match LOBSTER_INTERNAL_SECRET on the myownlobster.ai Vercel deployment." >> "$CONFIG_FILE"
        echo "# Generated by installer on $(date)" >> "$CONFIG_FILE"
        echo "LOBSTER_INTERNAL_SECRET=$INTERNAL_SECRET" >> "$CONFIG_FILE"
        success "LOBSTER_INTERNAL_SECRET generated and saved to config.env"
    else
        success "LOBSTER_INTERNAL_SECRET already set"
    fi
fi

#===============================================================================
# GitHub MCP Server (Optional)
#===============================================================================

step "GitHub Integration (Optional)..."

# Check if GitHub MCP is already configured
GITHUB_MCP_CONFIGURED=false
if command -v claude &>/dev/null && claude mcp list 2>/dev/null | grep -q "github"; then
    GITHUB_MCP_CONFIGURED=true
    success "GitHub MCP server already configured"
fi

if [ "$GITHUB_MCP_CONFIGURED" = true ]; then
    : # Already configured, skip
elif [ -n "${GITHUB_PAT:-}" ]; then
    # PAT available from config.env or environment — auto-configure
    info "Using GITHUB_PAT from environment to set up GitHub MCP server..."
    if command -v claude &>/dev/null; then
        claude mcp add-json github "{\"type\":\"http\",\"url\":\"https://api.githubcopilot.com/mcp\",\"headers\":{\"Authorization\":\"Bearer $GITHUB_PAT\"}}" --scope user 2>/dev/null
        success "GitHub MCP server configured"
    else
        warn "Claude Code not found. Configure GitHub MCP manually after install."
    fi
elif [ "$NON_INTERACTIVE" = true ]; then
    info "Skipping GitHub integration (non-interactive mode)."
elif true; then

echo ""
echo -e "${BOLD}GitHub MCP Server Setup${NC}"
echo ""
echo "The GitHub MCP server lets Lobster:"
echo "  - Read and manage GitHub issues & PRs"
echo "  - Browse repositories and code"
echo "  - Access project boards"
echo "  - Monitor GitHub Actions workflows"
echo ""
read -p "Set up GitHub integration? [y/N] " -n 1 -r
echo

if [[ $REPLY =~ ^[Yy]$ ]]; then
    echo ""
    echo "You need a GitHub Personal Access Token (PAT)."
    echo ""
    echo "To create one:"
    echo "  1. Go to https://github.com/settings/tokens"
    echo "  2. Click 'Generate new token (classic)'"
    echo "  3. Select scopes: repo, read:org, read:project"
    echo "  4. Copy the generated token"
    echo ""

    read -p "Enter your GitHub PAT (or press Enter to skip): " GITHUB_PAT

    if [ -n "$GITHUB_PAT" ]; then
        # Add GitHub MCP server to Claude Code
        if command -v claude &> /dev/null; then
            claude mcp add-json github "{\"type\":\"http\",\"url\":\"https://api.githubcopilot.com/mcp\",\"headers\":{\"Authorization\":\"Bearer $GITHUB_PAT\"}}" --scope user 2>/dev/null
            success "GitHub MCP server configured"

            # Save PAT to config (optional, for reference)
            if [ -f "$CONFIG_FILE" ]; then
                echo "" >> "$CONFIG_FILE"
                echo "# GitHub Integration" >> "$CONFIG_FILE"
                echo "GITHUB_PAT_CONFIGURED=true" >> "$CONFIG_FILE"
            fi
        else
            warn "Claude Code not found. Configure GitHub MCP manually after install:"
            echo "  claude mcp add-json github '{\"type\":\"http\",\"url\":\"https://api.githubcopilot.com/mcp\",\"headers\":{\"Authorization\":\"Bearer YOUR_PAT\"}}'"
        fi
    else
        info "Skipped GitHub integration. You can set it up later:"
        echo "  claude mcp add-json github '{\"type\":\"http\",\"url\":\"https://api.githubcopilot.com/mcp\",\"headers\":{\"Authorization\":\"Bearer YOUR_PAT\"}}'"
    fi
else
    info "Skipped GitHub integration. You can set it up later - see README.md"
fi
fi  # end non-interactive guard for GitHub integration

#===============================================================================
# GitHub CLI Authentication
#===============================================================================

step "Checking GitHub CLI authentication..."

if gh auth status &>/dev/null 2>&1; then
    success "GitHub CLI already authenticated"
elif [ -n "${GITHUB_PAT:-}" ]; then
    info "Authenticating gh with GITHUB_PAT from environment..."
    echo "$GITHUB_PAT" | gh auth login --with-token 2>/dev/null && \
        success "GitHub CLI authenticated via PAT" || \
        warn "GitHub CLI auth via PAT failed. Authenticate later with: gh auth login"
else
    echo ""
    echo "GitHub CLI (gh) is not authenticated."
    echo "This is needed for creating PRs, managing issues, etc."
    echo ""
    read -p "Authenticate GitHub CLI now? [y/N] " -n 1 -r || true
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        if gh auth login; then
            success "GitHub CLI authenticated"
        else
            warn "GitHub CLI auth failed. Authenticate later with: gh auth login"
        fi
    else
        info "Skipped. Authenticate later with: gh auth login"
    fi
fi

#===============================================================================
# Voice Transcription (whisper.cpp + ffmpeg)
#
# This is a HARD REQUIREMENT. If whisper.cpp fails to build or the model
# fails to download, the installer will exit with an error.
#===============================================================================

step "Voice Transcription Setup (whisper.cpp)..."

# Check ffmpeg - should already be installed by the system deps section above.
# For dnf systems, ffmpeg may not be in standard repos; try to install if missing.
if ! command -v ffmpeg &>/dev/null; then
    warn "ffmpeg not found. Attempting to install..."
    if [ "$PKG_MANAGER" = "apt" ]; then
        sudo apt-get install -y -qq ffmpeg
    else
        # Amazon Linux 2023 does not ship ffmpeg in standard repos; try RPM Fusion
        if ! sudo dnf install -y ffmpeg 2>/dev/null; then
            error "ffmpeg installation failed."
            error "On Amazon Linux 2023, install via RPM Fusion:"
            error "  sudo dnf install -y https://download1.rpmfusion.org/free/fedora/rpmfusion-free-release-\$(rpm -E %fedora).noarch.rpm"
            error "  sudo dnf install -y ffmpeg"
            exit 1
        fi
    fi
fi
if command -v ffmpeg &>/dev/null; then
    success "ffmpeg is available"
else
    error "ffmpeg is required for voice message transcription but could not be installed."
    exit 1
fi

# Build whisper.cpp
WHISPER_DIR="${WORKSPACE_DIR}/whisper.cpp"
if [ ! -f "$WHISPER_DIR/build/bin/whisper-cli" ]; then
    step "Building whisper.cpp (this may take a few minutes)..."
    # Ensure build tools are present (already installed via system deps, but be safe)
    if ! command -v cmake &>/dev/null; then
        error "cmake is required to build whisper.cpp but is not installed."
        error "Install it with: sudo apt-get install -y cmake build-essential"
        exit 1
    fi
    mkdir -p "$(dirname "$WHISPER_DIR")"
    if [ ! -d "$WHISPER_DIR" ]; then
        info "Cloning whisper.cpp..."
        git clone --quiet https://github.com/ggerganov/whisper.cpp.git "$WHISPER_DIR"
    fi
    cd "$WHISPER_DIR"
    cmake -B build -DCMAKE_BUILD_TYPE=Release -DWHISPER_BUILD_TESTS=OFF -DWHISPER_BUILD_EXAMPLES=ON 2>&1 | tail -5
    cmake --build build -j"$(nproc)" 2>&1 | tail -10
    cd "$INSTALL_DIR"
    if [ -f "$WHISPER_DIR/build/bin/whisper-cli" ]; then
        success "whisper.cpp built successfully"
    else
        error "whisper.cpp build failed. Voice transcription is a hard requirement."
        error "Check build output above and ensure build-essential/cmake/gcc are installed."
        exit 1
    fi
else
    success "whisper.cpp already built"
fi

# Download whisper small model (~465MB)
if [ ! -f "$WHISPER_DIR/models/ggml-small.bin" ]; then
    step "Downloading whisper small model (~465MB)..."
    if [ -f "$WHISPER_DIR/models/download-ggml-model.sh" ]; then
        bash "$WHISPER_DIR/models/download-ggml-model.sh" small
        if [ -f "$WHISPER_DIR/models/ggml-small.bin" ]; then
            success "Whisper small model downloaded"
        else
            error "Whisper model download failed."
            error "Try manually: bash $WHISPER_DIR/models/download-ggml-model.sh small"
            exit 1
        fi
    else
        error "Whisper model download script not found at $WHISPER_DIR/models/download-ggml-model.sh"
        error "Ensure whisper.cpp was cloned correctly."
        exit 1
    fi
else
    success "Whisper small model already present"
fi

# Verify the full pipeline works
info "Verifying whisper.cpp transcription pipeline..."
if "$WHISPER_DIR/build/bin/whisper-cli" --help &>/dev/null 2>&1; then
    success "whisper-cli binary verified"
else
    error "whisper-cli binary does not execute correctly."
    exit 1
fi

#===============================================================================
# Authentication Method (OAuth-first)
#===============================================================================

step "Setting up Claude authentication..."

AUTH_METHOD=""

# If we already detected a valid OAuth session earlier, skip the prompt
if [ "$EXISTING_OAUTH" = true ]; then
    AUTH_METHOD="oauth"
    success "Using existing OAuth session"
elif [ -n "${ANTHROPIC_API_KEY:-}" ]; then
    # API key was provided via environment variable before install started
    AUTH_METHOD="apikey"
    success "Using ANTHROPIC_API_KEY from environment"
elif [ "$NON_INTERACTIVE" = true ]; then
    warn "Skipping Claude authentication (non-interactive mode)."
    info "Run the installer again without --non-interactive to configure authentication."
    AUTH_METHOD="skipped"
else
    # Ask the user which auth method they prefer
    echo ""
    echo -e "${BOLD}Claude Authentication${NC}"
    echo ""
    echo "Do you have a Claude Pro or Max subscription?"
    echo -e "Using OAuth with your subscription is recommended ${GREEN}(saves money vs API key).${NC}"
    echo ""
    echo "  1) Yes, I have a subscription - use OAuth (recommended)"
    echo "  2) No, I'll use an API key"
    echo ""

    while true; do
        read -p "Choose [1/2]: " AUTH_CHOICE
        case "$AUTH_CHOICE" in
            1)
                AUTH_METHOD="oauth"
                break
                ;;
            2)
                AUTH_METHOD="apikey"
                break
                ;;
            *)
                warn "Please enter 1 or 2"
                ;;
        esac
    done
fi

# --- OAuth path ---
if [ "$AUTH_METHOD" = "oauth" ] && [ "$EXISTING_OAUTH" != true ]; then
    echo ""
    info "Starting OAuth authentication..."
    echo ""

    # Detect headless environment
    IS_HEADLESS=false
    if [ -z "${DISPLAY:-}" ] && [ -z "${WAYLAND_DISPLAY:-}" ] && ! command -v open &>/dev/null; then
        IS_HEADLESS=true
        echo -e "${YELLOW}Headless server detected (no display).${NC}"
        echo ""
        echo "For headless authentication, we recommend using 'claude setup-token'."
        echo "It will display a URL — open it in any browser (phone, laptop, etc.),"
        echo "authorize, then paste the code back here when prompted."
        echo ""
        echo -e "Alternatively, you can use an ${BOLD}API key${NC} instead (billed per-token)."
        echo ""
        echo "  1) Try setup-token (OAuth via URL + code paste)"
        echo "  2) Use an API key instead"
        echo ""
        read -p "Choose [1/2]: " HEADLESS_CHOICE
        if [ "$HEADLESS_CHOICE" = "2" ]; then
            AUTH_METHOD="apikey_fallback"
        fi
    fi

    if [ "$AUTH_METHOD" = "oauth" ]; then
        echo ""
        echo "Claude Code will generate an authentication URL."
        echo -e "Open it in ${BOLD}any browser${NC} (phone, laptop, etc.) and sign in with your Anthropic account."
        echo ""
        read -p "Press Enter to continue..."
        echo ""

        if [ "$IS_HEADLESS" = true ]; then
            # --- Headless path: setup-token ---
            # Two issues with setup-token inside a bash script:
            # 1. It needs a pseudo-TTY (uses Ink/React-for-CLI which requires raw mode).
            #    Fix: `script -qc` provides a pseudo-TTY.
            # 2. It does NOT save credentials to ~/.claude/.credentials.json by design.
            #    It only outputs a long-lived OAuth token to stdout.
            #    See: https://github.com/anthropics/claude-code/issues/19274
            #    Fix: Capture the token and persist it to config.env.

            SETUP_TMPFILE=$(mktemp)
            # Clean up temp file if the script is killed mid-auth
            trap 'rm -f "$SETUP_TMPFILE"' EXIT INT TERM
            info "Running 'claude setup-token' with pseudo-TTY (via 'script')..."
            echo ""

            # Run setup-token inside a pseudo-TTY so Ink's raw mode works.
            # The 'script' command records all terminal output to SETUP_TMPFILE.
            script -qc "claude setup-token" "$SETUP_TMPFILE"
            SETUP_EXIT=$?

            # Extract the OAuth token from setup-token output.
            # Token format: sk-ant-oat01-<base64-chars>
            # Strip ANSI escape codes first, then grep for the token.
            CAPTURED_TOKEN=""
            if [ -f "$SETUP_TMPFILE" ]; then
                CAPTURED_TOKEN=$(sed 's/\x1b\[[0-9;]*[a-zA-Z]//g' "$SETUP_TMPFILE" \
                    | grep -oP 'sk-ant-oat01-[A-Za-z0-9_-]+' | head -1)
                rm -f "$SETUP_TMPFILE"
            fi

            if [ -n "$CAPTURED_TOKEN" ]; then
                # Save the token so Claude Code can use it at runtime
                export CLAUDE_CODE_OAUTH_TOKEN="$CAPTURED_TOKEN"

                # Persist to config.env so systemd service picks it up
                if [ -f "$CONFIG_FILE" ]; then
                    echo "" >> "$CONFIG_FILE"
                    echo "# OAuth token from claude setup-token (long-lived)" >> "$CONFIG_FILE"
                    echo "export CLAUDE_CODE_OAUTH_TOKEN=$CAPTURED_TOKEN" >> "$CONFIG_FILE"
                fi

                success "OAuth token captured and saved to config.env!"
            else
                # Token extraction failed — fall back to manual paste
                warn "Could not automatically extract the OAuth token from setup-token output."
                echo ""
                echo "If setup-token displayed a token (starts with sk-ant-oat01-...), paste it now."
                echo "If it failed entirely, press Enter to fall back to API key."
                echo ""
                read -p "Paste token (or Enter to skip): " MANUAL_TOKEN

                if [[ "$MANUAL_TOKEN" == sk-ant-* ]]; then
                    CAPTURED_TOKEN="$MANUAL_TOKEN"
                    export CLAUDE_CODE_OAUTH_TOKEN="$CAPTURED_TOKEN"
                    if [ -f "$CONFIG_FILE" ]; then
                        echo "" >> "$CONFIG_FILE"
                        echo "# OAuth token from claude setup-token (long-lived, manually pasted)" >> "$CONFIG_FILE"
                        echo "export CLAUDE_CODE_OAUTH_TOKEN=$CAPTURED_TOKEN" >> "$CONFIG_FILE"
                    fi
                    success "OAuth token saved to config.env!"
                else
                    warn "No valid token provided."
                    echo "Falling back to API key..."
                    AUTH_METHOD="apikey_fallback"
                fi
            fi

            # Verify auth works if we got a token
            if [ "$AUTH_METHOD" = "oauth" ] && [ -n "${CAPTURED_TOKEN:-}" ]; then
                if claude --print -p "ping" --max-turns 1 &>/dev/null 2>&1; then
                    success "OAuth authentication verified!"
                else
                    warn "Token was saved but API verification failed."
                    warn "The token may need a moment to activate, or the OAuth flow didn't complete."
                    echo ""
                    echo "Falling back to API key..."
                    AUTH_METHOD="apikey_fallback"
                fi
            fi
        else
            # --- Non-headless path: auth login ---
            # auth login saves credentials to ~/.claude/.credentials.json automatically,
            # but still needs a pseudo-TTY when run inside a script (Ink/raw mode).
            if script -qc "claude auth login" /dev/null; then
                if claude --print -p "ping" --max-turns 1 &>/dev/null 2>&1; then
                    success "OAuth authentication successful (verified)!"
                else
                    warn "Auth command completed but API verification failed."
                    warn "The token may have expired or the code exchange didn't complete."
                    echo ""
                    echo "Falling back to API key..."
                    AUTH_METHOD="apikey_fallback"
                fi
            else
                warn "OAuth authentication failed or was cancelled."
                echo ""
                echo "Falling back to API key..."
                AUTH_METHOD="apikey_fallback"
            fi
        fi
    fi
fi

# --- API key path (chosen directly or as fallback from OAuth) ---
if [ "$AUTH_METHOD" = "apikey" ] || [ "$AUTH_METHOD" = "apikey_fallback" ]; then
    if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
        echo ""
        echo -e "${BOLD}Anthropic API Key${NC}"
        echo ""
        echo "Get one from: https://console.anthropic.com/settings/keys"
        echo ""
        if [ "$AUTH_METHOD" = "apikey" ]; then
            echo -e "${YELLOW}Note: API key usage is billed per-token. A Claude Pro/Max subscription${NC}"
            echo -e "${YELLOW}      would be more cost-effective for regular use.${NC}"
            echo ""
        fi

        while true; do
            read -p "Enter your Anthropic API key: " API_KEY
            if [ -n "$API_KEY" ]; then
                export ANTHROPIC_API_KEY="$API_KEY"
                break
            else
                warn "API key is required for this auth method."
                echo ""
                echo "  1) Enter an API key"
                echo "  2) Go back and try OAuth instead"
                echo ""
                read -p "Choose [1/2]: " RETRY_CHOICE
                if [ "$RETRY_CHOICE" = "2" ]; then
                    AUTH_METHOD="oauth"
                    echo ""
                    info "Starting OAuth authentication..."
                    echo ""
                    read -p "Press Enter to continue..."
                    echo ""
                    if script -qc "claude auth login" /dev/null && claude auth status &>/dev/null 2>&1; then
                        success "OAuth authentication successful!"
                    else
                        error "OAuth also failed. Cannot proceed without authentication."
                        echo ""
                        echo "Please authenticate manually and re-run the installer:"
                        echo -e "  ${CYAN}claude auth login${NC}"
                        echo "  or"
                        echo -e "  ${CYAN}export ANTHROPIC_API_KEY=your_key_here${NC}"
                        exit 1
                    fi
                    break
                fi
            fi
        done

        # Save API key to config.env if we got one
        if [ -n "${ANTHROPIC_API_KEY:-}" ] && [ -f "$CONFIG_FILE" ]; then
            echo "" >> "$CONFIG_FILE"
            echo "# Anthropic API Key (per-token billing)" >> "$CONFIG_FILE"
            echo "ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY" >> "$CONFIG_FILE"
        fi
    fi
fi

# --- Make launchers executable ---
# start-claude.sh dispatches to claude-persistent.sh (normal) or
# claude-wrapper.exp (debug) at runtime based on LOBSTER_DEBUG.
# No selection needed here; just ensure all scripts are executable.

for script in \
    "$INSTALL_DIR/scripts/start-claude.sh" \
    "$INSTALL_DIR/scripts/claude-persistent.sh" \
    "$INSTALL_DIR/scripts/claude-wrapper.exp"; do
    [ -f "$script" ] && chmod +x "$script" || true
done
success "Claude launchers ready (start-claude.sh, claude-persistent.sh, claude-wrapper.exp)"

#===============================================================================
# Generate Service Files from Templates
#===============================================================================

step "Generating systemd service files from templates..."

# Check that templates exist
ROUTER_TEMPLATE="$INSTALL_DIR/services/lobster-router.service.template"
CLAUDE_TEMPLATE="$INSTALL_DIR/services/lobster-claude.service.template"

if [ ! -f "$ROUTER_TEMPLATE" ]; then
    error "Router service template not found: $ROUTER_TEMPLATE"
    error "Please ensure you have the latest version of the repository."
    exit 1
fi

if [ ! -f "$CLAUDE_TEMPLATE" ]; then
    error "Claude service template not found: $CLAUDE_TEMPLATE"
    error "Please ensure you have the latest version of the repository."
    exit 1
fi

# Generate service files from templates
generate_from_template \
    "$ROUTER_TEMPLATE" \
    "$INSTALL_DIR/services/lobster-router.service"

generate_from_template \
    "$CLAUDE_TEMPLATE" \
    "$INSTALL_DIR/services/lobster-claude.service"

# Generate Slack router service if template exists
SLACK_ROUTER_TEMPLATE="$INSTALL_DIR/services/lobster-slack-router.service.template"
if [ -f "$SLACK_ROUTER_TEMPLATE" ]; then
    generate_from_template \
        "$SLACK_ROUTER_TEMPLATE" \
        "$INSTALL_DIR/services/lobster-slack-router.service"
fi

# Generate MCP HTTP bridge service if template exists
MCP_TEMPLATE="$INSTALL_DIR/services/lobster-mcp.service.template"
if [ -f "$MCP_TEMPLATE" ]; then
    generate_from_template \
        "$MCP_TEMPLATE" \
        "$INSTALL_DIR/services/lobster-mcp.service"
fi

# Generate observability server service if template exists
OBSERVABILITY_TEMPLATE="$INSTALL_DIR/services/lobster-observability.service.template"
if [ -f "$OBSERVABILITY_TEMPLATE" ]; then
    generate_from_template \
        "$OBSERVABILITY_TEMPLATE" \
        "$INSTALL_DIR/services/lobster-observability.service"
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

sudo systemctl daemon-reload

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

# Install git pre-commit hook (enforces execute bits on scripts/ and hooks/)
if [ -f "$INSTALL_DIR/hooks/pre-commit" ]; then
    cp "$INSTALL_DIR/hooks/pre-commit" "$INSTALL_DIR/.git/hooks/pre-commit"
    chmod +x "$INSTALL_DIR/.git/hooks/pre-commit"
    success "Pre-commit hook installed (.git/hooks/pre-commit)"
fi

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

# Helper: create or replace a symlink idempotently.
# If a regular file/dir exists at the link path, backs it up first.
# Usage: make_symlink <target> <link_path>
make_symlink() {
    local target="$1"
    local link="$2"
    if [ -L "$link" ]; then
        # Already a symlink -- update if it points somewhere different
        if [ "$(readlink "$link")" != "$target" ]; then
            rm "$link"
            ln -s "$target" "$link"
            info "  Updated symlink: $link -> $target"
        else
            info "  Symlink already correct: $link"
        fi
    elif [ -e "$link" ]; then
        # Regular file/dir exists -- back it up then replace with symlink
        mv "$link" "${link}.pre-symlink-backup"
        warn "  Backed up existing $(basename "$link") to ${link}.pre-symlink-backup"
        ln -s "$target" "$link"
        success "  Created symlink (replaced existing): $(basename "$link") -> $target"
    else
        ln -s "$target" "$link"
        success "  Created symlink: $(basename "$link") -> $target"
    fi
}

# CLAUDE.md: CC reads this as the system prompt, walking up from CWD.
# Symlinking ensures CC always loads the real repo CLAUDE.md, not a stale copy.
make_symlink "$INSTALL_DIR/CLAUDE.md" "$WORKSPACE_DIR/CLAUDE.md"

# .claude/: CC discovers agents at {CWD}/.claude/agents/ (no upward traversal).
# Symlinking the whole .claude/ dir exposes agents/ and any future CC-standard
# subdirs (commands/, etc.) without needing per-subdir symlinks.
make_symlink "$INSTALL_DIR/.claude" "$WORKSPACE_DIR/.claude"

success "Claude Code discovery symlinks configured"

#===============================================================================
# Apply Private Configuration Overlay
#===============================================================================

apply_private_overlay

#===============================================================================
# Run Post-Install Hook
#===============================================================================

run_hook "post-install.sh"

#===============================================================================
# Start Services
#===============================================================================

step "Starting Lobster services..."

if [ "$NON_INTERACTIVE" = true ]; then
    info "Skipping service start (non-interactive mode)."
    info "Start services manually with: lobster start"
    REPLY="n"
else
    echo ""
    read -p "Start Lobster services now? [Y/n] " -n 1 -r
    echo
fi

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
        warn "Telegram bot: not running (check logs)"
    fi

    if tmux -L lobster has-session -t lobster 2>/dev/null; then
        success "Claude session: running in tmux"
    else
        warn "Claude session: not running (check with: lobster attach)"
    fi

    # Start dashboard server if not already running
    DASHBOARD_CMD="$INSTALL_DIR/.venv/bin/python3 $INSTALL_DIR/src/dashboard/server.py --host 0.0.0.0 --port 9100"
    if ss -tlnp | grep -q 9100; then
        success "Dashboard server: already running on port 9100"
    else
        info "Starting dashboard server..."
        mkdir -p "$WORKSPACE_DIR/logs"
        nohup $DASHBOARD_CMD >> "$WORKSPACE_DIR/logs/dashboard-server.log" 2>&1 &
        sleep 2
        if ss -tlnp | grep -q 9100; then
            success "Dashboard server: running on port 9100"
        else
            warn "Dashboard server: failed to start (check $WORKSPACE_DIR/logs/dashboard-server.log)"
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
echo "  $USER_CONFIG_DIR    User config and memory"
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
