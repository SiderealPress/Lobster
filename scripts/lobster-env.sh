#!/bin/bash
#===============================================================================
# lobster-env - Manage the global Lobster environment variable store
#
# Usage:
#   lobster env set KEY VALUE    Add or update a key
#   lobster env get KEY          Print the value of a key
#   lobster env list             List all keys (values hidden for security)
#   lobster env edit             Open the file in $EDITOR
#   lobster env path             Print the path to the global env file
#
# The global env file lives at: ~/lobster-config/global.env
# (or $LOBSTER_CONFIG_DIR/global.env if LOBSTER_CONFIG_DIR is set)
#===============================================================================

set -euo pipefail

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

# Resolve the global env file path
CONFIG_DIR="${LOBSTER_CONFIG_DIR:-$HOME/lobster-config}"
GLOBAL_ENV="$CONFIG_DIR/global.env"

#-------------------------------------------------------------------------------
# Ensure the global env file exists with correct permissions
#-------------------------------------------------------------------------------
_ensure_file() {
    if [ ! -d "$CONFIG_DIR" ]; then
        mkdir -p "$CONFIG_DIR"
        chmod 700 "$CONFIG_DIR"
    fi

    if [ ! -f "$GLOBAL_ENV" ]; then
        cat > "$GLOBAL_ENV" << 'ENVTEMPLATE'
# Lobster Global Environment Store
# Machine-wide API tokens and credentials shared across services and tools.
# Format: KEY=value (no export keyword needed)
# Run: lobster env set KEY VALUE  to add or update a key
# Run: lobster env list            to see all stored keys

# === Cloud Providers ===
# HETZNER_API_TOKEN=
# DO_TOKEN=
# CLOUDFLARE_API_TOKEN=

# === AI / LLM Services ===
# ANTHROPIC_API_KEY=
# OPENAI_API_KEY=

# === Code / DevOps ===
# GITHUB_TOKEN=
# VERCEL_TOKEN=

# === Communication Services ===
# TWILIO_ACCOUNT_SID=
# TWILIO_AUTH_TOKEN=

# === Add your own below ===
ENVTEMPLATE
        chmod 600 "$GLOBAL_ENV"
    fi
}

#-------------------------------------------------------------------------------
# Commands
#-------------------------------------------------------------------------------

cmd_set() {
    local key="$1"
    local value="$2"

    # Validate key name (alphanumeric and underscores only)
    if ! [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
        echo -e "${RED}Error:${NC} Invalid key name '$key'. Keys must start with a letter or underscore and contain only alphanumeric characters and underscores." >&2
        exit 1
    fi

    _ensure_file

    # Use Python for in-place replacement so special characters in values
    # (|, &, \, etc.) are handled correctly without sed delimiter collisions.
    local action
    action=$(python3 - "$GLOBAL_ENV" "$key" "$value" << 'PYEOF'
import re, sys, os, tempfile

env_file, key, value = sys.argv[1], sys.argv[2], sys.argv[3]

with open(env_file, 'r') as f:
    content = f.read()

# Use a lambda repl to prevent backslash and ampersand interpretation
def make_repl(k, v):
    line = k + '=' + v
    return lambda m: line

active_pattern  = r'^' + re.escape(key) + r'=.*'
comment_pattern = r'^#[ \t]*' + re.escape(key) + r'=.*'

if re.search(active_pattern, content, re.MULTILINE):
    new_content = re.sub(active_pattern, make_repl(key, value), content, flags=re.MULTILINE)
    action = 'updated'
elif re.search(comment_pattern, content, re.MULTILINE):
    new_content = re.sub(comment_pattern, make_repl(key, value), content, flags=re.MULTILINE)
    action = 'uncommented'
else:
    new_content = content.rstrip('\n') + '\n' + key + '=' + value + '\n'
    action = 'appended'

# Write atomically: temp file in same dir, then rename
dir_path = os.path.dirname(os.path.abspath(env_file))
fd, tmp_path = tempfile.mkstemp(dir=dir_path)
try:
    with os.fdopen(fd, 'w') as f:
        f.write(new_content)
    os.chmod(tmp_path, 0o600)
    os.replace(tmp_path, env_file)
except Exception as e:
    try:
        os.unlink(tmp_path)
    except OSError:
        pass
    raise

print(action)
PYEOF
    )

    if [ $? -ne 0 ]; then
        echo -e "${RED}Error:${NC} Failed to write to $GLOBAL_ENV" >&2
        exit 1
    fi

    case "$action" in
        updated)     echo -e "${GREEN}Updated${NC} $key" ;;
        uncommented) echo -e "${GREEN}Set${NC} $key (was commented out)" ;;
        *)           echo -e "${GREEN}Set${NC} $key" ;;
    esac
}

