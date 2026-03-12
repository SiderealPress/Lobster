#!/usr/bin/env bash
# install.sh — Multiplayer Telegram Bot skill installer
set -euo pipefail

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MESSAGES_DIR="${LOBSTER_MESSAGES:-$HOME/messages}"
CONFIG_DIR="$MESSAGES_DIR/config"

echo "Installing multiplayer-telegram-bot skill..."

# Create config directory if needed
mkdir -p "$CONFIG_DIR"

# Initialize group whitelist if it doesn't exist
WHITELIST="$CONFIG_DIR/group-whitelist.json"
if [ ! -f "$WHITELIST" ]; then
    cat > "$WHITELIST" <<'EOF'
{
  "groups": {}
}
EOF
    echo "Created $WHITELIST"
else
    echo "Whitelist already exists at $WHITELIST"
fi

echo ""
echo "Installation complete!"
echo ""
echo "Next steps:"
echo "  1. Add your Lobster bot to a Telegram group"
echo "  2. Get the group chat ID (forward a message to @userinfobot)"
echo "  3. Tell Lobster: /enable-group-bot <chat_id>"
echo "  4. Whitelist users: /whitelist <user_id> <chat_id>"
echo ""
echo "Group messages will appear in your inbox with source=lobster-group"
