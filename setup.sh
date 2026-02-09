#!/bin/bash
#===============================================================================
# Lobster Agent Hub Setup Script
# A secure personal hub server for hosting Claude Code with messaging integrations
#
# Optimized for: tmux + zsh power users
#
# Features:
# - Claude Code (latest native binary, always-on session)
# - Telegram Bot (send/receive messages via Claude)
# - Voice message transcription (OpenAI Whisper API)
# - Security hardening (firewall, fail2ban, SSH keys only)
# - Full zsh + oh-my-zsh + powerlevel10k setup
# - Advanced tmux configuration with agent-specific layouts
#
# Future: Signal, Twilio SMS (see docs/FUTURE.md)
#
# Usage: bash lobster-agent-hub-setup.sh
#===============================================================================

set -e  # Exit on error

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

print_header() {
    echo -e "\n${BLUE}═══════════════════════════════════════════════════════════════${NC}"
    echo -e "${BLUE}  $1${NC}"
    echo -e "${BLUE}═══════════════════════════════════════════════════════════════${NC}\n"
}

print_step() {
    echo -e "${GREEN}▶ $1${NC}"
}

print_warning() {
    echo -e "${YELLOW}⚠ $1${NC}"
}

print_error() {
    echo -e "${RED}✖ $1${NC}"
}

print_success() {
    echo -e "${GREEN}✔ $1${NC}"
}

#-------------------------------------------------------------------------------
# System Update and Base Dependencies
#-------------------------------------------------------------------------------
print_header "Step 1: System Update and Base Dependencies"

print_step "Updating system packages (this may take a few minutes)..."
sudo apt update && sudo apt upgrade -y
print_success "System updated"

print_step "Installing essential packages (20+ packages)..."
sudo apt install -y \
    curl \
    wget \
    git \
    build-essential \
    tmux \
    zsh \
    htop \
    tree \
    jq \
    unzip \
    ripgrep \
    fd-find \
    bat \
    fzf \
    ca-certificates \
    gnupg \
    lsb-release \
    python3 \
    python3-pip \
    python3-venv \
    ufw \
    fail2ban \
    fontconfig \
    cron
print_success "Essential packages installed"

print_step "Enabling cron service..."
sudo systemctl enable cron 2>/dev/null || true
sudo systemctl start cron 2>/dev/null || true
print_success "Cron service enabled"

#-------------------------------------------------------------------------------
# Zsh + Oh-My-Zsh Setup
#-------------------------------------------------------------------------------
print_header "Step 2: Installing Zsh + Oh-My-Zsh"

print_step "Installing Oh-My-Zsh (downloading from GitHub)..."
if [ ! -d "$HOME/.oh-my-zsh" ]; then
    sh -c "$(curl -fsSL https://raw.githubusercontent.com/ohmyzsh/ohmyzsh/master/tools/install.sh)" "" --unattended
fi

print_step "Installing Powerlevel10k theme..."
git clone --depth=1 https://github.com/romkatv/powerlevel10k.git \
    ${ZSH_CUSTOM:-$HOME/.oh-my-zsh/custom}/themes/powerlevel10k 2>/dev/null || true

print_step "Installing zsh plugins (4 plugins from GitHub)..."
# zsh-autosuggestions
echo "  - zsh-autosuggestions"
git clone https://github.com/zsh-users/zsh-autosuggestions \
    ${ZSH_CUSTOM:-~/.oh-my-zsh/custom}/plugins/zsh-autosuggestions 2>/dev/null || true

# zsh-syntax-highlighting
echo "  - zsh-syntax-highlighting"
git clone https://github.com/zsh-users/zsh-syntax-highlighting.git \
    ${ZSH_CUSTOM:-~/.oh-my-zsh/custom}/plugins/zsh-syntax-highlighting 2>/dev/null || true

# zsh-completions
echo "  - zsh-completions"
git clone https://github.com/zsh-users/zsh-completions \
    ${ZSH_CUSTOM:-~/.oh-my-zsh/custom}/plugins/zsh-completions 2>/dev/null || true

# fzf-tab (better tab completion with fzf)
echo "  - fzf-tab"
git clone https://github.com/Aloxaf/fzf-tab \
    ${ZSH_CUSTOM:-~/.oh-my-zsh/custom}/plugins/fzf-tab 2>/dev/null || true
print_success "Zsh plugins installed"

