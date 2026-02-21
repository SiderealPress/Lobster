#!/bin/bash
# Daily check for Lobster updates - inject message if updates available
set -euo pipefail

LOBSTER_DIR="${LOBSTER_INSTALL_DIR:-$HOME/lobster}"
INBOX="${LOBSTER_MESSAGES:-$HOME/messages}/inbox"

cd "$LOBSTER_DIR"

# Support both git and tarball installs
if [ -d "$LOBSTER_DIR/.git" ]; then
    git fetch origin main --quiet

    LOCAL=$(git rev-parse HEAD)
    REMOTE=$(git rev-parse origin/main)

    if [ "$LOCAL" != "$REMOTE" ]; then
        BEHIND=$(git rev-list --count "$LOCAL..$REMOTE")
        TIMESTAMP=$(date +%s%3N)
        cat > "$INBOX/${TIMESTAMP}_update_available.json" << EOF
{
  "id": "${TIMESTAMP}_update_available",
  "source": "internal",
  "chat_id": 0,
  "type": "update_notification",
  "text": "UPDATE AVAILABLE: Lobster is ${BEHIND} commits behind origin/main. Use check_updates for details.",
  "timestamp": "$(date -Iseconds)"
}
EOF
    fi
else
    # Tarball mode: check GitHub Releases API
    CURRENT_VERSION=$(cat "$LOBSTER_DIR/VERSION" 2>/dev/null || echo "0.0.0")
    LATEST_TAG=$(curl -fsSL "https://api.github.com/repos/SiderealPress/lobster/releases/latest" 2>/dev/null | jq -r '.tag_name // empty')
    LATEST_VERSION="${LATEST_TAG#v}"

    if [ -n "$LATEST_VERSION" ] && [ "$LATEST_VERSION" != "$CURRENT_VERSION" ]; then
        TIMESTAMP=$(date +%s%3N)
        cat > "$INBOX/${TIMESTAMP}_update_available.json" << EOF
{
  "id": "${TIMESTAMP}_update_available",
  "source": "internal",
  "chat_id": 0,
  "type": "update_notification",
  "text": "UPDATE AVAILABLE: Lobster v${CURRENT_VERSION} -> v${LATEST_VERSION}. Use check_updates for details.",
  "timestamp": "$(date -Iseconds)"
}
EOF
    fi
fi
