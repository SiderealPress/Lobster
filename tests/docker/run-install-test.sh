#!/usr/bin/env bash
# run-install-test.sh
#
# Runs inside the Dockerfile.test container to verify that install.sh works
# end-to-end in a clean Debian environment.
#
# What this tests:
#   - install.sh --non-interactive exits 0 with pre-copied source
#   - lobster CLI is installed at /usr/local/bin/lobster and responds
#   - Required runtime directories are created
#   - Core Python packages (mcp, telegram) are importable
#
# What this does NOT test:
#   - Claude Code authentication (requires real credentials)
#   - systemd services (not available in Docker)
#   - Telegram bot connectivity (requires real token)

set -euo pipefail

INSTALL_DIR=/home/testuser/lobster
VENV="$INSTALL_DIR/.venv"

echo "=== Testing Lobster Installation ==="
echo ""

# Run the real installer in non-interactive mode.
# LOBSTER_INSTALL_DIR points to the pre-copied source so the installer
# skips the git clone step (source is already present, non-interactive mode).
echo "[STEP] Running install.sh --non-interactive..."
LOBSTER_INSTALL_DIR="$INSTALL_DIR" bash "$INSTALL_DIR/install.sh" --non-interactive
echo ""

echo "=== Verifying post-install state ==="
echo ""

# 1. lobster CLI must be installed and executable
if [ ! -x /usr/local/bin/lobster ]; then
    echo "FAIL: /usr/local/bin/lobster not found or not executable"
    exit 1
fi
echo "OK: lobster CLI installed at /usr/local/bin/lobster"

# 2. lobster help must exit 0
if ! lobster help >/dev/null 2>&1; then
    echo "FAIL: 'lobster help' exited non-zero"
    exit 1
fi
echo "OK: 'lobster help' runs without error"

# 3. Runtime directories
for required_dir in \
    /home/testuser/messages/inbox \
    /home/testuser/messages/outbox \
    /home/testuser/messages/processing \
    /home/testuser/lobster-workspace/logs
do
    if [ ! -d "$required_dir" ]; then
        echo "FAIL: required directory not created: $required_dir"
        exit 1
    fi
done
echo "OK: runtime directories created"

# 4. Python venv and core imports
if [ ! -f "$VENV/bin/python" ]; then
    echo "FAIL: Python venv not created at $VENV"
    exit 1
fi

"$VENV/bin/python" -c "from mcp.server import Server" 2>/dev/null || {
    echo "FAIL: mcp.server not importable"
    exit 1
}
echo "OK: MCP import OK"

"$VENV/bin/python" -c "from telegram import Bot" 2>/dev/null || {
    echo "FAIL: python-telegram-bot not importable"
    exit 1
}
echo "OK: Telegram import OK"

echo ""
echo "=== Installation Test PASSED ==="