#-------------------------------------------------------------------------------
# Create Optimized .zshrc
#-------------------------------------------------------------------------------
print_step "Creating optimized .zshrc..."

cat > ~/.zshrc << 'ZSHRC'
#===============================================================================
# Lobster Agent Hub - Zsh Configuration
#===============================================================================

# Powerlevel10k instant prompt (keep at top)
if [[ -r "${XDG_CACHE_HOME:-$HOME/.cache}/p10k-instant-prompt-${(%):-%n}.zsh" ]]; then
  source "${XDG_CACHE_HOME:-$HOME/.cache}/p10k-instant-prompt-${(%):-%n}.zsh"
fi

# Path to oh-my-zsh
export ZSH="$HOME/.oh-my-zsh"

# Theme
ZSH_THEME="powerlevel10k/powerlevel10k"

# Plugins
plugins=(
    git
    tmux
    docker
    npm
    python
    pip
    sudo
    history
    command-not-found
    zsh-autosuggestions
    zsh-syntax-highlighting
    zsh-completions
    fzf-tab
    colored-man-pages
)

# Tmux auto-start (disabled by default, enable if desired)
# ZSH_TMUX_AUTOSTART=true
ZSH_TMUX_AUTOCONNECT=false

source $ZSH/oh-my-zsh.sh

#-------------------------------------------------------------------------------
# Environment Variables
#-------------------------------------------------------------------------------

# Path additions
export PATH="$HOME/.local/bin:$PATH"
export PATH="$HOME/.npm-global/bin:$PATH"

# Editor
export EDITOR='nano'
export VISUAL='nano'

# History
HISTSIZE=50000
SAVEHIST=50000
setopt EXTENDED_HISTORY
setopt HIST_EXPIRE_DUPS_FIRST
setopt HIST_IGNORE_DUPS
setopt HIST_IGNORE_SPACE
setopt HIST_VERIFY
setopt SHARE_HISTORY

# FZF configuration
export FZF_DEFAULT_OPTS='
  --height 40%
  --layout=reverse
  --border
  --color=fg:#c0caf5,bg:#1a1b26,hl:#bb9af7
  --color=fg+:#c0caf5,bg+:#292e42,hl+:#7dcfff
  --color=info:#7aa2f7,prompt:#7dcfff,pointer:#7dcfff
  --color=marker:#9ece6a,spinner:#9ece6a,header:#9ece6a
'
export FZF_DEFAULT_COMMAND='fdfind --type f --hidden --follow --exclude .git'
export FZF_CTRL_T_COMMAND="$FZF_DEFAULT_COMMAND"

# Bat (better cat)
export BAT_THEME="TwoDark"
alias cat='batcat --paging=never'
alias catp='batcat'

#-------------------------------------------------------------------------------
# Aliases - General
#-------------------------------------------------------------------------------

# Navigation
alias ..='cd ..'
alias ...='cd ../..'
alias ....='cd ../../..'
alias ~='cd ~'
alias -- -='cd -'

# List
alias ll='ls -alFh --color=auto'
alias la='ls -A --color=auto'
alias l='ls -CF --color=auto'

# Safety
alias rm='rm -i'
alias cp='cp -i'
alias mv='mv -i'

# Grep
alias grep='grep --color=auto'
alias fgrep='fgrep --color=auto'
alias egrep='egrep --color=auto'

#-------------------------------------------------------------------------------
# Aliases - Agent Hub
#-------------------------------------------------------------------------------

# Claude Code
alias c='claude'
alias cc='claude --continue'
alias cr='claude --resume'
alias cm='claude mcp'
alias cml='claude mcp list'

# Tmux agent sessions
alias ta='tmux attach -t'
alias tl='tmux list-sessions'
alias tk='tmux kill-session -t'
alias tn='tmux new-session -s'

# Quick agent start
alias agent='~/start-agent.sh'
alias agent-new='~/start-agent.sh agent-$(date +%H%M)'
alias agent-dev='~/start-agent.sh dev dev'
alias agent-mon='~/start-agent.sh mon monitor'

# Status
alias status='~/check-status.sh'

#-------------------------------------------------------------------------------
# Aliases - Tmux
#-------------------------------------------------------------------------------

alias t='tmux'
alias tks='tmux kill-server'
alias tns='tmux new-session -s'
alias tls='tmux list-sessions'

#-------------------------------------------------------------------------------
# Functions
#-------------------------------------------------------------------------------

