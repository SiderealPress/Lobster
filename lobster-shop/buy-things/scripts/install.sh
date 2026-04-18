#!/usr/bin/env bash
# =============================================================================
# buy-things skill installer
# =============================================================================
# Usage: bash scripts/install.sh [--no-onboarding]
#
# Creates required directories, initializes spend log, and optionally
# runs onboarding to capture payment card details.
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(dirname "$SCRIPT_DIR")"
MESSAGES_DIR="${LOBSTER_MESSAGES:-$HOME/messages}"
CONFIG_DIR="$MESSAGES_DIR/config"
RECEIPTS_DIR="$MESSAGES_DIR/receipts"
PAYMENT_YAML="$CONFIG_DIR/payment.yaml"
SPEND_LOG="$CONFIG_DIR/spend_log.yaml"

BLUE='\033[0;34m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

info()    { echo -e "${BLUE}[buy-things]${NC} $*"; }
success() { echo -e "${GREEN}[buy-things]${NC} $*"; }
warn()    { echo -e "${YELLOW}[buy-things]${NC} $*"; }
error()   { echo -e "${RED}[buy-things]${NC} $*" >&2; }

# =============================================================================
# 1. Create required directories
# =============================================================================

info "Creating required directories..."
mkdir -p "$CONFIG_DIR"
mkdir -p "$RECEIPTS_DIR"
success "Directories ready: $CONFIG_DIR, $RECEIPTS_DIR"

# =============================================================================
# 2. Ensure Python dependency (pyyaml)
# =============================================================================

info "Checking Python dependencies..."
if python3 -c "import yaml" 2>/dev/null; then
    success "pyyaml is available"
else
    info "Installing pyyaml..."
    pip install pyyaml --quiet || {
        warn "pip install failed — trying uv..."
        uv pip install pyyaml --quiet || error "Could not install pyyaml. Run: pip install pyyaml"
    }
fi

# =============================================================================
# 3. Initialize spend log if missing
# =============================================================================

if [[ ! -f "$SPEND_LOG" ]]; then
    info "Initializing spend log at $SPEND_LOG..."
    cat > "$SPEND_LOG" <<'EOF'
purchases: []
EOF
    success "Spend log created: $SPEND_LOG"
else
    info "Spend log already exists: $SPEND_LOG"
fi

# =============================================================================
# 4. Check payment config
# =============================================================================

if [[ -f "$PAYMENT_YAML" ]]; then
    # Verify it has required fields
    if python3 -c "
import yaml, sys
data = yaml.safe_load(open('$PAYMENT_YAML'))
required = ['number', 'expiry', 'cvv']
card = data.get('card', {})
missing = [k for k in required if not card.get(k)]
if missing:
    print('MISSING: ' + ', '.join(missing))
    sys.exit(1)
print('OK')
" 2>/dev/null; then
        success "Payment config exists and is valid: $PAYMENT_YAML"
        NEEDS_ONBOARDING=false
    else
        warn "Payment config exists but is missing required fields — onboarding needed"
        NEEDS_ONBOARDING=true
    fi
else
    info "No payment config found — onboarding required"
    NEEDS_ONBOARDING=true
fi

# Enforce chmod 600 on payment file if it exists
if [[ -f "$PAYMENT_YAML" ]]; then
    chmod 600 "$PAYMENT_YAML"
    info "Permissions enforced: chmod 600 on $PAYMENT_YAML"
fi

# =============================================================================
# 5. Register skill with Lobster skill manager
# =============================================================================

LOBSTER_DIR="${LOBSTER_INSTALL_DIR:-$HOME/lobster}"
SKILL_NAME="buy-things"
STATE_FILE="${LOBSTER_MESSAGES:-$HOME/messages}/config/skills-state.json"

info "Registering skill '$SKILL_NAME' with Lobster..."
python3 - <<PYEOF
import sys
sys.path.insert(0, "$LOBSTER_DIR/src")
try:
    from mcp.skill_manager import mark_installed
    mark_installed("$SKILL_NAME", "1.0.0")
    print("[buy-things] Skill registered in skills-state.json")
except Exception as e:
    print(f"[buy-things] Warning: could not register skill: {e}")
    print("[buy-things] You can activate it manually via the activate_skill MCP tool")
PYEOF

# =============================================================================
# 6. Onboarding prompt
# =============================================================================

if [[ "${1:-}" == "--no-onboarding" ]]; then
    info "Skipping onboarding (--no-onboarding flag)"
elif [[ "$NEEDS_ONBOARDING" == "true" ]]; then
    echo ""
    echo "============================================================"
    echo "  Buy-Things Skill — Payment Setup Required"
    echo "============================================================"
    echo ""
    echo "  No payment config found. To complete setup, send this"
    echo "  message to your Lobster bot on Telegram:"
    echo ""
    echo "    /buy setup"
    echo ""
    echo "  Lobster will guide you through entering your card details"
    echo "  securely via Telegram."
    echo ""
    echo "  Alternatively, create $PAYMENT_YAML manually:"
    echo ""
    cat <<'TEMPLATE'
  card:
    number: "XXXXXXXXXXXXXXXXXXXX"
    expiry: "MM/YY"
    cvv: "XXX"
    billing_address:
      street: "123 Main St"
      city: "Your City"
      state: "CA"
      zip: "00000"
      country: "US"

  spending:
    monthly_limit_usd: 1000
    tracker_file: "~/messages/config/spend_log.yaml"
TEMPLATE
    echo ""
    echo "  After creating the file, run: chmod 600 $PAYMENT_YAML"
    echo "============================================================"
else
    success "Payment config is ready — no onboarding needed"
fi

# =============================================================================
# Done
# =============================================================================

echo ""
success "buy-things skill installed successfully!"
echo ""
echo "  Activate in Lobster:  tell Lobster to activate the buy-things skill"
echo "  Or via MCP tool:      activate_skill(skill_name='buy-things')"
echo ""
echo "  Commands:"
echo "    /buy <item>     — search and purchase an item"
echo "    /order <item>   — same as /buy"
echo "    /spend          — this month's spending vs cap"
echo "    /receipts       — recent purchase history"
echo "    /buy setup      — update payment card"
echo ""
