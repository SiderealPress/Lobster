#!/bin/bash
#===============================================================================
# Shared template processing library
#
# Canonical single implementation of _tmpl_generate_from_template().
# Source this file from install.sh, update-lobster.sh, or any other script
# that needs to render {{PLACEHOLDER}} service templates.
#
# NOTE: This is the single source of truth for template substitution.
# If the placeholder set changes here, it changes everywhere — that is the
# point. Do NOT copy this logic into another script; source this file instead.
#
# Required variables (set by the calling script before calling the function):
#   LOBSTER_USER        — system user running lobster        (maps to {{USER}})
#   LOBSTER_GROUP       — system group                       (maps to {{GROUP}})
#   LOBSTER_HOME        — home directory                     (maps to {{HOME}})
#   LOBSTER_INSTALL_DIR — repo root                          (maps to {{INSTALL_DIR}})
#   LOBSTER_WORKSPACE   — workspace dir                      (maps to {{WORKSPACE_DIR}})
#   LOBSTER_MESSAGES    — messages dir                       (maps to {{MESSAGES_DIR}})
#   LOBSTER_CONFIG_DIR  — config dir                         (maps to {{CONFIG_DIR}})
#   LOBSTER_USER_CONFIG — user-config dir                    (maps to {{USER_CONFIG_DIR}})
#
# Each calling script defines its own generate_from_template() wrapper that
# calls _tmpl_generate_from_template(), allowing caller-specific logging:
#
#   source "$(dirname "$0")/lib/template.sh"
#   generate_from_template() {
#       _tmpl_generate_from_template "$1" "$2" || return 1
#       success "Generated: $2"   # caller's logging function
#   }
#===============================================================================

# _tmpl_generate_from_template TEMPLATE OUTPUT
#
# Core implementation: substitutes all 8 {{PLACEHOLDER}} variables in TEMPLATE
# and writes OUTPUT.  Fails if TEMPLATE is missing or any placeholder is left
# unresolved after substitution.
_tmpl_generate_from_template() {
    local template="$1"
    local output="$2"

    if [ ! -f "$template" ]; then
        echo "[ERROR] Template not found: $template" >&2
        return 1
    fi

    # Guard: reject poisoned inputs before substitution.
    #
    # LOBSTER_* vars are frequently defaulted with "${LOBSTER_FOO:-default}"
    # by callers. That pattern only applies the default when the variable is
    # UNSET or EMPTY — if a parent process (e.g. a systemd unit whose
    # Environment= line itself still has a raw {{PLACEHOLDER}}) exports the
    # variable with a bad value, the default is silently skipped and the bad
    # value flows straight through sed, re-poisoning every freshly generated
    # service file forever. Catch that here, at the one place all callers
    # funnel through, instead of relying on every caller to remember to
    # validate its own inputs.
    local _var _val
    for _var in LOBSTER_USER LOBSTER_GROUP LOBSTER_HOME LOBSTER_INSTALL_DIR \
                LOBSTER_WORKSPACE LOBSTER_MESSAGES LOBSTER_CONFIG_DIR LOBSTER_USER_CONFIG; do
        _val="${!_var:-}"
        case "$_val" in
            *'{{'*)
                echo "[ERROR] $_var is set to an unrendered template placeholder: '$_val'" >&2
                echo "[ERROR] Refusing to render $template — this would re-poison $output." >&2
                echo "[ERROR] Check what exported $_var (e.g. 'env | grep $_var', or the" >&2
                echo "[ERROR] Environment= lines of the systemd unit this process inherited" >&2
                echo "[ERROR] its environment from) and fix or unset it before retrying." >&2
                return 1
                ;;
        esac
    done

    sed -e "s|{{USER}}|${LOBSTER_USER}|g" \
        -e "s|{{GROUP}}|${LOBSTER_GROUP}|g" \
        -e "s|{{HOME}}|${LOBSTER_HOME}|g" \
        -e "s|{{INSTALL_DIR}}|${LOBSTER_INSTALL_DIR}|g" \
        -e "s|{{WORKSPACE_DIR}}|${LOBSTER_WORKSPACE}|g" \
        -e "s|{{MESSAGES_DIR}}|${LOBSTER_MESSAGES}|g" \
        -e "s|{{CONFIG_DIR}}|${LOBSTER_CONFIG_DIR}|g" \
        -e "s|{{USER_CONFIG_DIR}}|${LOBSTER_USER_CONFIG}|g" \
        "$template" > "$output"

    # Guard: fail if any placeholder remains unresolved
    if grep -q '{{' "$output" 2>/dev/null; then
        local unresolved
        unresolved=$(grep -o '{{[^}]*}}' "$output" | sort -u | tr '\n' ' ')
        echo "[ERROR] Unresolved placeholders in $output: $unresolved" >&2
        rm -f "$output"
        return 1
    fi
}
