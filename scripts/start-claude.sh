#!/bin/bash
#===============================================================================
# Lobster Claude Launcher
#
# Reads LOBSTER_DEBUG from config.env and dispatches to the appropriate runner:
#
#   LOBSTER_DEBUG=false (default):
#     claude-persistent.sh — headless claude -p, lifecycle-managed, hibernation
#     support. lobster attach is read-only (Claude owns the terminal).
#
#   LOBSTER_DEBUG=true:
#     claude-wrapper.exp — interactive Claude REPL via expect. lobster attach
#     gives a full interactive session where you can type commands.
#     Health check suppresses the "old-style mode" warning in this mode.
#
# This script is called by the lobster-claude systemd service via tmux.
#===============================================================================

set -uo pipefail

INSTALL_DIR="${LOBSTER_INSTALL_DIR:-$HOME/lobster}"
CONFIG_DIR="${LOBSTER_CONFIG_DIR:-$HOME/lobster-config}"

# Source config to pick up LOBSTER_DEBUG (may already be in environment from
# the EnvironmentFile directives in the service unit, but source here too so
# this script works when run standalone).
if [[ -f "$CONFIG_DIR/config.env" ]]; then
    # shellcheck source=/dev/null
    set -o allexport
    source "$CONFIG_DIR/config.env"
    set +o allexport
fi

# Fallback: also try the repo's own config.env (lower priority)
if [[ -f "$INSTALL_DIR/config/config.env" ]]; then
    # Only set LOBSTER_DEBUG if not already set
    if [[ -z "${LOBSTER_DEBUG:-}" ]]; then
        # shellcheck source=/dev/null
        LOBSTER_DEBUG=$(grep '^LOBSTER_DEBUG=' "$INSTALL_DIR/config/config.env" 2>/dev/null | cut -d'=' -f2- | tr -d '[:space:]"' || echo "false")
    fi
fi

LOBSTER_DEBUG="${LOBSTER_DEBUG:-false}"

if [[ "$LOBSTER_DEBUG" == "true" ]]; then
    echo "[start-claude] LOBSTER_DEBUG=true — launching interactive REPL (claude-wrapper.exp)"
    exec "$INSTALL_DIR/scripts/claude-wrapper.exp"
else
    echo "[start-claude] LOBSTER_DEBUG=false — launching persistent session (claude-persistent.sh)"
    exec "$INSTALL_DIR/scripts/claude-persistent.sh"
fi
