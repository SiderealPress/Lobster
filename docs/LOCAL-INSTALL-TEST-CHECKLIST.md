# Local Installation Test Checklist

Manual verification checklist for testing `docs/LOCAL-INSTALL.md` and `scripts/local-setup-helper.sh`.

## Prerequisites

- [ ] Fresh Debian 12 VM (or Ubuntu 22.04+)
- [ ] VM has internet access
- [ ] Tailscale account ready
- [ ] Telegram bot token and user ID available
- [ ] Claude Max subscription authenticated (or ready to auth)

---

## Test A: Manual Installation (Following Documentation)

### A1: VM Creation
- [ ] VM created with recommended specs (4GB RAM, 2 CPU, 20GB disk)
- [ ] Debian 12 installed successfully
- [ ] Can log in to VM

### A2: Initial Setup (Step 2 in docs)
```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y curl git sudo
sudo usermod -aG sudo $USER
```
- [ ] Commands complete without error
- [ ] User has sudo access

### A3: Tailscale Installation (Step 3 in docs)
```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```
- [ ] Tailscale installs successfully
- [ ] Authentication URL appears
- [ ] Can authenticate via browser
- [ ] `tailscale status` shows connected

### A4: Lobster Installation (Step 4 in docs)
```bash
bash <(curl -fsSL https://raw.githubusercontent.com/SiderealPress/lobster/main/install.sh)
```
- [ ] Installer downloads and starts
- [ ] Prompts for Telegram bot token
- [ ] Prompts for Telegram user ID
- [ ] Python venv created successfully
- [ ] MCP servers registered
- [ ] systemd services installed
- [ ] Claude Code authentication works

### A5: Tailscale Funnel (Step 5 in docs)
```bash
sudo tailscale funnel 443 on
```
- [ ] Command succeeds OR shows admin console message
- [ ] If failed, enabling in admin console works
- [ ] `tailscale funnel status` shows funnel active

### A6: Verification (Step 6 in docs)
```bash
lobster status
```
- [ ] lobster-router service is running
- [ ] lobster-claude service is running
- [ ] Send test Telegram message to bot
- [ ] Bot responds correctly

### A7: Documentation Accuracy
- [ ] All commands in docs worked as written
- [ ] No missing steps discovered
- [ ] No confusing or unclear instructions

---

## Test B: Helper Script Installation

### B1: Fresh VM
- [ ] Start with fresh Debian 12 VM (or reset previous)
- [ ] Only base system installed

### B2: Run Helper Script
```bash
curl -fsSL https://raw.githubusercontent.com/SiderealPress/lobster/main/scripts/local-setup-helper.sh | bash
```

- [ ] Script detects Debian/Ubuntu correctly
- [ ] No root warning (running as regular user)
- [ ] Virtualization detection runs (informational)

### B3: Step 1/4 - Dependencies
- [ ] `apt update` runs
- [ ] `curl git` installed
- [ ] Shows success message

### B4: Step 2/4 - Tailscale
- [ ] Tailscale installs (or detects existing)
- [ ] `tailscale up` runs
- [ ] Auth URL displayed
- [ ] Authentication completes
- [ ] Status displayed

### B5: Step 3/4 - Lobster
- [ ] Lobster installer runs
- [ ] All prompts work correctly
- [ ] Installation completes

### B6: Step 4/4 - Funnel
- [ ] Funnel enable attempted
- [ ] Success or helpful error message shown

### B7: Completion
- [ ] "Setup Complete" banner displayed
- [ ] Tailscale hostname URL displayed
- [ ] Useful commands listed
- [ ] Platform-specific tips shown (based on hypervisor)

### B8: Functional Test
- [ ] `lobster status` shows services running
- [ ] Telegram bot responds to messages

---

## Test C: Edge Cases

### C1: Non-Debian System
```bash
# Run on Fedora, Arch, etc.
curl -fsSL https://raw.githubusercontent.com/SiderealPress/lobster/main/scripts/local-setup-helper.sh | bash
```
- [ ] Warning displayed about unsupported distro
- [ ] Prompt to continue anyway works
- [ ] Can abort with 'n'

### C2: Running as Root
```bash
sudo su -
curl -fsSL https://raw.githubusercontent.com/SiderealPress/lobster/main/scripts/local-setup-helper.sh | bash
```
- [ ] Warning displayed about running as root
- [ ] Prompt to continue works
- [ ] Can abort with 'n'

### C3: Tailscale Already Installed
- [ ] Script detects existing Tailscale
- [ ] Skips installation, proceeds to auth

### C4: Re-running Script
- [ ] Script handles already-configured system gracefully
- [ ] No destructive overwrites

---

## Test D: Platform-Specific (Host Machine)

### D1: Linux Host with KVM
- [ ] KVM/virt-manager instructions work
- [ ] VM creation steps accurate
- [ ] `virsh autostart` tip displayed at end

### D2: Linux Host with VirtualBox
- [ ] VirtualBox instructions work
- [ ] VBoxManage tips displayed at end

### D3: macOS Host with UTM
- [ ] UTM instructions work
- [ ] VM creation steps accurate

### D4: Windows Host with VirtualBox
- [ ] VirtualBox instructions work
- [ ] VM creation steps accurate

---

## Test Results Summary

| Test | Pass | Fail | Notes |
|------|------|------|-------|
| A: Manual Install | | | |
| B: Helper Script | | | |
| C: Edge Cases | | | |
| D: Platform-Specific | | | |

### Issues Found

1.
2.
3.

### Suggested Improvements

1.
2.
3.

---

## Tester Info

- **Date**:
- **VM Software**:
- **Host OS**:
- **Debian Version**:
- **Tester**:
