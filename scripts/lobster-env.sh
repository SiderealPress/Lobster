#!/usr/bin/env bash
# lobster-env.sh — Manage the Lobster global environment variable store
#
# Subcommands:
#   set KEY VALUE   — Add or update a key in global.env
#   get KEY         — Print the current value of a key
#   list            — List all non-comment, non-empty keys
#   edit            — Open global.env in $EDITOR
#   path            — Print the path to global.env
#
# Storage: ~/lobster-config/global.env (or $LOBSTER_CONFIG_DIR/global.env)
# The file is loaded by lobster-router.service after config.env, so values
# here override the defaults in config/config.env.
#
# IMPORTANT — service-critical Telegram variables:
#   TELEGRAM_BOT_TOKEN, TELEGRAM_ALLOWED_USERS, and ADMIN_CHAT_ID are defined
#   (possibly empty) in ~/lobster-config/config.env which is loaded before
#   global.env. In systemd, later EnvironmentFile entries override earlier ones,
#   so setting these in global.env DOES take effect — but only if the entry in
#   config.env uses the bare form KEY= (empty). If config.env already has a
#   non-empty value, edit config.env directly instead.
#
# Issue: #1454

set -e

CONFIG_DIR="${LOBSTER_CONFIG_DIR:-$HOME/lobster-config}"
GLOBAL_ENV="$CONFIG_DIR/global.env"

# Colour helpers (no-op if stdout is not a terminal)
if [ -t 1 ]; then
    RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
    BLUE='\033[0;34m'; NC='\033[0m'
else
    RED=''; GREEN=''; YELLOW=''; BLUE=''; NC=''
fi

_die() { echo -e "${RED}Error: $*${NC}" >&2; exit 1; }

_ensure_file() {
    if [ ! -f "$GLOBAL_ENV" ]; then
        _die "global.env not found at $GLOBAL_ENV — run 'lobster install' first"
    fi
}

_cmd_set() {
    local key="$1" value="$2"
    [ -z "$key" ] && _die "Usage: lobster env set KEY VALUE"
    [ -z "$value" ] && _die "Usage: lobster env set KEY VALUE (value cannot be empty — use 'lobster env edit' to set an empty value)"

    # Validate key: only word chars and underscores
    if ! echo "$key" | grep -qE '^[A-Za-z_][A-Za-z0-9_]*$'; then
        _die "Invalid key '$key' — must match [A-Za-z_][A-Za-z0-9_]*"
    fi

    _ensure_file

    # Escape value: wrap in single quotes, escaping any embedded single quotes.
    # Use printf to avoid echo interpretation of backslashes.
    local escaped_value
    escaped_value=$(printf '%s' "$value" | sed "s/'/'\\\\''/g")
    local new_line="${key}='${escaped_value}'"

    if grep -qE "^#?[[:space:]]*${key}=" "$GLOBAL_ENV" 2>/dev/null; then
        # Replace existing line (commented or active)
        sed -i "s|^#\{0,1\}[[:space:]]*${key}=.*|${new_line}|" "$GLOBAL_ENV"
        echo -e "${GREEN}Updated${NC} ${BLUE}${key}${NC} in $GLOBAL_ENV"
    else
        # Append at end of file
        printf '\n%s\n' "$new_line" >> "$GLOBAL_ENV"
        echo -e "${GREEN}Added${NC} ${BLUE}${key}${NC} to $GLOBAL_ENV"
    fi

    echo -e "${YELLOW}Tip:${NC} Restart services for the change to take effect: lobster restart"
}

_cmd_get() {
    local key="$1"
    [ -z "$key" ] && _die "Usage: lobster env get KEY"
    _ensure_file

    local value
    # Source the file in a subshell to expand quoting, then echo the variable.
    value=$(
        set -a
        # shellcheck disable=SC1090
        . "$GLOBAL_ENV"
        eval "printf '%s' \"\${${key}:-}\""
    )

    if [ -z "$value" ]; then
        # Check if the key exists at all (even with empty value)
        if grep -qE "^[[:space:]]*${key}=" "$GLOBAL_ENV" 2>/dev/null; then
            echo -e "${YELLOW}(empty)${NC}" >&2
        else
            echo -e "${YELLOW}(not set)${NC}" >&2
            exit 1
        fi
    else
        printf '%s\n' "$value"
    fi
}

_cmd_list() {
    _ensure_file

    echo -e "${BLUE}Keys in $GLOBAL_ENV:${NC}"
    local found=0
    while IFS= read -r line; do
        # Skip comments and blank lines
        [[ "$line" =~ ^[[:space:]]*# ]] && continue
        [[ -z "${line//[[:space:]]/}" ]] && continue
        # Only print KEY= lines with a non-empty value
        if [[ "$line" =~ ^([A-Za-z_][A-Za-z0-9_]*)=(.+)$ ]]; then
            local k="${BASH_REMATCH[1]}"
            echo "  $k"
            found=1
        fi
    done < "$GLOBAL_ENV"

    if [ "$found" -eq 0 ]; then
        echo -e "  ${YELLOW}(no keys set)${NC}"
    fi
}

_cmd_edit() {
    _ensure_file
    local editor="${EDITOR:-${VISUAL:-vi}}"
    exec "$editor" "$GLOBAL_ENV"
}

_cmd_path() {
    echo "$GLOBAL_ENV"
}

# Dispatch
cmd="${1:-}"
shift 2>/dev/null || true

case "$cmd" in
    set)  _cmd_set "$@" ;;
    get)  _cmd_get "$@" ;;
    list) _cmd_list ;;
    edit) _cmd_edit ;;
    path) _cmd_path ;;
    "")
        echo -e "${BLUE}lobster env${NC} — manage the global environment variable store"
        echo ""
        echo "Usage: lobster env <subcommand> [args]"
        echo ""
        echo "Subcommands:"
        echo "  set KEY VALUE   Add or update a key in global.env"
        echo "  get KEY         Print the current value of a key"
        echo "  list            List all non-empty keys"
        echo "  edit            Open global.env in \$EDITOR"
        echo "  path            Print path to global.env"
        echo ""
        echo "File: $GLOBAL_ENV"
        ;;
    *)
        _die "Unknown subcommand '$cmd'. Run 'lobster env' for usage."
        ;;
esac
