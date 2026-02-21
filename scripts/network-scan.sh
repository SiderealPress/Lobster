#!/bin/bash
# network-scan.sh — Local network IoT discovery & analysis
# Run on your local machine with: bash network-scan.sh [output_dir]
#
# Prerequisites:
#   sudo apt install nmap arp-scan net-tools
#   (Claude Code should be available in PATH)
#
# Usage:
#   bash network-scan.sh ~/Desktop/network-scan-results
#   bash network-scan.sh  # defaults to ./network-scan-results

set -euo pipefail

OUTPUT_DIR="${1:-./network-scan-results}"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
SCAN_DIR="$OUTPUT_DIR/$TIMESTAMP"
mkdir -p "$SCAN_DIR"

echo "========================================="
echo "  Network Scanner for Lobster"
echo "  $(date)"
echo "========================================="
echo ""
echo "Output directory: $SCAN_DIR"
echo ""

# --- Step 1: Detect local network ---
echo "[1/5] Detecting local network..."
GATEWAY=$(ip route | grep default | awk '{print $3}' | head -1)
INTERFACE=$(ip route | grep default | awk '{print $5}' | head -1)
LOCAL_IP=$(ip -4 addr show "$INTERFACE" | grep -oP '(?<=inet\s)\d+(\.\d+){3}')
SUBNET=$(ip -4 addr show "$INTERFACE" | grep -oP '(?<=inet\s)\d+(\.\d+){3}/\d+')

echo "  Interface: $INTERFACE"
echo "  Local IP:  $LOCAL_IP"
echo "  Gateway:   $GATEWAY"
echo "  Subnet:    $SUBNET"
echo ""

cat > "$SCAN_DIR/network-info.json" << EOF
{
  "scan_timestamp": "$(date -Iseconds)",
  "interface": "$INTERFACE",
  "local_ip": "$LOCAL_IP",
  "gateway": "$GATEWAY",
  "subnet": "$SUBNET"
}
EOF

# --- Step 2: ARP scan (fast layer-2 discovery) ---
echo "[2/5] Running ARP scan (fast host discovery)..."
if command -v arp-scan &> /dev/null; then
    sudo arp-scan --localnet --interface="$INTERFACE" 2>/dev/null | \
        grep -E '^[0-9]+\.' > "$SCAN_DIR/arp-scan-raw.txt" || true
    echo "  Found $(wc -l < "$SCAN_DIR/arp-scan-raw.txt") hosts via ARP"
else
    echo "  arp-scan not found, skipping (install with: sudo apt install arp-scan)"
    touch "$SCAN_DIR/arp-scan-raw.txt"
fi
echo ""

# --- Step 3: Nmap ping sweep ---
echo "[3/5] Running nmap ping sweep..."
sudo nmap -sn "$SUBNET" -oX "$SCAN_DIR/ping-sweep.xml" -oN "$SCAN_DIR/ping-sweep.txt" 2>/dev/null
HOSTS_UP=$(grep -c "Host is up" "$SCAN_DIR/ping-sweep.txt" || echo "0")
echo "  Found $HOSTS_UP hosts up"
echo ""

# --- Step 4: Nmap service + OS detection on discovered hosts ---
echo "[4/5] Running nmap service & OS detection (this takes 1-3 minutes)..."
sudo nmap -sV -O --osscan-guess -T4 "$SUBNET" \
    -oX "$SCAN_DIR/service-scan.xml" \
    -oN "$SCAN_DIR/service-scan.txt" 2>/dev/null
echo "  Service scan complete"
echo ""

# --- Step 5: Generate analysis prompt ---
echo "[5/5] Generating analysis prompt..."

cat > "$SCAN_DIR/ANALYZE.md" << 'PROMPT_EOF'
# Network Scan Analysis Prompt

Run this with Claude Code from the scan results directory:

```
cd <this-directory>
claude --print "$(cat ANALYZE.md)"
```

Or paste the contents of `service-scan.txt` and this prompt into Claude.

---

## Instructions for Claude

You are analyzing a home/office network scan. Read the following files in this directory:
- `network-info.json` — basic network info
- `arp-scan-raw.txt` — ARP discovery results (IP, MAC, vendor)
- `service-scan.txt` — nmap service and OS detection results

Produce a JSON report saved as `analysis.json` with this exact structure:

```json
{
  "scan_summary": {
    "timestamp": "ISO-8601",
    "subnet": "x.x.x.x/xx",
    "total_hosts": 0,
    "iot_devices": 0,
    "computers": 0,
    "network_equipment": 0,
    "unknown": 0
  },
  "devices": [
    {
      "ip": "x.x.x.x",
      "mac": "AA:BB:CC:DD:EE:FF",
      "vendor": "manufacturer name",
      "hostname": "if known",
      "os_guess": "best OS guess",
      "category": "iot|computer|phone|network|printer|media|camera|unknown",
      "device_type": "smart speaker|thermostat|router|laptop|etc",
      "open_ports": [
        {"port": 80, "service": "http", "product": "nginx", "version": "1.x"}
      ],
      "security_notes": ["any concerns — default creds, unencrypted services, etc"]
    }
  ],
  "security_assessment": {
    "risk_level": "low|medium|high",
    "findings": [
      "List of security observations"
    ],
    "recommendations": [
      "List of recommended actions"
    ]
  },
  "network_topology": {
    "gateway": "x.x.x.x",
    "segments": ["description of any apparent network segmentation"],
    "notes": "any topology observations"
  }
}
```

Also produce a human-readable summary saved as `summary.md` with:
- Device inventory table (IP | Type | Name | OS | Notable Ports)
- IoT devices highlighted
- Security findings
- Recommendations

Categorize devices using MAC vendor prefixes, open ports, and OS detection:
- Smart home: Philips Hue, Ring, Nest, Sonos, Echo, etc.
- Media: Roku, Apple TV, Chromecast, smart TVs
- Network: routers, switches, access points
- Cameras: IP cameras, doorbells
- Computers: Windows, macOS, Linux workstations/servers
- Phones: iOS, Android
- Printers: network printers, scanners
- Unknown: anything unidentifiable

PROMPT_EOF

echo ""
echo "========================================="
echo "  Scan Complete!"
echo "========================================="
echo ""
echo "Results saved to: $SCAN_DIR"
echo ""
echo "Files:"
ls -la "$SCAN_DIR"
echo ""
echo "Next steps:"
echo "  1. Review results: cat $SCAN_DIR/service-scan.txt"
echo "  2. Run Claude analysis:"
echo "     cd $SCAN_DIR && claude --print \"\$(cat ANALYZE.md)\" > analysis-output.txt"
echo "  3. Send results to Lobster (copy analysis.json to your Telegram chat)"
echo ""
