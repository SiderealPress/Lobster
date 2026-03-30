#!/usr/bin/env bash
# Install the Obsidian KM skill for Lobster
set -euo pipefail

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_NAME="obsidian-km"

echo "Installing $SKILL_NAME..."

# Check for required environment variable
if [[ -z "${OBSIDIAN_VAULT_PATH:-}" ]]; then
    echo ""
    echo "WARNING: OBSIDIAN_VAULT_PATH is not set."
    echo "Add this to your shell profile (~/.bashrc or ~/.zshrc):"
    echo ""
    echo "  export OBSIDIAN_VAULT_PATH=\"\$HOME/path/to/your/vault\""
    echo ""
fi

# Verify Python MCP package is available
if ! python3 -c "import mcp" 2>/dev/null; then
    echo "Installing mcp package..."
    pip install --quiet mcp
fi

# Register MCP server with Claude Code (if claude_config available)
CLAUDE_CONFIG="$HOME/.claude.json"
if [[ -f "$CLAUDE_CONFIG" ]]; then
    echo "Registering MCP server with Claude Code..."

    # Use jq to add the server if jq is available
    if command -v jq &>/dev/null; then
        # Create backup
        cp "$CLAUDE_CONFIG" "${CLAUDE_CONFIG}.bak"

        # Add server entry
        jq --arg name "$SKILL_NAME" \
           --arg cmd "python3" \
           --arg script "$SKILL_DIR/src/obsidian_km_server.py" \
           '.mcpServers[$name] = {"command": $cmd, "args": [$script]}' \
           "$CLAUDE_CONFIG" > "${CLAUDE_CONFIG}.tmp" && \
        mv "${CLAUDE_CONFIG}.tmp" "$CLAUDE_CONFIG"

        echo "MCP server registered: $SKILL_NAME"
    else
        echo "jq not found — manual registration required."
        echo "Add to $CLAUDE_CONFIG mcpServers:"
        echo "  \"$SKILL_NAME\": {\"command\": \"python3\", \"args\": [\"$SKILL_DIR/src/obsidian_km_server.py\"]}"
    fi
else
    echo "Claude config not found at $CLAUDE_CONFIG"
    echo "Manual MCP server registration may be required."
fi

echo ""
echo "$SKILL_NAME installed successfully!"
echo ""
echo "Next steps:"
echo "  1. Set OBSIDIAN_VAULT_PATH if not already set"
echo "  2. Restart Claude Code to load the MCP server"
echo "  3. Activate the skill: activate_skill(\"$SKILL_NAME\")"