# Quick directory creation and cd
mkcd() {
    mkdir -p "$1" && cd "$1"
}

# Find and open in editor
fe() {
    local file
    file=$(fzf --query="$1" --select-1 --exit-0)
    [ -n "$file" ] && ${EDITOR:-nano} "$file"
}

# Kill process with fzf
fkill() {
    local pid
    pid=$(ps -ef | sed 1d | fzf -m | awk '{print $2}')
    if [ "x$pid" != "x" ]; then
        echo $pid | xargs kill -${1:-9}
    fi
}

# Attach or create tmux session
tma() {
    local session="${1:-main}"
    tmux attach -t "$session" 2>/dev/null || tmux new-session -s "$session"
}

#-------------------------------------------------------------------------------
# Key Bindings
#-------------------------------------------------------------------------------

# FZF key bindings
[ -f /usr/share/doc/fzf/examples/key-bindings.zsh ] && source /usr/share/doc/fzf/examples/key-bindings.zsh

# Edit command in editor
autoload -U edit-command-line
zle -N edit-command-line
bindkey '^x^e' edit-command-line

#-------------------------------------------------------------------------------
# Powerlevel10k Configuration
#-------------------------------------------------------------------------------

[[ ! -f ~/.p10k.zsh ]] || source ~/.p10k.zsh

# Welcome message
if [[ $- == *i* ]]; then
    echo ""
    echo "🤖 Lobster Agent Hub"
    echo "   Type 'agent' to start a Claude session"
    echo "   Type 'status' to check system health"
    echo ""
fi
ZSHRC

#-------------------------------------------------------------------------------
# Create Powerlevel10k Config (Server-optimized)
#-------------------------------------------------------------------------------
print_step "Creating Powerlevel10k configuration..."

cat > ~/.p10k.zsh << 'P10K'
# Minimal Powerlevel10k config for servers
'builtin' 'local' '-a' 'p10k_config_opts'
[[ ! -o 'aliases'         ]] || p10k_config_opts+=('aliases')
[[ ! -o 'sh_glob'         ]] || p10k_config_opts+=('sh_glob')
[[ ! -o 'no_brace_expand' ]] || p10k_config_opts+=('no_brace_expand')
'builtin' 'setopt' 'no_aliases' 'no_sh_glob' 'brace_expand'

