#!/usr/bin/env bash
#===============================================================================
# Obsidian KM Skill - Generate LiveSync Setup URI
#
# Generates an obsidian://setuplivesync?settings=<encrypted> URI that can be
# pasted (or scanned as a QR code) in Obsidian to auto-configure the
# Self-hosted LiveSync plugin in one step.
#
# The URI is encrypted with a random passphrase (uri_passphrase). That
# passphrase is printed once — keep it safe. It is separate from the E2EE
# passphrase stored in obsidian.env.
#
# Prerequisites:
#   - ~/lobster-config/obsidian.env  (COUCHDB_USER, COUCHDB_PASSWORD, etc.)
#   - deno   (auto-installed to ~/.deno/bin/deno if missing)
#   - curl   (for fetching external IP)
#   - qrencode  (optional — generates QR code for mobile scanning)
#
# Usage:
#   bash ~/lobster/lobster-shop/obsidian-km/scripts/generate-setup-uri.sh
#
# Output:
#   - Setup URI printed to stdout
#   - URI passphrase printed to stdout (save it!)
#   - QR code printed to terminal (if qrencode is available)
#   - Both also saved to ~/lobster-config/obsidian-setup-uri.txt
#===============================================================================

set -euo pipefail

# ---------------------------------------------------------------------------
# Colors
# ---------------------------------------------------------------------------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

