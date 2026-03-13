#!/bin/bash
# Lobster Install Verification Script
#
# Run this inside the lobster-install-test container after install.sh completes
# to verify the key installation artifacts are in place.
#
# Usage (inside the container):
#   bash ~/lobster/tests/docker/verify-install.sh
#
# Exit code:
#   0 - all checks passed
#   1 - one or more checks failed

set -uo pipefail

PASS=0
FAIL=0
WARN=0

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
BOLD='\033[1m'
NC='\033[0m'

ok()   { echo -e "${GREEN}[PASS]${NC} $1"; PASS=$((PASS+1)); }
fail() { echo -e "${RED}[FAIL]${NC} $1"; FAIL=$((FAIL+1)); }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; WARN=$((WARN+1)); }

echo ""
echo -e "${BOLD}=== Lobster Install Verification ===${NC}"
echo ""

# --- Python and uv ---
echo -e "${BOLD}Python environment${NC}"

if command -v uv &>/dev/null; then
    ok "uv installed: $(uv --version)"
else
    fail "uv not found"
fi

VENV="$HOME/lobster/.venv"
if [ -d "$VENV" ]; then
    ok "Python venv exists: $VENV"
else
    fail "Python venv missing: $VENV"
fi

if [ -x "$VENV/bin/python" ]; then
    ok "Python in venv: $($VENV/bin/python --version)"
else
    fail "Python not executable in venv"
fi

if "$VENV/bin/python" -c "import mcp" 2>/dev/null; then
    ok "MCP package importable"
else
    fail "MCP package not importable"
fi

# --- CLI tools ---
echo ""
echo -e "${BOLD}CLI tools${NC}"

for tool in git gh jq curl; do
    if command -v "$tool" &>/dev/null; then
        ok "$tool: $(command -v "$tool")"
    else
        fail "$tool not found"
    fi
done

if command -v claude &>/dev/null; then
    ok "claude: $(claude --version 2>/dev/null | head -1 || echo '(version unknown)')"
else
    fail "claude not found"
fi

if command -v lobster &>/dev/null; then
    ok "lobster CLI: $(command -v lobster)"
else
    fail "lobster CLI not found at /usr/local/bin/lobster"
fi

# --- Directory structure ---
echo ""
echo -e "${BOLD}Directory structure${NC}"

WORKSPACE="${LOBSTER_WORKSPACE:-$HOME/lobster-workspace}"
MESSAGES="${LOBSTER_MESSAGES:-$HOME/messages}"

for dir in \
    "$HOME/lobster" \
    "$WORKSPACE" \
    "$WORKSPACE/projects" \
    "$MESSAGES/inbox" \
    "$MESSAGES/outbox" \
    "$MESSAGES/processed" \
    "$MESSAGES/processing" \
    "$MESSAGES/failed" \
    "$MESSAGES/audio" \
    "$MESSAGES/task-outputs"; do
    if [ -d "$dir" ]; then
        ok "Directory exists: $dir"
    else
        fail "Directory missing: $dir"
    fi
done

# --- Config ---
echo ""
echo -e "${BOLD}Configuration${NC}"

CONFIG_DIR="${LOBSTER_CONFIG_DIR:-$HOME/lobster-config}"
if [ -d "$CONFIG_DIR" ]; then
    ok "Config dir exists: $CONFIG_DIR"
else
    fail "Config dir missing: $CONFIG_DIR"
fi

if [ -f "$CONFIG_DIR/global.env" ]; then
    ok "global.env exists"
else
    fail "global.env missing"
fi

# --- Claude Code discovery symlinks ---
echo ""
echo -e "${BOLD}Claude Code discovery${NC}"

if [ -f "$WORKSPACE/CLAUDE.md" ] || [ -L "$WORKSPACE/CLAUDE.md" ]; then
    ok "CLAUDE.md discoverable from workspace"
else
    warn "CLAUDE.md not found at $WORKSPACE/CLAUDE.md (may affect Claude Code startup)"
fi

if [ -d "$WORKSPACE/.claude/agents" ] || [ -L "$WORKSPACE/.claude/agents" ]; then
    ok ".claude/agents discoverable from workspace"
else
    warn ".claude/agents not found at $WORKSPACE/.claude/agents"
fi

# --- Whisper (optional but expected) ---
echo ""
echo -e "${BOLD}Voice transcription${NC}"

WHISPER_BIN="$WORKSPACE/whisper.cpp/build/bin/whisper-cli"
if [ -x "$WHISPER_BIN" ]; then
    ok "whisper-cli binary exists"
else
    warn "whisper-cli not found at $WHISPER_BIN (voice transcription won't work)"
fi

WHISPER_MODEL="$WORKSPACE/whisper.cpp/models/ggml-small.bin"
if [ -f "$WHISPER_MODEL" ]; then
    ok "whisper small model downloaded"
else
    warn "whisper small model missing at $WHISPER_MODEL"
fi

# --- Systemd service files ---
echo ""
echo -e "${BOLD}Systemd service files${NC}"

for svc in lobster-router lobster-claude; do
    if [ -f "/etc/systemd/system/${svc}.service" ]; then
        ok "Service file installed: ${svc}.service"
    else
        fail "Service file missing: /etc/systemd/system/${svc}.service"
    fi
done

# --- MCP registration ---
echo ""
echo -e "${BOLD}MCP registration${NC}"

CLAUDE_CONFIG="$HOME/.claude/claude_desktop_config.json"
if [ -f "$CLAUDE_CONFIG" ] && python3 -c "import json; d=json.load(open('$CLAUDE_CONFIG')); assert 'lobster-inbox' in d.get('mcpServers', {})" 2>/dev/null; then
    ok "lobster-inbox MCP server registered in claude_desktop_config.json"
elif claude mcp list 2>/dev/null | grep -q "lobster-inbox"; then
    ok "lobster-inbox MCP server registered (via claude mcp list)"
else
    warn "lobster-inbox MCP registration not confirmed (may need auth)"
fi

# --- Summary ---
echo ""
echo -e "${BOLD}=== Summary ===${NC}"
echo -e "  ${GREEN}Passed${NC}: $PASS"
if [ "$WARN" -gt 0 ]; then
    echo -e "  ${YELLOW}Warnings${NC}: $WARN"
fi
if [ "$FAIL" -gt 0 ]; then
    echo -e "  ${RED}Failed${NC}: $FAIL"
    echo ""
    echo "Some checks failed. Review the output above."
    exit 1
else
    echo ""
    echo -e "${GREEN}All required checks passed.${NC}"
    if [ "$WARN" -gt 0 ]; then
        echo "(Some optional components had warnings — see above)"
    fi
    exit 0
fi
