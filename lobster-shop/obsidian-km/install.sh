#!/usr/bin/env bash
# =============================================================================
# Obsidian KM Skill — Installer
# =============================================================================
# Installs the Obsidian Knowledge Management skill for Lobster.
# Creates the vault structure, syncs config, and registers MCP tools.
#
# Usage: bash install.sh
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VAULT_DIR="${OBSIDIAN_VAULT_DIR:-$HOME/obsidian-vault}"
SCAFFOLD_DIR="$SCRIPT_DIR/vault-scaffold"

# -----------------------------------------------------------------------------
# Logging
# -----------------------------------------------------------------------------
log_info() { echo "[INFO] $*"; }
log_warn() { echo "[WARN] $*" >&2; }
log_error() { echo "[ERROR] $*" >&2; }

# -----------------------------------------------------------------------------
# Vault Creation
# -----------------------------------------------------------------------------
# Creates the vault directory structure idempotently.
# Does not overwrite existing content — only creates missing directories/files.
create_vault() {
    log_info "Creating vault at $VAULT_DIR..."

    # Create main vault directory
    if [[ ! -d "$VAULT_DIR" ]]; then
        mkdir -p "$VAULT_DIR"
        log_info "Created $VAULT_DIR"
    else
        log_info "Vault directory already exists: $VAULT_DIR"
    fi

    # Create subdirectories (idempotent)
    local dirs=(Inbox Notes Links Daily Archive .obsidian)
    for dir in "${dirs[@]}"; do
        if [[ ! -d "$VAULT_DIR/$dir" ]]; then
            mkdir -p "$VAULT_DIR/$dir"
            log_info "Created $VAULT_DIR/$dir"
        fi
    done

    # Copy template files only if they don't exist
    copy_if_missing() {
        local src="$1"
        local dest="$2"
        if [[ ! -f "$dest" ]]; then
            cp "$src" "$dest"
            log_info "Created $dest"
        else
            log_info "Skipping existing file: $dest"
        fi
    }

    # Copy scaffold files
    copy_if_missing "$SCAFFOLD_DIR/README.md" "$VAULT_DIR/README.md"
    copy_if_missing "$SCAFFOLD_DIR/.gitignore" "$VAULT_DIR/.gitignore"
    copy_if_missing "$SCAFFOLD_DIR/.obsidian/app.json" "$VAULT_DIR/.obsidian/app.json"
    copy_if_missing "$SCAFFOLD_DIR/.obsidian/plugins.json" "$VAULT_DIR/.obsidian/plugins.json"

    # Set permissions
    chmod 750 "$VAULT_DIR"
    log_info "Set permissions: chmod 750 $VAULT_DIR"

    log_info "Vault setup complete!"
}

# -----------------------------------------------------------------------------
# Verification
# -----------------------------------------------------------------------------
verify_vault() {
    log_info "Verifying vault structure..."

    local required_dirs=(Inbox Notes Links Daily Archive .obsidian)
    local missing=0

    for dir in "${required_dirs[@]}"; do
        if [[ ! -d "$VAULT_DIR/$dir" ]]; then
            log_error "Missing directory: $VAULT_DIR/$dir"
            missing=$((missing + 1))
        fi
    done

    local required_files=(README.md .gitignore .obsidian/app.json)
    for file in "${required_files[@]}"; do
        if [[ ! -f "$VAULT_DIR/$file" ]]; then
            log_error "Missing file: $VAULT_DIR/$file"
            missing=$((missing + 1))
        fi
    done

    if [[ $missing -eq 0 ]]; then
        log_info "Vault structure verified successfully!"
        return 0
    else
        log_error "Vault verification failed: $missing missing items"
        return 1
    fi
}

# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------
main() {
    log_info "Installing Obsidian KM skill..."
    log_info "Vault location: $VAULT_DIR"
    log_info "Scaffold source: $SCAFFOLD_DIR"

    # Step 1: Create vault structure
    create_vault

    # Step 2: Verify installation
    verify_vault

    log_info ""
    log_info "Obsidian KM skill installed successfully!"
    log_info ""
    log_info "Next steps:"
    log_info "  1. Open $VAULT_DIR in Obsidian"
    log_info "  2. Configure CouchDB sync (see BIS-230)"
    log_info "  3. Activate the skill: activate_skill('obsidian-km')"
}

# Run main if executed directly
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    main "$@"
fi
