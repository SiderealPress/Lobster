# Running Hyperion Locally

Run Hyperion on your local machine in a virtual machine for testing, development, or personal use.

## Overview

This guide walks you through setting up Hyperion inside a VM on your local computer, using Tailscale Funnel to make it accessible from the internet. This approach:

- Uses the existing `install.sh` unchanged
- Provides full OS isolation (like a cloud server)
- Works on Mac, Windows, and Linux host machines
- Requires no cloud server or hosting costs

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                 Host Machine (Linux/Mac/Windows)             │
├─────────────────────────────────────────────────────────────┤
│  VM Software:                                                │
│    • Linux: KVM/virt-manager, GNOME Boxes, VirtualBox       │
│    • macOS: UTM, VirtualBox, VMware, Parallels              │
│    • Windows: VirtualBox, VMware, Hyper-V                   │
│    │                                                         │
│    └── Debian 12 VM                                         │
│          ├── Tailscale (with Funnel enabled)                │
│          ├── hyperion-router.service (systemd)              │
│          ├── hyperion-claude.service (systemd)              │
│          └── Existing install.sh works unchanged            │
│                                                              │
│          Accessible at: https://your-vm.tailnet-name.ts.net │
└─────────────────────────────────────────────────────────────┘
```

## Prerequisites

| Requirement | Details |
|-------------|---------|
| **VM Software** | See platform-specific options below |
| **Debian 12 ISO** | Download from [debian.org](https://www.debian.org/download) |
| **Tailscale Account** | Free tier works - [tailscale.com](https://tailscale.com) |
| **Claude Max** | Authenticated Claude Code subscription |
| **VM Resources** | 4GB RAM, 2 CPUs, 20GB disk recommended |

### VM Software by Platform

| Platform | Recommended | Alternatives |
|----------|-------------|--------------|
| **Linux** | KVM/virt-manager (best performance) | VirtualBox, GNOME Boxes |
| **macOS** | UTM (free, native) | VirtualBox, VMware Fusion, Parallels |
| **Windows** | VirtualBox (free) | VMware Workstation, Hyper-V |

## Step 1: Create the Virtual Machine

Choose the instructions for your host operating system:

### Linux (KVM/virt-manager - Recommended)

KVM provides near-native performance since it's built into the Linux kernel.

1. Install KVM and virt-manager:
   ```bash
   # Debian/Ubuntu
   sudo apt install -y qemu-kvm libvirt-daemon-system virt-manager

   # Fedora
   sudo dnf install -y @virtualization

   # Arch
   sudo pacman -S qemu-full virt-manager libvirt
   ```

2. Add your user to the libvirt group:
   ```bash
   sudo usermod -aG libvirt $USER
   newgrp libvirt
   ```

3. Start libvirtd:
   ```bash
   sudo systemctl enable --now libvirtd
   ```

4. Open virt-manager and create a new VM:
   - Click **Create a new virtual machine**
   - Select **Local install media (ISO image)**
   - Browse to your Debian 12 ISO
   - Allocate 4096 MB RAM and 2 CPUs
   - Create a 20 GB disk
   - Name it `hyperion`

5. Complete the Debian installation:
   - Choose minimal/standard installation
   - Set a username and password
   - When prompted for software, select **SSH server** and **standard system utilities**

### Linux (GNOME Boxes - Simpler)

GNOME Boxes is simpler but less configurable than virt-manager.

1. Install GNOME Boxes:
   ```bash
   # Debian/Ubuntu
   sudo apt install -y gnome-boxes

   # Fedora
   sudo dnf install -y gnome-boxes
   ```

2. Open Boxes and click **+** → **Create a Virtual Machine**

3. Select the Debian 12 ISO and follow the wizard

4. Adjust resources in VM preferences (4GB RAM recommended)

### Linux/Windows (VirtualBox)

VirtualBox works on any platform but has more overhead than KVM on Linux.

1. Download and install [VirtualBox](https://www.virtualbox.org/)

2. Create a new VM:
   - Click **New**
   - Name: `hyperion`
   - Type: Linux
   - Version: Debian (64-bit)

3. Configure resources:
   - Memory: 4096 MB
   - Create virtual hard disk: 20 GB (VDI, dynamically allocated)

4. Attach the Debian ISO:
   - Select the VM → Settings → Storage
   - Click the empty disk icon → Choose disk file → Select Debian ISO

5. Start the VM and complete Debian installation

### macOS (UTM - Recommended)

UTM is a free, native macOS app built on QEMU.

1. Download and install [UTM](https://mac.getutm.app/) (free)

2. Create a new VM:
   - Click **Create a New Virtual Machine**
   - Select **Virtualize** (faster) or **Emulate** (for Apple Silicon compatibility)
   - Choose **Linux**
   - Select the Debian 12 ISO or download from the gallery

3. Configure resources:
   - Memory: 4096 MB (4 GB)
   - CPU Cores: 2
   - Storage: 20 GB

4. Complete the Debian installation:
   - Choose minimal/standard installation
   - Set a username and password
   - When prompted for software, select **SSH server** and **standard system utilities**

### macOS/Windows (VMware, Parallels)

Follow similar steps - create a Debian 12 VM with 4GB RAM, 2 CPUs, and 20GB disk.

## Step 2: Initial VM Setup

After Debian boots, log in and run:

```bash
# Update system
sudo apt update && sudo apt upgrade -y

# Install essential tools
sudo apt install -y curl git sudo

