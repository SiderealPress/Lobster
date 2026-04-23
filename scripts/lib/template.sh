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
