#!/bin/bash
# Obsidian KM Skill Installer
# Installs dependencies and configures the MCP server

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "Installing obsidian-km skill dependencies..."

# Install Python dependencies
uv pip install mcp pyyaml --quiet

echo "obsidian-km skill installed successfully."
echo ""
echo "To configure, set your vault path:"
echo "  /skill set obsidian-km vault_path /path/to/your/vault"