() {
  emulate -L zsh -o extended_glob

  unset -m '(POWERLEVEL9K_*|DEFAULT_USER)~POWERLEVEL9K_GITSTATUS_DIR'

  # Left prompt
  typeset -g POWERLEVEL9K_LEFT_PROMPT_ELEMENTS=(
    dir
    vcs
    newline
    prompt_char
  )

  # Right prompt
  typeset -g POWERLEVEL9K_RIGHT_PROMPT_ELEMENTS=(
    status
    command_execution_time
    background_jobs
    virtualenv
    time
  )

  # Basic settings
  typeset -g POWERLEVEL9K_MODE=ascii
  typeset -g POWERLEVEL9K_PROMPT_ON_NEWLINE=true

  # Colors
  typeset -g POWERLEVEL9K_DIR_FOREGROUND=39
  typeset -g POWERLEVEL9K_VCS_CLEAN_FOREGROUND=green
  typeset -g POWERLEVEL9K_VCS_MODIFIED_FOREGROUND=yellow

  # Prompt char
  typeset -g POWERLEVEL9K_PROMPT_CHAR_OK_{VIINS,VICMD,VIVIS,VIOWR}_FOREGROUND=green
  typeset -g POWERLEVEL9K_PROMPT_CHAR_ERROR_{VIINS,VICMD,VIVIS,VIOWR}_FOREGROUND=red
  typeset -g POWERLEVEL9K_PROMPT_CHAR_{OK,ERROR}_VIINS_CONTENT_EXPANSION='>'
  typeset -g POWERLEVEL9K_PROMPT_CHAR_{OK,ERROR}_VICMD_CONTENT_EXPANSION='<'

  # Directory
  typeset -g POWERLEVEL9K_SHORTEN_STRATEGY=truncate_to_unique
  typeset -g POWERLEVEL9K_SHORTEN_DIR_LENGTH=3

  # Time
  typeset -g POWERLEVEL9K_TIME_FORMAT='%D{%H:%M}'
  typeset -g POWERLEVEL9K_TIME_FOREGROUND=240

  # Command execution time
  typeset -g POWERLEVEL9K_COMMAND_EXECUTION_TIME_THRESHOLD=3
  typeset -g POWERLEVEL9K_COMMAND_EXECUTION_TIME_FOREGROUND=yellow

  # Instant prompt
  typeset -g POWERLEVEL9K_INSTANT_PROMPT=quiet

  (( ${#p10k_config_opts} )) && setopt ${p10k_config_opts[@]}
  'builtin' 'unset' 'p10k_config_opts'
}
P10K

#-------------------------------------------------------------------------------
# Advanced Tmux Configuration
#-------------------------------------------------------------------------------
print_header "Step 3: Creating Advanced Tmux Configuration"

cat > ~/.tmux.conf << 'TMUX'
#===============================================================================
# Lobster Agent Hub - Tmux Configuration
# Optimized for Claude Code agent sessions
#===============================================================================

#-------------------------------------------------------------------------------
# General Settings
#-------------------------------------------------------------------------------

# Modern terminal
set -g default-terminal "tmux-256color"
set -ag terminal-overrides ",xterm-256color:RGB"

# Use zsh
set -g default-shell /usr/bin/zsh

# Start windows and panes at 1
set -g base-index 1
setw -g pane-base-index 1

# Renumber windows
set -g renumber-windows on

# Big history
set -g history-limit 50000

# Fast escape
set -s escape-time 0

# Mouse support
set -g mouse on

# Focus events
set -g focus-events on

# Activity
setw -g monitor-activity on
set -g visual-activity off

#-------------------------------------------------------------------------------
# Key Bindings
#-------------------------------------------------------------------------------

# Prefix: Ctrl-a
unbind C-b
set -g prefix C-a
bind C-a send-prefix

# Reload config
bind r source-file ~/.tmux.conf \; display "Config reloaded!"

# Split panes
bind | split-window -h -c "#{pane_current_path}"
bind - split-window -v -c "#{pane_current_path}"
bind \\ split-window -h -c "#{pane_current_path}"
unbind '"'
unbind %

# New window in current path
bind c new-window -c "#{pane_current_path}"

# Vim-style pane navigation
bind h select-pane -L
bind j select-pane -D
bind k select-pane -U
bind l select-pane -R

# Alt+arrow pane navigation (no prefix)
bind -n M-Left select-pane -L
bind -n M-Right select-pane -R
bind -n M-Up select-pane -U
bind -n M-Down select-pane -D

# Resize panes
bind -r C-h resize-pane -L 5
bind -r C-j resize-pane -D 5
bind -r C-k resize-pane -U 5
bind -r C-l resize-pane -R 5

# Window navigation
bind -n S-Left previous-window
bind -n S-Right next-window

# Swap windows
bind -r < swap-window -t -1\; select-window -t -1
bind -r > swap-window -t +1\; select-window -t +1

# Kill without confirmation
bind x kill-pane
bind X kill-window

# Sync panes toggle
bind S setw synchronize-panes

# Vim copy mode
setw -g mode-keys vi
bind Enter copy-mode
bind -T copy-mode-vi v send -X begin-selection
bind -T copy-mode-vi y send -X copy-selection-and-cancel
bind -T copy-mode-vi Escape send -X cancel

#-------------------------------------------------------------------------------
# Agent Session Layouts (Prefix + Key)
#-------------------------------------------------------------------------------

# A = Agent layout: claude + monitoring sidebar
bind A new-window -n 'agent' \; \
    send-keys 'claude' Enter \; \
    split-window -h -p 30 \; \
    send-keys 'htop' Enter \; \
    select-pane -t 0

# D = Dev layout: main + terminal + logs
bind D new-window -n 'dev' \; \
    split-window -h -p 40 \; \
    split-window -v -p 50 \; \
    select-pane -t 0

# M = Monitoring layout
bind M new-window -n 'monitor' \; \
    send-keys 'htop' Enter \; \
    split-window -h \; \
    send-keys 'watch -n 5 ~/check-status.sh' Enter \; \
    split-window -v \; \
    send-keys 'tail -f /var/log/syslog 2>/dev/null || journalctl -f' Enter \; \
    select-pane -t 0

# C = Quick Claude (single pane)
bind C new-window -n 'claude' \; \
    send-keys 'claude' Enter

#-------------------------------------------------------------------------------
# Status Bar (Clean minimal theme)
#-------------------------------------------------------------------------------

set -g status on
set -g status-interval 5
set -g status-position bottom
set -g status-justify left
set -g status-style 'bg=#1a1b26 fg=#c0caf5'

# Left: session name
set -g status-left-length 30
set -g status-left '#[fg=#7aa2f7,bold] #S #[fg=#565f89]| '

# Right: path, time, host
set -g status-right-length 60
set -g status-right '#[fg=#565f89]| #[fg=#9ece6a]#{b:pane_current_path} #[fg=#565f89]| #[fg=#bb9af7]%H:%M #[fg=#565f89]| #[fg=#7dcfff]#H '

# Window tabs
set -g window-status-format '#[fg=#565f89] #I:#W '
set -g window-status-current-format '#[fg=#7aa2f7,bold] #I:#W* '
set -g window-status-separator ''

# Pane borders
set -g pane-border-style 'fg=#292e42'
set -g pane-active-border-style 'fg=#7aa2f7'

# Messages
set -g message-style 'fg=#7aa2f7 bg=#1a1b26 bold'

# Selection
set -g mode-style 'fg=#c0caf5 bg=#292e42'

#-------------------------------------------------------------------------------
# Plugins (TPM)
#-------------------------------------------------------------------------------

set -g @plugin 'tmux-plugins/tpm'
set -g @plugin 'tmux-plugins/tmux-sensible'
set -g @plugin 'tmux-plugins/tmux-resurrect'
set -g @plugin 'tmux-plugins/tmux-continuum'
set -g @plugin 'tmux-plugins/tmux-yank'

# Resurrect: save/restore sessions
set -g @resurrect-capture-pane-contents 'on'

# Continuum: auto-save every 15 min
set -g @continuum-restore 'on'
set -g @continuum-save-interval '15'

# Initialize TPM (keep at bottom)
run '~/.tmux/plugins/tpm/tpm || true'
TMUX

#-------------------------------------------------------------------------------
# Install Tmux Plugin Manager
#-------------------------------------------------------------------------------
print_step "Installing Tmux Plugin Manager..."
if [ ! -d "$HOME/.tmux/plugins/tpm" ]; then
    git clone https://github.com/tmux-plugins/tpm ~/.tmux/plugins/tpm
fi

#-------------------------------------------------------------------------------
# Node.js Installation
#-------------------------------------------------------------------------------
print_header "Step 4: Installing Node.js (LTS)"

print_step "Adding NodeSource repository..."
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
print_step "Installing Node.js..."
sudo apt install -y nodejs
mkdir -p ~/.npm-global
npm config set prefix '~/.npm-global'
print_success "Node.js $(node --version) installed"

#-------------------------------------------------------------------------------
# UV (Python Package Manager)
#-------------------------------------------------------------------------------
print_header "Step 5: Installing UV"

print_step "Downloading and installing UV..."
curl -LsSf https://astral.sh/uv/install.sh | sh
print_success "UV installed"

#-------------------------------------------------------------------------------
# Claude Code
#-------------------------------------------------------------------------------
print_header "Step 6: Installing Claude Code"

print_step "Downloading Claude Code binary (this may take a minute)..."
echo "  - Fetching latest version info..."
echo "  - Downloading binary (~50MB)..."
echo "  - Verifying checksum..."
echo "  - Installing shell integration..."
curl -fsSL https://claude.ai/install.sh | bash
print_success "Claude Code installed"
print_warning "Note: Run 'claude' manually after setup to authenticate"

#-------------------------------------------------------------------------------
# Security
#-------------------------------------------------------------------------------
print_header "Step 7: Security Hardening"

print_step "Configuring UFW firewall..."
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow ssh
if sudo ufw --force enable; then
    print_success "UFW firewall enabled"
else
    print_warning "UFW failed to enable (normal in containers/VMs without iptables)"
fi

print_step "Enabling fail2ban..."
if sudo systemctl enable fail2ban 2>/dev/null && sudo systemctl start fail2ban 2>/dev/null; then
    print_success "fail2ban enabled"
else
    print_warning "fail2ban not started (systemd may not be running)"
fi

print_step "Hardening SSH configuration..."
if [ -d /etc/ssh/sshd_config.d ]; then
    sudo tee /etc/ssh/sshd_config.d/hardening.conf > /dev/null << 'EOF'
PermitRootLogin no
MaxAuthTries 3
ClientAliveInterval 300
ClientAliveCountMax 2
EOF
    print_success "SSH hardening applied"
else
    print_warning "SSH config directory not found (SSH server not installed)"
fi
print_success "Security hardening complete"

#-------------------------------------------------------------------------------
# Projects Directory Structure
#-------------------------------------------------------------------------------
print_header "Step 8: Creating Projects Directory Structure"

print_step "Creating ~/projects directory tree..."
mkdir -p ~/projects/personal
mkdir -p ~/projects/business
print_success "Projects directories created:"
echo "  ~/projects/personal - Personal projects"
echo "  ~/projects/business - Business/work projects"

#-------------------------------------------------------------------------------
# Helper Scripts
#-------------------------------------------------------------------------------
print_header "Step 9: Creating Helper Scripts"

print_step "Creating start-agent.sh..."
cat > ~/start-agent.sh << 'SCRIPT'
#!/usr/bin/env zsh
SESSION="${1:-agent}"
LAYOUT="${2:-default}"

if tmux has-session -t "$SESSION" 2>/dev/null; then
    echo "📎 Attaching: $SESSION"
    tmux attach -t "$SESSION"
    exit 0
fi

echo "🚀 Creating: $SESSION ($LAYOUT)"

case "$LAYOUT" in
    dev)
        tmux new-session -d -s "$SESSION" -n 'claude'
        tmux send-keys -t "$SESSION" 'claude' Enter
        tmux split-window -h -t "$SESSION" -p 35
        tmux select-pane -t "$SESSION":0.0
        ;;
    monitor)
        tmux new-session -d -s "$SESSION" -n 'claude'
        tmux send-keys -t "$SESSION" 'claude' Enter
        tmux split-window -h -t "$SESSION" -p 30
        tmux send-keys -t "$SESSION" 'htop' Enter
        tmux split-window -v -t "$SESSION"
        tmux send-keys -t "$SESSION" 'journalctl -u lobster-* -f' Enter
        tmux select-pane -t "$SESSION":0.0
        ;;
    *)
        tmux new-session -d -s "$SESSION" -n 'claude'
        tmux send-keys -t "$SESSION" 'echo "🤖 Ready. Type: claude"' Enter
        ;;