# Ensure your user has sudo access (if not already)
sudo usermod -aG sudo $USER
```

## Step 3: Install Tailscale

```bash
# Install Tailscale
curl -fsSL https://tailscale.com/install.sh | sh

# Authenticate with Tailscale
sudo tailscale up

# Follow the URL to authenticate in your browser
```

Verify Tailscale is connected:

```bash
tailscale status
```

## Step 4: Install Hyperion

Use the standard one-line installer:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/SiderealPress/hyperion/main/install.sh)
```

The installer will:
- Install all dependencies
- Prompt for your Telegram bot token and user ID
- Set up systemd services
- Configure Claude Code

### Authenticate Claude Code

When prompted, authenticate Claude Code:

```bash
claude auth login
```

Follow the browser-based authentication flow.

## Step 5: Enable Tailscale Funnel

Tailscale Funnel exposes your VM to the internet over HTTPS.

```bash
# Enable Funnel on port 443 (HTTPS)
sudo tailscale funnel 443 on
```

If you need to expose a specific port (e.g., if running a web interface):

```bash
# Expose a specific local port
sudo tailscale funnel --bg 8080
```

### Verify Funnel is Working

Check your Funnel status:

```bash
tailscale funnel status
```

Your VM is now accessible at:
```
https://[vm-hostname].[tailnet-name].ts.net
```

You can find your exact URL with:

```bash
tailscale status | grep -i funnel
# Or check the Tailscale admin console
```

## Step 6: Verify Hyperion

Check that services are running:

```bash
hyperion status
```

Test with a Telegram message to your bot.

## Optional: Using the Helper Script

For convenience, you can use the helper script that automates steps 2-5:

```bash
curl -fsSL https://raw.githubusercontent.com/SiderealPress/hyperion/main/scripts/local-setup-helper.sh | bash
```

This script will:
1. Install Tailscale and prompt for authentication
2. Run the Hyperion installer
3. Enable Tailscale Funnel
4. Display your access URL

## Keeping the VM Running

### Option A: Run in Background (Headless)

Most VM software supports running VMs in the background:

- **KVM/virsh**: `virsh start hyperion` (runs headless by default)
- **virt-manager**: Close the console window; VM keeps running
- **GNOME Boxes**: Minimize the window; VM keeps running
- **VirtualBox**: `VBoxManage startvm hyperion --type headless`
- **UTM**: Right-click VM → Run without window
- **VMware**: Run in background mode

### Option B: Auto-start on Host Boot

Configure your VM to start automatically when your computer boots:

- **KVM/libvirt**: `virsh autostart hyperion`
- **VirtualBox**: `VBoxManage modifyvm hyperion --autostart-enabled on`
- **UTM**: Currently requires third-party tools
- **VMware**: Enable in VM settings
- **systemd (any)**: Create a systemd service to start your VM

## Troubleshooting

### VM Won't Start

- Ensure virtualization is enabled in BIOS/UEFI (VT-x/AMD-V)
- On Linux, verify KVM is available:
  ```bash
  # Check for hardware virtualization support
  grep -E '(vmx|svm)' /proc/cpuinfo

  # Check if KVM module is loaded
  lsmod | grep kvm

  # If using kvm-ok (from cpu-checker package)
  sudo apt install -y cpu-checker && kvm-ok
  ```
- On Mac with Apple Silicon, use UTM's Virtualize mode for best performance

### Tailscale Authentication Issues

```bash
# Re-authenticate
sudo tailscale logout
sudo tailscale up
```

### Funnel Not Accessible

1. Verify Funnel is enabled in your Tailscale admin console:
   - Visit [login.tailscale.com/admin/machines](https://login.tailscale.com/admin/machines)
   - Click on your VM
   - Ensure Funnel is enabled

2. Check firewall settings in the VM:
   ```bash
   sudo ufw status
   # If active, allow Tailscale
   sudo ufw allow in on tailscale0
   ```

### Claude Code Auth in VM

If Claude Code authentication fails:

```bash
# Ensure you have a display or use SSH forwarding
claude auth login

# If no browser available, use device code flow
claude auth login --method device-code
```

### Services Won't Start

```bash
# Check service logs
journalctl -u hyperion-router -n 50
journalctl -u hyperion-claude -n 50

# Restart services
hyperion restart
```

### Performance Issues

If the VM feels slow:

1. Increase RAM allocation (6-8 GB if available)
2. Enable hardware acceleration in VM settings
3. Use SSD storage for the VM disk
4. On Mac with Apple Silicon, ensure you're using ARM64 Debian

## Comparison: Local VM vs Cloud Server

| Aspect | Local VM | Cloud Server |
|--------|----------|--------------|
| **Cost** | Free (your hardware) | ~$5-20/month |
| **Latency** | Depends on your internet | Generally lower |
| **Availability** | Only when host is on | 24/7 |
| **Setup** | More steps | Simpler |
| **Resources** | Shares host resources | Dedicated |

**Best for:**
- **Local VM**: Development, testing, personal use, cost-conscious setups
- **Cloud Server**: Production, 24/7 availability, team access

## Security Considerations

- Tailscale Funnel exposes your VM to the internet - ensure Hyperion's security measures are in place
- The Telegram bot is restricted to allowed user IDs only
- Keep your VM and all software updated
- Consider disabling Funnel when not in use: `sudo tailscale funnel 443 off`

## Next Steps

- Read the main [README](../README.md) for CLI commands and features
- Set up [custom configuration](CUSTOMIZATION.md) if needed
- Configure [scheduled tasks](../README.md#scheduled-jobs) for automation
