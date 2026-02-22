#!/bin/bash
#
# Start Lobster - Persistent Claude Code session with lifecycle management
#
# This script starts the persistent Claude wrapper in a tmux session.
# Claude will run in a wait_for_messages() loop, processing Telegram messages
# as they arrive, and supporting clean hibernation/restart cycles.
#
# Prefer using systemd for production: sudo systemctl start lobster-claude
# This script is for manual/dev use.
#

set -e

WORKSPACE="${LOBSTER_WORKSPACE:-$HOME/lobster-workspace}"
INSTALL_DIR="${LOBSTER_INSTALL_DIR:-$HOME/lobster}"
SESSION_NAME="lobster"

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

# Ensure workspace and log dirs exist
mkdir -p "$WORKSPACE" "$WORKSPACE/logs"
cd "$WORKSPACE"

info "Starting Lobster (persistent Claude session)..."
info "Workspace: $WORKSPACE"
info "Launcher:  $INSTALL_DIR/scripts/claude-persistent.sh"

# Create tmux session with persistent wrapper
tmux -L lobster new-session -d -s "$SESSION_NAME" -c "$WORKSPACE" \
    "$INSTALL_DIR/scripts/claude-persistent.sh"

sleep 2

if tmux -L lobster has-session -t "$SESSION_NAME" 2>/dev/null; then
    info "Lobster started successfully!"

    # Start dashboard server in a second tmux window if not already running
    DASHBOARD_CMD="$INSTALL_DIR/.venv/bin/python3 $INSTALL_DIR/src/dashboard/server.py --host 0.0.0.0 --port 9100"
    if ss -tlnp | grep -q 9100; then
        info "Dashboard server already running on port 9100"
    else
        info "Starting dashboard server..."
        tmux -L lobster new-window -t "$SESSION_NAME" -n "dashboard" "$DASHBOARD_CMD"
        info "Dashboard server started on port 9100"
    fi

    echo ""
    echo "  Attach to session:  tmux -L lobster attach -t $SESSION_NAME"
    echo "  View logs:          tail -f $WORKSPACE/logs/claude-persistent.log"
    echo "  View Claude log:    tail -f $WORKSPACE/logs/claude-session.log"
    echo "  Stop Lobster:       tmux -L lobster kill-session -t $SESSION_NAME"
    echo ""
    info "Claude will start and call wait_for_messages() to listen for Telegram messages."
else
    error "Failed to start Lobster tmux session"
    exit 1
fi
