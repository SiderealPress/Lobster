#!/usr/bin/env bash
# =============================================================================
# install-pii-scan-hook.sh
#
# Installs hooks/pii-scan-guard.py as the `.git/hooks/pre-push` hook in a
# target repository, so every `git push` from that repo — however it is
# invoked (a plain terminal, a script, an IDE, or a Claude Code Bash tool
# call) — runs the Fable-5-backed PII scan (fails closed in block mode if
# the scanner itself cannot produce a verdict — see hooks/pii-scan-guard.py's
# module docstring for the full fail-closed rationale).
#
# Usage:
#   scripts/install-pii-scan-hook.sh [target-repo-path]
#
#   target-repo-path defaults to the repo containing the current directory.
#   Any repo under ~/lobster-workspace/projects/ (or elsewhere) can be
#   targeted — this is intentionally NOT specific to SiderealPress/lobster.
#
# NOT run automatically by install.sh or upgrade.sh. Installing this hook is
# a deliberate, explicit action an operator takes after reviewing the design
# — see the module docstring in hooks/pii-scan-guard.py. Even once installed,
# the hook is inert by default: LOBSTER_PII_SCAN_MODE defaults to "off" and
# must be set to "warn" or "block" (in config.env or the environment) before
# the hook does anything.
#
# If a pre-push hook already exists at the target and is not a previous
# install of this same hook, the script refuses to overwrite it and exits
# non-zero — pass --force to overwrite anyway (the previous hook is backed up
# to pre-push.pre-pii-scan-backup first).
# =============================================================================

set -euo pipefail

FORCE=0
TARGET_ARG=""
for arg in "$@"; do
    case "$arg" in
        --force) FORCE=1 ;;
        *) TARGET_ARG="$arg" ;;
    esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOBSTER_REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
HOOK_SOURCE="$LOBSTER_REPO_ROOT/hooks/pii-scan-guard.py"
PROMPT_SOURCE="$LOBSTER_REPO_ROOT/hooks/pii-scan-guard.prompt.md"

if [[ ! -f "$HOOK_SOURCE" ]]; then
    echo "ERROR: $HOOK_SOURCE not found. Run this from a lobster checkout." >&2
    exit 1
fi
if [[ ! -f "$PROMPT_SOURCE" ]]; then
    echo "ERROR: $PROMPT_SOURCE not found (scanner system prompt)." >&2
    exit 1
fi

TARGET_DIR="${TARGET_ARG:-.}"
TARGET_REPO_ROOT="$(cd "$TARGET_DIR" && git rev-parse --show-toplevel 2>/dev/null)" || {
    echo "ERROR: '$TARGET_DIR' is not inside a git repository." >&2
    exit 1
}

GIT_HOOKS_DIR="$TARGET_REPO_ROOT/.git/hooks"
if [[ ! -d "$GIT_HOOKS_DIR" ]]; then
    echo "ERROR: $GIT_HOOKS_DIR does not exist (unusual repo layout — is this a worktree?)." >&2
    exit 1
fi

DEST_HOOK="$GIT_HOOKS_DIR/pre-push"
DEST_PROMPT="$GIT_HOOKS_DIR/pii-scan-guard.prompt.md"

MARKER="# installed-by: install-pii-scan-hook.sh"

if [[ -e "$DEST_HOOK" ]]; then
    if grep -qF "$MARKER" "$DEST_HOOK" 2>/dev/null; then
        echo "[install-pii-scan-hook] Existing install found at $DEST_HOOK — updating in place."
    elif [[ "$FORCE" -eq 1 ]]; then
        BACKUP="$GIT_HOOKS_DIR/pre-push.pre-pii-scan-backup"
        echo "[install-pii-scan-hook] --force set: backing up existing pre-push hook to $BACKUP"
        cp "$DEST_HOOK" "$BACKUP"
    else
        echo "ERROR: $DEST_HOOK already exists and is not a previous install of this" >&2
        echo "  hook (no '$MARKER' marker found). Refusing to overwrite a hook that" >&2
        echo "  might be doing something else (e.g. scripts/pre-push-security-scan.sh)." >&2
        echo "  Re-run with --force to overwrite (the existing hook is backed up first)." >&2
        exit 1
    fi
fi

cp "$PROMPT_SOURCE" "$DEST_PROMPT"

cat > "$DEST_HOOK" <<EOF
#!/usr/bin/env bash
$MARKER
# Source: $HOOK_SOURCE
# Re-run scripts/install-pii-scan-hook.sh to update after a hook change.
exec python3 "$HOOK_SOURCE" "\$@"
EOF
chmod +x "$DEST_HOOK"

echo "[install-pii-scan-hook] Installed pre-push hook at $DEST_HOOK"
echo "[install-pii-scan-hook] Mode is controlled by LOBSTER_PII_SCAN_MODE (config.env or environment):"
echo "  off   (default, and the current state — nothing happens on push)"
echo "  warn  (scans and prints findings, never blocks)"
echo "  block (scans and blocks the push on a confident finding)"
echo "[install-pii-scan-hook] This hook does nothing until LOBSTER_PII_SCAN_MODE is set explicitly."
