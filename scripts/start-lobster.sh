#!/bin/bash
#
# Start Lobster - Always-on Claude Code session
#
# This script starts Claude Code in a tmux session with the lobster-inbox MCP server.
# Claude will run in an infinite loop, processing Telegram messages as they arrive.
#

set -e

WORKSPACE="$HOME/lobster-workspace"
SESSION_NAME="lobster"
TMUX_SOCKET="/tmp/lobster-tmux"

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

info() { echo -e "${GREEN}[INFO]${NC} $1"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; }

# Check if already running
if tmux -L lobster has-session -t "$SESSION_NAME" 2>/dev/null; then
    warn "Lobster is already running!"
    echo ""
    echo "To attach: tmux -L lobster attach -t $SESSION_NAME"
    echo "To stop:   tmux -L lobster kill-session -t $SESSION_NAME"
    exit 0
fi

# Ensure workspace exists
mkdir -p "$WORKSPACE"
cd "$WORKSPACE"

info "Starting Lobster (always-on Claude session)..."
info "Workspace: $WORKSPACE"

# Create tmux session with Claude
tmux -L lobster new-session -d -s "$SESSION_NAME" -c "$WORKSPACE" \
    "claude --dangerously-skip-permissions 2>&1 | tee -a $WORKSPACE/logs/claude-session.log"

sleep 1

if tmux -L lobster has-session -t "$SESSION_NAME" 2>/dev/null; then
    info "Lobster started successfully!"
    echo ""
    echo "  Attach to session:  tmux -L lobster attach -t $SESSION_NAME"
    echo "  View logs:          tail -f $WORKSPACE/logs/claude-session.log"
    echo "  Stop Lobster:       tmux -L lobster kill-session -t $SESSION_NAME"
    echo ""
    info "Claude is now waiting for messages. Send a Telegram message to interact."
else
    error "Failed to start Lobster tmux session"
    exit 1
fi