cmd_get() {
    local key="$1"

    if [ ! -f "$GLOBAL_ENV" ]; then
        echo -e "${YELLOW}No global env file found at $GLOBAL_ENV${NC}" >&2
        exit 1
    fi

    local value
    value=$(grep "^${key}=" "$GLOBAL_ENV" | head -1 | cut -d= -f2-)

    if [ -z "$value" ]; then
        # Check if the key exists but is empty
        if grep -q "^${key}=" "$GLOBAL_ENV"; then
            # Key exists but value is empty — print nothing, exit 0
            true
        else
            echo -e "${YELLOW}Key '$key' not found in $GLOBAL_ENV${NC}" >&2
            exit 1
        fi
    else
        echo "$value"
    fi
}

cmd_list() {
    if [ ! -f "$GLOBAL_ENV" ]; then
        echo -e "${YELLOW}No global env file found at $GLOBAL_ENV${NC}"
        echo "Create one with: lobster env set KEY VALUE"
        return 0
    fi

    local count=0
    echo -e "${CYAN}Keys in $GLOBAL_ENV:${NC}"
    echo ""

    while IFS= read -r line; do
        # Skip comments and blank lines
        [[ "$line" =~ ^#.*$ || -z "$line" ]] && continue
        # Extract key name (everything before the first =)
        local key="${line%%=*}"
        if [ -n "$key" ]; then
            echo "  $key"
            count=$((count + 1))
        fi
    done < "$GLOBAL_ENV"

    echo ""
    if [ "$count" -eq 0 ]; then
        echo -e "${YELLOW}No keys set yet. Use: lobster env set KEY VALUE${NC}"
    else
        echo "$count key(s) stored. Values hidden for security."
        echo "Use 'lobster env get KEY' to retrieve a specific value."
    fi
}

cmd_edit() {
    _ensure_file
    local editor="${EDITOR:-${VISUAL:-vi}}"
    echo "Opening $GLOBAL_ENV in $editor..."
    "$editor" "$GLOBAL_ENV"
}

cmd_path() {
    echo "$GLOBAL_ENV"
}

cmd_help() {
    echo "Usage: lobster env <command> [args]"
    echo ""
    echo "Commands:"
    echo "  set KEY VALUE   Add or update a key in the global env store"
    echo "  get KEY         Print the value of a key"
    echo "  list            List all key names (values hidden for security)"
    echo "  edit            Open the global env file in \$EDITOR"
    echo "  path            Print the path to the global env file"
    echo "  help            Show this help"
    echo ""
    echo "File: $GLOBAL_ENV"
    echo ""
    echo "Examples:"
    echo "  lobster env set HETZNER_API_TOKEN abc123"
    echo "  lobster env get GITHUB_TOKEN"
    echo "  lobster env list"
}

#-------------------------------------------------------------------------------
# Main
#-------------------------------------------------------------------------------

COMMAND="${1:-help}"
shift || true

case "$COMMAND" in
    set)
        if [ $# -lt 2 ]; then
            echo -e "${RED}Error:${NC} 'lobster env set' requires KEY and VALUE arguments" >&2
            echo "Usage: lobster env set KEY VALUE" >&2
            exit 1
        fi
        cmd_set "$1" "$2"
        ;;
    get)
        if [ $# -lt 1 ]; then
            echo -e "${RED}Error:${NC} 'lobster env get' requires a KEY argument" >&2
            echo "Usage: lobster env get KEY" >&2
            exit 1
        fi
        cmd_get "$1"
        ;;
    list|ls)
        cmd_list
        ;;
    edit)
        cmd_edit
        ;;
    path)
        cmd_path
        ;;
    help|--help|-h)
        cmd_help
        ;;
    *)
        echo -e "${RED}Unknown env command:${NC} $COMMAND" >&2
        echo "Run 'lobster env help' for usage." >&2
        exit 1
        ;;
esac
