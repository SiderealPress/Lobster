#!/usr/bin/env bash
# =============================================================================
# spam-report skill installer
# =============================================================================
# Usage: bash scripts/install.sh
#
# Creates required directories, initializes the tracking log, and registers
# the skill with Lobster's skill manager.
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(dirname "$SCRIPT_DIR")"
MESSAGES_DIR="${LOBSTER_MESSAGES:-$HOME/messages}"
CONFIG_DIR="$MESSAGES_DIR/config"
RECEIPTS_DIR="$MESSAGES_DIR/receipts/spam"
LOG_PATH="$CONFIG_DIR/spam_reports.yaml"

BLUE='\033[0;34m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()    { echo -e "${BLUE}[spam-report]${NC} $*"; }
success() { echo -e "${GREEN}[spam-report]${NC} $*"; }
warn()    { echo -e "${YELLOW}[spam-report]${NC} $*"; }

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
        uv pip install pyyaml --quiet || warn "Could not install pyyaml. Run: pip install pyyaml"
    }
fi

# =============================================================================
# 3. Initialize tracking log if missing
# =============================================================================

if [[ ! -f "$LOG_PATH" ]]; then
    info "Initializing tracking log at $LOG_PATH..."
    cat > "$LOG_PATH" <<'EOF'
reports: []
EOF
    success "Tracking log created: $LOG_PATH"
else
    info "Tracking log already exists: $LOG_PATH"
fi

# =============================================================================
# 4. Register skill with Lobster skill manager
# =============================================================================

LOBSTER_DIR="${LOBSTER_INSTALL_DIR:-$HOME/lobster}"
SKILL_NAME="spam-report"

info "Registering skill '$SKILL_NAME' with Lobster..."
python3 - <<PYEOF
import sys
sys.path.insert(0, "$LOBSTER_DIR/src")
try:
    from mcp.skill_manager import mark_installed
    mark_installed("$SKILL_NAME", "1.0.0")
    print("[spam-report] Skill registered in skills-state.json")
except Exception as e:
    print(f"[spam-report] Warning: could not register skill: {e}")
    print("[spam-report] You can activate it manually via the activate_skill MCP tool")
PYEOF

# =============================================================================
# Done
# =============================================================================

echo ""
success "spam-report skill installed successfully!"
echo ""
echo "  Activate in Lobster:  tell Lobster to activate the spam-report skill"
echo "  Or via MCP tool:      activate_skill(skill_name='spam-report')"
echo ""
echo "  Commands:"
echo "    /reportspam <number> <description>   — file FTC/FCC complaints + draft 7726 forward"
echo "    /spamlog <number>                     — check if a number was reported before"
echo ""
echo "  Natural language also works: \"report this as spam\", \"file a spam complaint\""
echo ""
echo "  NOTE: carrier 7726 reporting is never automated — this skill only drafts"
echo "  the forward instructions. Drew must forward the message himself."
echo ""
