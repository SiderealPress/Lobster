#!/bin/bash
set -e
BRIDGE_DIR="/home/admin/lobster-workspace/projects/whatsapp-bridge"
SERVICE_FILE="$(dirname "$0")/lobster-whatsapp-bridge.service"

echo "=== Installing Lobster WhatsApp Bridge ==="

# Create required directories
mkdir -p ~/lobster-workspace/logs
mkdir -p ~/messages/wa-commands

# Install Node dependencies
cd "$BRIDGE_DIR"
npm install

# Install systemd service
sudo cp "$SERVICE_FILE" /etc/systemd/system/lobster-whatsapp-bridge.service
sudo systemctl daemon-reload
sudo systemctl enable lobster-whatsapp-bridge

echo "=== Installation complete ==="
echo "Run: sudo systemctl start lobster-whatsapp-bridge"
echo "Then check QR: journalctl -u lobster-whatsapp-bridge -f"
