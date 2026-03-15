#!/bin/bash
# Install the Lobster WhatsApp connector (bridge + adapter services + logrotate).
# Run as a user with sudo access.
#
# Usage: bash scripts/install-whatsapp-connector.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
BRIDGE_DIR="$REPO_DIR/connectors/whatsapp"
CONFIG_DIR="$HOME/lobster-workspace/config"
LOG_DIR="$HOME/lobster-workspace/logs"
MESSAGES_DIR="${LOBSTER_MESSAGES:-$HOME/messages}"

echo "=== Lobster WhatsApp Connector Install ==="
echo "Repo:    $REPO_DIR"
echo "Bridge:  $BRIDGE_DIR"
echo ""

# 1. Install Node.js dependencies for the bridge
echo "[1/5] Installing bridge npm dependencies..."
if ! command -v node &>/dev/null; then
    echo "ERROR: Node.js is not installed. Install Node 18+ first:"
    echo "  curl -fsSL https://deb.nodesource.com/setup_18.x | sudo -E bash -"
    echo "  sudo apt-get install -y nodejs"
    exit 1
fi

NODE_VERSION=$(node --version | sed 's/v//')
NODE_MAJOR=$(echo "$NODE_VERSION" | cut -d. -f1)
if [ "$NODE_MAJOR" -lt 18 ]; then
    echo "ERROR: Node.js 18+ required (found $NODE_VERSION)"
    exit 1
fi

(cd "$BRIDGE_DIR" && npm install)
echo "  Bridge dependencies installed"

# 2. Ensure required directories exist
echo "[2/5] Creating required directories..."
mkdir -p "$LOG_DIR" "$CONFIG_DIR"
mkdir -p "$MESSAGES_DIR/wa-events" "$MESSAGES_DIR/wa-commands"
echo "  Directories created"

# 3. Create config file from example if it doesn't exist
echo "[3/5] Setting up configuration..."
if [ ! -f "$CONFIG_DIR/whatsapp.env" ]; then
    cp "$REPO_DIR/config/whatsapp.env.example" "$CONFIG_DIR/whatsapp.env"
    echo "  Created $CONFIG_DIR/whatsapp.env — edit to add your WHATSAPP_LOBSTER_JID after first QR scan"
else
    echo "  Config already exists at $CONFIG_DIR/whatsapp.env"
fi

# 4. Install systemd services
echo "[4/5] Installing systemd services..."
sudo cp "$REPO_DIR/services/lobster-whatsapp-bridge.service" /etc/systemd/system/
sudo cp "$REPO_DIR/services/lobster-whatsapp-adapter.service" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable lobster-whatsapp-bridge
sudo systemctl enable lobster-whatsapp-adapter
echo "  Services installed and enabled"

# 5. Install logrotate config
echo "[5/5] Installing logrotate config..."
if [ -f "$REPO_DIR/logrotate/lobster-whatsapp" ]; then
    sudo cp "$REPO_DIR/logrotate/lobster-whatsapp" /etc/logrotate.d/lobster-whatsapp
    echo "  Logrotate config installed"
else
    echo "  Logrotate config not found — skipping"
fi

echo ""
echo "=== Installation complete ==="
echo ""
echo "NEXT STEPS:"
echo "  1. Start the bridge for QR authentication:"
echo "     sudo systemctl start lobster-whatsapp-bridge"
echo "     journalctl -u lobster-whatsapp-bridge -f"
echo ""
echo "  2. Scan the QR code in WhatsApp:"
echo "     Settings > Linked Devices > Link a Device"
echo ""
echo "  3. Copy the JID from the log (e.g. 15551234567@c.us) and set it:"
echo "     echo 'WHATSAPP_LOBSTER_JID=<your-jid>' >> $CONFIG_DIR/whatsapp.env"
echo ""
echo "  4. Restart services:"
echo "     sudo systemctl restart lobster-whatsapp-bridge"
echo "     sudo systemctl start lobster-whatsapp-adapter"
echo ""
echo "  5. Test by sending a direct message to your WhatsApp number."