esac

tmux attach -t "$SESSION"
SCRIPT
chmod +x ~/start-agent.sh

print_step "Creating check-status.sh..."
cat > ~/check-status.sh << 'SCRIPT'
#!/usr/bin/env zsh
echo "\n\033[1;34m══ Lobster Status ══\033[0m\n"

echo "Claude Code:"
command -v claude &>/dev/null && echo "  ✔ $(claude --version 2>/dev/null)" || echo "  ✖ Not found"

echo "\nServices:"
systemctl is-active lobster-router &>/dev/null && echo "  ✔ lobster-router" || echo "  ✖ lobster-router"
systemctl is-active lobster-daemon &>/dev/null && echo "  ✔ lobster-daemon" || echo "  ✖ lobster-daemon"

echo "\nTmux Sessions:"
tmux list-sessions 2>/dev/null || echo "  None"
echo ""
SCRIPT
chmod +x ~/check-status.sh

#-------------------------------------------------------------------------------
# README
#-------------------------------------------------------------------------------
cat > ~/README.md << 'EOF'
# 🤖 Lobster Agent Hub

## Quick Start
```zsh
agent              # Start default session
agent dev dev      # Dev layout (side terminal)
agent mon monitor  # Monitor layout (htop + logs)
```

## Tmux Keys (Prefix: Ctrl-a)
| Key | Action |
|-----|--------|
| `|` or `\` | Split vertical |
| `-` | Split horizontal |
| `h/j/k/l` | Navigate panes |
| `A` | Agent layout |
| `D` | Dev layout |
| `M` | Monitor layout |
| `C` | Quick Claude |
| `d` | Detach |
| `r` | Reload config |

## Zsh Aliases
| Alias | Command |
|-------|---------|
| `c` | claude |
| `cc` | claude --continue |
| `agent` | Start session |
| `status` | System check |
| `ta NAME` | Attach session |
| `tl` | List sessions |

## Setup
1. `agent` - Start tmux session
2. `claude` - Start Claude

## Lobster Services
```zsh
sudo systemctl start lobster   # Start all services
sudo systemctl status lobster  # Check status
journalctl -u lobster-* -f     # View logs
```

## Install Tmux Plugins
Inside tmux: `Ctrl-a I`
EOF
print_success "Helper scripts created"

#-------------------------------------------------------------------------------
# Set Zsh Default
#-------------------------------------------------------------------------------
print_header "Step 10: Setting Zsh as Default"

print_step "Changing default shell to zsh..."
sudo chsh -s $(which zsh) $(whoami)
print_success "Default shell set to zsh"

#-------------------------------------------------------------------------------
# GitHub MCP Server (Optional)
#-------------------------------------------------------------------------------
print_header "Step 11: GitHub MCP Server Configuration"

echo -e "${CYAN}The GitHub MCP server allows Lobster to access your GitHub repos, issues, and PRs.${NC}"
echo ""

# Check if GITHUB_PAT already exists in config
GITHUB_PAT=""
if [ -f "$HOME/lobster/config/config.env" ]; then
    GITHUB_PAT=$(grep "^GITHUB_PERSONAL_ACCESS_TOKEN=" "$HOME/lobster/config/config.env" 2>/dev/null | cut -d'=' -f2 | tr -d '"' | tr -d "'")
fi

if [ -n "$GITHUB_PAT" ]; then
    print_success "GitHub PAT found in config.env"
    CONFIGURE_GITHUB="y"
else
    echo -e "${YELLOW}To enable GitHub integration, you need a Personal Access Token.${NC}"
    echo "Create one at: https://github.com/settings/tokens"
    echo "Required scopes: repo, read:org, read:project"
    echo ""
    read -p "Do you want to configure GitHub integration now? [y/N] " CONFIGURE_GITHUB
fi

if [[ "$CONFIGURE_GITHUB" =~ ^[Yy]$ ]]; then
    if [ -z "$GITHUB_PAT" ]; then
        echo ""
        read -sp "Enter your GitHub Personal Access Token: " GITHUB_PAT
        echo ""

        if [ -n "$GITHUB_PAT" ]; then
            # Save to config.env
            mkdir -p "$HOME/lobster/config"
            if [ -f "$HOME/lobster/config/config.env" ]; then
                # Remove existing GITHUB_PAT line if present
                grep -v "^GITHUB_PERSONAL_ACCESS_TOKEN=" "$HOME/lobster/config/config.env" > "$HOME/lobster/config/config.env.tmp" || true
                mv "$HOME/lobster/config/config.env.tmp" "$HOME/lobster/config/config.env"
            fi
            echo "GITHUB_PERSONAL_ACCESS_TOKEN=\"$GITHUB_PAT\"" >> "$HOME/lobster/config/config.env"
            print_success "GitHub PAT saved to config.env"
        fi
    fi

    if [ -n "$GITHUB_PAT" ]; then
        print_step "Configuring GitHub MCP server..."

        # Add GitHub MCP server using the remote HTTP method
        if command -v claude &> /dev/null; then
            claude mcp add-json github "{\"type\":\"http\",\"url\":\"https://api.githubcopilot.com/mcp\",\"headers\":{\"Authorization\":\"Bearer $GITHUB_PAT\"}}" --scope user 2>/dev/null || true
            print_success "GitHub MCP server configured"
            echo "  Tools available: issues, PRs, repos, projects, actions"
        else
            print_warning "Claude CLI not found. Configure GitHub MCP manually after setup:"
            echo "  claude mcp add-json github '{\"type\":\"http\",\"url\":\"https://api.githubcopilot.com/mcp\",\"headers\":{\"Authorization\":\"Bearer YOUR_PAT\"}}'"
        fi
    fi
else
    print_warning "GitHub integration skipped. You can configure it later:"
    echo "  1. Create a PAT at https://github.com/settings/tokens"
    echo "  2. Run: claude mcp add-json github '{\"type\":\"http\",\"url\":\"https://api.githubcopilot.com/mcp\",\"headers\":{\"Authorization\":\"Bearer YOUR_PAT\"}}'"
fi

#-------------------------------------------------------------------------------
# Done
#-------------------------------------------------------------------------------
print_header "Setup Complete!"

echo -e "${GREEN}Lobster Agent Hub ready!${NC}\n"
echo "Next:"
echo "  1. ${YELLOW}exec zsh${NC} (or log out/in)"
echo "  2. ${CYAN}agent${NC} (start a Claude session)"
echo "  3. Inside tmux: ${CYAN}Ctrl-a I${NC} (install plugins)"
echo ""
echo "Docs: ~/README.md"
