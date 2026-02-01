#!/bin/bash
#===============================================================================
# Hyperion Local Setup Helper
#
# Convenience script for setting up Hyperion in a local VM with Tailscale Funnel.
# Run this inside a fresh Debian 12 VM.
#
# Usage: curl -fsSL https://raw.githubusercontent.com/SiderealPress/hyperion/main/scripts/local-setup-helper.sh | bash
#===============================================================================

set -e

# Colors
GREEN='\033[0;32m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

info() { echo -e "${BLUE}[INFO]${NC} $1"; }
success() { echo -e "${GREEN}[OK]${NC} $1"; }
step() { echo -e "\n${CYAN}${BOLD}▶ $1${NC}"; }

echo -e "${BOLD}"
echo "╔═══════════════════════════════════════════════════════════╗"
echo "║         Hyperion Local Setup Helper                       ║"
echo "║         Sets up Hyperion + Tailscale Funnel in a VM       ║"
echo "╚═══════════════════════════════════════════════════════════╝"
echo -e "${NC}"

#-------------------------------------------------------------------------------
step "Step 1/4: Installing system dependencies"
#-------------------------------------------------------------------------------
sudo apt update
sudo apt install -y curl git
success "Dependencies installed"

#-------------------------------------------------------------------------------
step "Step 2/4: Installing Tailscale"
#-------------------------------------------------------------------------------
if command -v tailscale &> /dev/null; then
    info "Tailscale already installed"
else
    curl -fsSL https://tailscale.com/install.sh | sh
    success "Tailscale installed"
fi

info "Starting Tailscale authentication..."
info "Follow the URL to authenticate in your browser"
sudo tailscale up

success "Tailscale connected"
tailscale status

#-------------------------------------------------------------------------------
step "Step 3/4: Installing Hyperion"
#-------------------------------------------------------------------------------
info "Running Hyperion installer..."
bash <(curl -fsSL https://raw.githubusercontent.com/SiderealPress/hyperion/main/install.sh)

success "Hyperion installed"

#-------------------------------------------------------------------------------
step "Step 4/4: Enabling Tailscale Funnel"
#-------------------------------------------------------------------------------
info "Enabling Funnel to expose Hyperion to the internet..."
sudo tailscale funnel 443 on || {
    info "Funnel may require enabling in Tailscale admin console"
    info "Visit: https://login.tailscale.com/admin/machines"
}

#-------------------------------------------------------------------------------
echo ""
echo -e "${GREEN}${BOLD}╔═══════════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}${BOLD}║                    Setup Complete!                        ║${NC}"
echo -e "${GREEN}${BOLD}╚═══════════════════════════════════════════════════════════╝${NC}"
echo ""

HOSTNAME=$(tailscale status --json | grep -o '"DNSName":"[^"]*"' | head -1 | cut -d'"' -f4 | sed 's/\.$//')
if [ -n "$HOSTNAME" ]; then
    echo -e "Your Hyperion instance is accessible at:"
    echo -e "  ${CYAN}${BOLD}https://${HOSTNAME}${NC}"
    echo ""
fi

echo "Useful commands:"
echo "  hyperion status    - Check service status"
echo "  hyperion attach    - Attach to Claude session"
echo "  hyperion logs      - View logs"
echo "  tailscale status   - Check Tailscale connection"
echo ""
