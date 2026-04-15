#!/usr/bin/env bash
# Lobster WhatsApp Connector — Setup Script
#
# Installs the Baileys-based WhatsApp bridge as a user-mode systemd service.
# Run this once per Lobster user. No root required for the service itself.
#
# Usage:
#   bash connectors/whatsapp/setup.sh
#
# After running, start the bridge and scan the QR code:
#   systemctl --user start lobster-whatsapp-bridge
#   journalctl --user -u lobster-whatsapp-bridge -f
#
# Scan the QR code with WhatsApp: Settings > Linked Devices > Link a Device
# Then add WHATSAPP_LOBSTER_JID=<jid> to ~/.config/lobster/whatsapp.env

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BRIDGE_DIR="${LOBSTER_PROJECTS:-${HOME}/lobster-workspace/projects}/lobster/connectors/whatsapp"
SERVICE_SRC="${SCRIPT_DIR}/lobster-whatsapp-bridge.service"
SERVICE_DEST="${HOME}/.config/systemd/user/lobster-whatsapp-bridge.service"
CONFIG_DIR="${HOME}/.config/lobster"
CONFIG_EXAMPLE="${SCRIPT_DIR}/config.example.env"
CONFIG_DEST="${CONFIG_DIR}/whatsapp.env"

echo "=== Lobster WhatsApp Connector Setup ==="
echo ""

# 1. Create required directories
echo "[1/5] Creating required directories..."
mkdir -p "${HOME}/messages/wa-events"
mkdir -p "${HOME}/messages/wa-commands"
mkdir -p "${HOME}/lobster-workspace/logs"
mkdir -p "${CONFIG_DIR}"
echo "      Done."

# 2. Create user config file if it doesn't exist
echo "[2/5] Creating user config file..."
if [[ -f "${CONFIG_DEST}" ]]; then
    echo "      ${CONFIG_DEST} already exists — skipping (edit manually if needed)"
else
    cp "${CONFIG_EXAMPLE}" "${CONFIG_DEST}"
    echo "      Created ${CONFIG_DEST}"
    echo "      Edit this file to set WHATSAPP_LOBSTER_JID after your first QR scan."
fi

# 3. Install npm dependencies in the bridge directory
echo "[3/5] Installing npm dependencies in ${BRIDGE_DIR}..."
if [[ ! -d "${BRIDGE_DIR}" ]]; then
    echo "      ERROR: Bridge directory not found: ${BRIDGE_DIR}"
    echo "      Make sure the lobster repo is cloned to ~/lobster-workspace/projects/lobster"
    exit 1
fi
cd "${BRIDGE_DIR}"
npm install --omit=dev
echo "      Done."

# 4. Install systemd user service
echo "[4/5] Installing systemd user service..."
mkdir -p "${HOME}/.config/systemd/user"
cp "${SERVICE_SRC}" "${SERVICE_DEST}"
systemctl --user daemon-reload
systemctl --user enable lobster-whatsapp-bridge
echo "      Service installed and enabled."

# 5. Also install the Python adapter service if present
ADAPTER_SRC="${SCRIPT_DIR}/../../services/lobster-whatsapp-adapter.service"
ADAPTER_DEST="${HOME}/.config/systemd/user/lobster-whatsapp-adapter.service"
if [[ -f "${ADAPTER_SRC}" ]]; then
    echo "[5/5] Installing Python adapter service..."
    cp "${ADAPTER_SRC}" "${ADAPTER_DEST}"
    # Patch hardcoded paths if needed (adapter service uses /home/admin paths)
    sed -i "s|/home/admin|${HOME}|g" "${ADAPTER_DEST}"
    systemctl --user daemon-reload
    systemctl --user enable lobster-whatsapp-adapter
    echo "      Adapter service installed and enabled."
else
    echo "[5/5] Skipping adapter service (file not found at ${ADAPTER_SRC})"
fi

echo ""
echo "=== Setup complete ==="
echo ""
echo "Next steps:"
echo ""
echo "  1. Start the bridge and view the QR code:"
echo "       systemctl --user start lobster-whatsapp-bridge"
echo "       journalctl --user -u lobster-whatsapp-bridge -f"
echo ""
echo "  2. Scan the QR code with WhatsApp:"
echo "       Settings > Linked Devices > Link a Device"
echo ""
echo "  3. The bridge will print your JID, e.g.:"
echo "       [READY] Detected Lobster JID: 15551234567@c.us"
echo ""
echo "  4. Add that JID to ${CONFIG_DEST}:"
echo "       WHATSAPP_LOBSTER_JID=15551234567@c.us"
echo ""
echo "  5. Restart the bridge to pick up the JID:"
echo "       systemctl --user restart lobster-whatsapp-bridge"
echo ""
echo "  6. Start the Python adapter:"
echo "       systemctl --user start lobster-whatsapp-adapter"
echo ""
echo "  7. Send yourself a WhatsApp DM and verify it appears in check_inbox()"