info()    { echo -e "${BLUE}[INFO]${NC} $1"; }
success() { echo -e "${GREEN}[OK]${NC} $1"; }
warn()    { echo -e "${YELLOW}[WARN]${NC} $1"; }
error()   { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
LOBSTER_CONFIG="${LOBSTER_CONFIG_DIR:-$HOME/lobster-config}"
OBSIDIAN_ENV="$LOBSTER_CONFIG/obsidian.env"
OUTPUT_FILE="$LOBSTER_CONFIG/obsidian-setup-uri.txt"

# Deno — prefer $PATH, fall back to ~/.deno/bin/deno
DENO_BIN="${DENO_EXEC:-}"
if [[ -z "$DENO_BIN" ]]; then
    if command -v deno &>/dev/null; then
        DENO_BIN="deno"
    elif [[ -x "$HOME/.deno/bin/deno" ]]; then
        DENO_BIN="$HOME/.deno/bin/deno"
    fi
fi

# ---------------------------------------------------------------------------
# Step 1: Load credentials
# ---------------------------------------------------------------------------
if [[ ! -f "$OBSIDIAN_ENV" ]]; then
    error "Config file not found: $OBSIDIAN_ENV\nCreate it with COUCHDB_USER, COUCHDB_PASSWORD, COUCHDB_DATABASE."
fi

# Source the env file (ignore blank lines and comments)
# shellcheck disable=SC1090
set -a
source "$OBSIDIAN_ENV"
set +a

COUCHDB_USER="${COUCHDB_USER:-}"
COUCHDB_PASSWORD="${COUCHDB_PASSWORD:-}"
COUCHDB_DATABASE="${COUCHDB_DATABASE:-obsidian}"
COUCHDB_HOST_OVERRIDE="${COUCHDB_HOST:-}"
COUCHDB_PORT_OVERRIDE="${COUCHDB_PORT:-5984}"
E2EE_PASSPHRASE="${OBSIDIAN_PASSPHRASE:-}"  # Optional E2EE passphrase

[[ -z "$COUCHDB_USER" ]]     && error "COUCHDB_USER not set in $OBSIDIAN_ENV"
[[ -z "$COUCHDB_PASSWORD" ]] && error "COUCHDB_PASSWORD not set in $OBSIDIAN_ENV"

# ---------------------------------------------------------------------------
# Step 2: Resolve hostname
# ---------------------------------------------------------------------------
info "Resolving external IP..."

EXTERNAL_IP=""
if [[ -n "$COUCHDB_HOST_OVERRIDE" && "$COUCHDB_HOST_OVERRIDE" != "127.0.0.1" && "$COUCHDB_HOST_OVERRIDE" != "localhost" ]]; then
    # Use whatever is explicitly configured (e.g., a domain name)
    EXTERNAL_IP="$COUCHDB_HOST_OVERRIDE"
    info "Using configured host: $EXTERNAL_IP"
else
    EXTERNAL_IP="$(curl -s --max-time 10 ifconfig.me 2>/dev/null || true)"
    if [[ -z "$EXTERNAL_IP" ]]; then
        warn "Could not fetch external IP from ifconfig.me — trying icanhazip.com"
        EXTERNAL_IP="$(curl -s --max-time 10 icanhazip.com 2>/dev/null | tr -d '[:space:]' || true)"
    fi
    if [[ -z "$EXTERNAL_IP" ]]; then
        error "Could not determine external IP. Set COUCHDB_HOST in $OBSIDIAN_ENV to override."
    fi
    info "External IP: $EXTERNAL_IP"
fi

# Wrap IPv6 addresses in brackets for URL use
HOST_FOR_URL="$EXTERNAL_IP"
if [[ "$EXTERNAL_IP" =~ : && ! "$EXTERNAL_IP" =~ ^\[ ]]; then
    HOST_FOR_URL="[$EXTERNAL_IP]"
fi

# Determine protocol and port
# If HTTPS/TLS proxy is active (port 6984), use https; otherwise http on 5984
if ss -tlnp 2>/dev/null | grep -q ":6984 "; then
    PROTOCOL="https"
    PORT="6984"
elif [[ "$COUCHDB_PORT_OVERRIDE" == "6984" ]]; then
    PROTOCOL="https"
    PORT="6984"
else
    PROTOCOL="http"
    PORT="${COUCHDB_PORT_OVERRIDE:-5984}"
fi

# Build the full hostname URL (no trailing slash)
HOSTNAME_URL="${PROTOCOL}://${HOST_FOR_URL}:${PORT}"
info "CouchDB URL for clients: $HOSTNAME_URL"

# ---------------------------------------------------------------------------
# Step 3: Ensure Deno is available
# ---------------------------------------------------------------------------
if [[ -z "$DENO_BIN" ]]; then
    warn "Deno not found — installing..."
    curl -fsSL https://deno.land/install.sh | sh >/dev/null 2>&1
    DENO_BIN="$HOME/.deno/bin/deno"
    success "Deno installed at $DENO_BIN"
fi

info "Deno: $($DENO_BIN --version 2>&1 | head -1)"

# ---------------------------------------------------------------------------
# Step 4: Generate the Setup URI via the official LiveSync script
# ---------------------------------------------------------------------------
info "Generating Setup URI..."

# The Deno script reads these env vars:
#   hostname, username, password, database, passphrase (E2EE), uri_passphrase (optional)
# We export them and let it run.

export hostname="$HOSTNAME_URL"
export username="$COUCHDB_USER"
export password="$COUCHDB_PASSWORD"
export database="$COUCHDB_DATABASE"
export passphrase="$E2EE_PASSPHRASE"
# Leave uri_passphrase unset so the script auto-generates a memorable one

DENO_OUTPUT="$(
    "$DENO_BIN" run -A \
        "https://raw.githubusercontent.com/vrtmrz/obsidian-livesync/main/utils/flyio/generate_setupuri.ts" \
        2>&1
)"

# Parse the output
SETUP_URI="$(echo "$DENO_OUTPUT" | grep '^obsidian://' | head -1)"
URI_PASSPHRASE_LINE="$(echo "$DENO_OUTPUT" | grep 'passphrase of Setup-URI' | head -1)"
URI_PASSPHRASE="$(echo "$URI_PASSPHRASE_LINE" | sed 's/.*: *//')"

if [[ -z "$SETUP_URI" ]]; then
    echo "Deno output was:"
    echo "$DENO_OUTPUT"
    error "Failed to extract Setup URI from Deno output."
fi

# ---------------------------------------------------------------------------
# Step 5: Display results
# ---------------------------------------------------------------------------
echo ""
echo -e "${GREEN}${BOLD}========================================${NC}"
echo -e "${GREEN}${BOLD}  LiveSync Setup URI Generated!${NC}"
echo -e "${GREEN}${BOLD}========================================${NC}"
echo ""
echo -e "${CYAN}${BOLD}Setup URI:${NC}"
echo "$SETUP_URI"
echo ""
echo -e "${CYAN}${BOLD}URI Passphrase (save this!):${NC}"
echo "  $URI_PASSPHRASE"
echo ""
echo -e "${YELLOW}IMPORTANT: The URI passphrase decrypts the Setup URI in Obsidian."
echo "Save it somewhere safe — it is not stored anywhere on this server.${NC}"
echo ""

# ---------------------------------------------------------------------------
# Step 6: Save to file
# ---------------------------------------------------------------------------
{
    echo "# Obsidian LiveSync Setup URI"
    echo "# Generated: $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
    echo "# Server: $HOSTNAME_URL  Database: $COUCHDB_DATABASE"
    echo ""
    echo "SETUP_URI=$SETUP_URI"
    echo ""
    echo "URI_PASSPHRASE=$URI_PASSPHRASE"
    echo ""
    echo "# To reconfigure Obsidian on a new device:"
    echo "#   1. Tap the Setup URI above (or paste it in Obsidian)"
    echo "#   2. Enter the URI Passphrase when prompted"
    echo "#   3. LiveSync will be configured automatically"
} > "$OUTPUT_FILE"
chmod 600 "$OUTPUT_FILE"
success "Saved to $OUTPUT_FILE"

# ---------------------------------------------------------------------------
# Step 7: QR code (optional)
# ---------------------------------------------------------------------------
if command -v qrencode &>/dev/null; then
    echo ""
    echo -e "${CYAN}${BOLD}QR Code (scan with mobile Obsidian):${NC}"
    echo ""
    qrencode -t ANSIUTF8 "$SETUP_URI" 2>/dev/null || warn "QR code generation failed"
else
    echo ""
    info "Tip: install qrencode for QR code output (sudo apt install qrencode)"
    info "Then scan the QR code with Obsidian on mobile."
fi

echo ""
echo -e "${GREEN}${BOLD}How to use:${NC}"
echo "  Mobile: tap the Setup URI link, or scan the QR code"
echo "  Desktop: paste the URI into Obsidian, then enter the passphrase"
echo ""
echo "  Obsidian will prompt: 'Use the copied setup URI'"
echo "  Enter passphrase: $URI_PASSPHRASE"
echo ""
