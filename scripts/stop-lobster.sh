#!/bin/bash
#
# Stop Lobster - Gracefully stop the always-on Claude session
#

SESSION_NAME="lobster"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info() { echo -e "${GREEN}[INFO]${NC} $1"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }

if tmux -L lobster has-session -t "$SESSION_NAME" 2>/dev/null; then
    info "Stopping Lobster..."
    tmux -L lobster kill-session -t "$SESSION_NAME"
    info "Lobster stopped."
else
    warn "Lobster is not running."
fi
