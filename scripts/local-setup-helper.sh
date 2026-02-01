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
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

info() { echo -e "${BLUE}[INFO]${NC} $1"; }
success() { echo -e "${GREEN}[OK]${NC} $1"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; }
step() { echo -e "\n${CYAN}${BOLD}▶ $1${NC}"; }

echo -e "${BOLD}"
echo "╔═══════════════════════════════════════════════════════════╗"
echo "║         Hyperion Local Setup Helper                       ║"
echo "║         Sets up Hyperion + Tailscale Funnel in a VM       ║"
echo "╚═══════════════════════════════════════════════════════════╝"
echo -e "${NC}"

#-------------------------------------------------------------------------------
# Pre-flight checks
#-------------------------------------------------------------------------------

# Detect if running on a supported system
detect_system() {
    if [ -f /etc/os-release ]; then
        . /etc/os-release
        echo "$ID"
    else
        echo "unknown"
    fi
}

DISTRO=$(detect_system)

case "$DISTRO" in
    debian|ubuntu)
        info "Detected: $DISTRO (supported)"
        ;;
    *)
        warn "This script is designed for Debian/Ubuntu systems."
        warn "Detected: $DISTRO"
        echo ""
        read -p "Continue anyway? (y/N) " -n 1 -r
        echo
        if [[ ! $REPLY =~ ^[Yy]$ ]]; then
            echo "Exiting."
            exit 1
        fi
        ;;
esac

# Check if running as root (not recommended)
if [ "$EUID" -eq 0 ]; then
    warn "Running as root is not recommended."
    warn "This script uses sudo where needed."
    echo ""
    read -p "Continue as root? (y/N) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo "Please run as a regular user with sudo access."
        exit 1
    fi
fi

# Detect if likely running in a VM (informational only)
detect_virtualization() {
    if command -v systemd-detect-virt &> /dev/null; then
        systemd-detect-virt 2>/dev/null || echo "none"
    elif [ -f /sys/class/dmi/id/product_name ]; then
        cat /sys/class/dmi/id/product_name 2>/dev/null || echo "unknown"
    else
        echo "unknown"
    fi
}

VIRT=$(detect_virtualization)
if [ "$VIRT" != "none" ] && [ "$VIRT" != "unknown" ]; then
    info "Virtualization detected: $VIRT"
else
    info "Running on: ${VIRT:-physical/unknown}"
    info "This script works on both VMs and physical machines."
fi

echo ""

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
    warn "Funnel may require enabling in Tailscale admin console"
    info "Visit: https://login.tailscale.com/admin/machines"
    info "Click on this machine and enable Funnel"
}

#-------------------------------------------------------------------------------
echo ""
echo -e "${GREEN}${BOLD}╔═══════════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}${BOLD}║                    Setup Complete!                        ║${NC}"
echo -e "${GREEN}${BOLD}╚═══════════════════════════════════════════════════════════╝${NC}"
echo ""

HOSTNAME=$(tailscale status --json 2>/dev/null | grep -o '"DNSName":"[^"]*"' | head -1 | cut -d'"' -f4 | sed 's/\.$//')
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

# Platform-specific tips
echo -e "${BOLD}Tips for keeping your VM running:${NC}"
case "$VIRT" in
    kvm|qemu)
        echo "  • KVM/libvirt: virsh autostart $(hostname)"
        echo "  • Close virt-manager window - VM keeps running"
        ;;
    oracle|virtualbox)
        echo "  • VirtualBox: VBoxManage modifyvm $(hostname) --autostart-enabled on"
        echo "  • Run headless: VBoxManage startvm $(hostname) --type headless"
        ;;
    vmware)
        echo "  • VMware: Enable auto-start in VM settings"
        ;;
    microsoft|hyperv)
        echo "  • Hyper-V: Set-VM -Name $(hostname) -AutomaticStartAction Start"
        ;;
    *)
        echo "  • Check your VM software docs for auto-start options"
        ;;
esac
echo ""
