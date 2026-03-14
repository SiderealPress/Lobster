#!/usr/bin/env bash
# =============================================================================
# pre-push-security-scan.sh
# Comprehensive pre-push Git hook that scans for secrets and PII.
#
# Install: symlink or copy to .git/hooks/pre-push in any repo.
#   ln -sf /home/admin/lobster/scripts/pre-push-security-scan.sh .git/hooks/pre-push
#
# Behavior:
#   - Scans only the commits about to be pushed (not entire history)
#   - BLOCKS push if secrets/credentials are found (exit 1)
#   - WARNS (but allows push) if PII is found
#   - Supports allowlisting via .security-allowlist in repo root
#   - Set SECURITY_SCAN_SKIP=1 to bypass entirely (emergency only)
#
# Exclusions (never scanned):
#   - Test directories: tests/, test/, spec/, or any path with /test
#   - Documentation: .md, .mdx, .rst, .txt, .adoc files
#   - Binary files: non-text files detected by git's binary check
#   - Known safe extensions: lock files, compiled artifacts, images, etc.
# =============================================================================

set -euo pipefail

# --- Configuration -----------------------------------------------------------

RED='\033[0;31m'
YELLOW='\033[1;33m'
GREEN='\033[0;32m'
CYAN='\033[0;36m'
BOLD='\033[1m'
RESET='\033[0m'

CRITICAL_FOUND=0
WARNING_FOUND=0
SCAN_ERRORS=0

# --- Emergency bypass --------------------------------------------------------

if [[ "${SECURITY_SCAN_SKIP:-0}" == "1" ]]; then
    echo -e "${YELLOW}[security-scan] SKIPPED (SECURITY_SCAN_SKIP=1)${RESET}"
    exit 0
fi

# --- Load allowlist ----------------------------------------------------------

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
ALLOWLIST_FILE="${REPO_ROOT}/.security-allowlist"
ALLOWLIST_PATTERNS=()

if [[ -f "$ALLOWLIST_FILE" ]]; then
    while IFS= read -r line; do
        # Skip comments and blank lines
        [[ -z "$line" || "$line" =~ ^[[:space:]]*# ]] && continue
        ALLOWLIST_PATTERNS+=("$line")
    done < "$ALLOWLIST_FILE"
fi

is_allowlisted() {
    local match_text="$1"
    for pattern in "${ALLOWLIST_PATTERNS[@]+"${ALLOWLIST_PATTERNS[@]}"}"; do
        if [[ "$match_text" == *"$pattern"* ]]; then
            return 0
        fi
    done
    return 1
}

# --- Determine commits to scan -----------------------------------------------

get_commits_to_scan() {
    local remote="$1"
    local url="$2"
    local commits=""

    while read -r local_ref local_sha remote_ref remote_sha; do
        # Skip delete pushes
        if [[ "$local_sha" == "0000000000000000000000000000000000000000" ]]; then
            continue
        fi

        if [[ "$remote_sha" == "0000000000000000000000000000000000000000" ]]; then
            # New branch: scan all commits not on any remote branch
            new_commits=$(git log --pretty=format:"%H" "$local_sha" --not --remotes 2>/dev/null || true)
        else
            # Existing branch: scan only new commits
            new_commits=$(git log --pretty=format:"%H" "${remote_sha}..${local_sha}" 2>/dev/null || true)
        fi

        if [[ -n "$new_commits" ]]; then
            commits="${commits}${commits:+$'\n'}${new_commits}"
        fi
    done

    echo "$commits"
}

# --- Pattern definitions ------------------------------------------------------

# CRITICAL patterns (block push)
# Each entry: "LABEL:::REGEX"
CRITICAL_PATTERNS=(
    # AWS
    "AWS Access Key:::AKIA[0-9A-Z]{16}"
    "AWS Secret Key:::['\"]?(?:aws)?_?(?:secret)?_?(?:access)?_?key['\"]?\s*[:=]\s*['\"][A-Za-z0-9/+=]{40}['\"]"
    "AWS MWS Key:::amzn\\.mws\\.[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"

    # Anthropic
    "Anthropic API Key:::sk-ant-[a-zA-Z0-9_-]{20,}"

    # OpenAI / Generic sk- keys (at least 20 chars after prefix)
    "Secret Key (sk-):::sk-[a-zA-Z0-9]{20,}"

    # GitHub tokens
    "GitHub PAT:::ghp_[A-Za-z0-9]{36,}"
    "GitHub OAuth:::gho_[A-Za-z0-9]{36,}"
    "GitHub App Token:::(?:ghu|ghs|ghr)_[A-Za-z0-9]{36,}"

    # Google
    "Google API Key:::AIza[0-9A-Za-z_-]{35}"
    "Google OAuth Secret:::['\"]?client_secret['\"]?\s*[:=]\s*['\"][A-Za-z0-9_-]{24,}['\"]"

    # Stripe
    "Stripe Secret Key:::(?:sk|rk)_(?:live|test)_[0-9a-zA-Z]{24,}"
    "Stripe Publishable Key:::pk_(?:live|test)_[0-9a-zA-Z]{24,}"

    # Slack
    "Slack Token:::xox[bpsorta]-[0-9a-zA-Z-]{10,}"
    "Slack Webhook:::https://hooks\\.slack\\.com/services/T[a-zA-Z0-9_]{8,}/B[a-zA-Z0-9_]{8,}/[a-zA-Z0-9_]{24,}"

    # Twilio
    "Twilio API Key:::SK[0-9a-fA-F]{32}"

    # SendGrid
    "SendGrid API Key:::SG\\.[a-zA-Z0-9_-]{22,}\\.[a-zA-Z0-9_-]{43,}"

    # Private keys
    "RSA Private Key:::-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"
    "PGP Private Key:::-----BEGIN PGP PRIVATE KEY BLOCK-----"

    # Generic passwords/secrets in assignments
    "Hardcoded Password:::(?i)(?:password|passwd|pwd|pass)\s*[:=]\s*['\"][^'\"]{8,}['\"]"
    "Hardcoded Secret:::(?i)(?:secret|token|api_key|apikey|api-key|auth_token|access_token)\s*[:=]\s*['\"][^'\"\\s]{8,}['\"]"

    # Database connection strings with credentials
    "Database URI with Creds:::(?:mongodb|postgres|postgresql|mysql|redis|amqp)://[^:]+:[^@]+@[^/\\s]+"

    # JWT tokens (the full encoded form)
    "JWT Token:::eyJ[A-Za-z0-9_-]{10,}\\.eyJ[A-Za-z0-9_-]{10,}\\.[A-Za-z0-9_-]{10,}"

    # OAuth client secrets
    "OAuth Client Secret:::(?i)client[_-]?secret\s*[:=]\s*['\"][A-Za-z0-9_-]{10,}['\"]"

    # Telegram Bot Token
    "Telegram Bot Token:::[0-9]{8,}:[A-Za-z0-9_-]{35}"

    # Heroku API Key
    "Heroku API Key:::[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"

    # .env file contents (entire file being committed)
    "Env Var Assignment:::^[A-Z][A-Z0-9_]*=(?:sk-|ghp_|AKIA|AIza|xox[bpsorta]-|SG\\.|sk_live_|pk_live_)"
)

# WARNING patterns (PII - warn but allow push)
PII_PATTERNS=(
    # Email addresses (but not common false positives like example.com)
    "Email Address:::[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\\.[a-zA-Z]{2,}"

    # US Phone numbers (various formats)
    "US Phone Number:::(?:\\+?1[-. ]?)?\\(?\\d{3}\\)?[-. ]?\\d{3}[-. ]?\\d{4}"

    # Social Security Numbers
    "SSN:::\\b[0-9]{3}-[0-9]{2}-[0-9]{4}\\b"

    # Credit card numbers (basic Luhn-eligible patterns)
    "Credit Card (Visa):::(?:\\b4[0-9]{3}[-. ]?[0-9]{4}[-. ]?[0-9]{4}[-. ]?[0-9]{4}\\b)"
    "Credit Card (MC):::(?:\\b5[1-5][0-9]{2}[-. ]?[0-9]{4}[-. ]?[0-9]{4}[-. ]?[0-9]{4}\\b)"
    "Credit Card (Amex):::(?:\\b3[47][0-9]{2}[-. ]?[0-9]{6}[-. ]?[0-9]{5}\\b)"
    "Credit Card (Discover):::(?:\\b6(?:011|5[0-9]{2})[-. ]?[0-9]{4}[-. ]?[0-9]{4}[-. ]?[0-9]{4}\\b)"

    # Physical addresses (basic US pattern: number + street name + street type)
    "Physical Address:::\\b[0-9]{1,5}\\s+[A-Z][a-zA-Z]+\\s+(?:St|Street|Ave|Avenue|Blvd|Boulevard|Dr|Drive|Ln|Lane|Rd|Road|Way|Ct|Court|Pl|Place|Cir|Circle)\\b"

    # IP addresses (non-localhost, non-common private ranges like 10.0.0.x or 192.168.x.x patterns in code)
    "Server IP Address:::(?<![\\.0-9])(?!(?:127\\.0\\.0\\.1|0\\.0\\.0\\.0|10\\.0\\.0\\.|192\\.168\\.0\\.|172\\.(?:1[6-9]|2[0-9]|3[01])\\.0\\.|255\\.))(?:[1-9]|[1-9][0-9]|1[0-9]{2}|2[0-4][0-9]|25[0-5])\\.(?:[0-9]|[1-9][0-9]|1[0-9]{2}|2[0-4][0-9]|25[0-5])\\.(?:[0-9]|[1-9][0-9]|1[0-9]{2}|2[0-4][0-9]|25[0-5])\\.(?:[1-9]|[1-9][0-9]|1[0-9]{2}|2[0-4][0-9]|25[0-5])(?![\\.0-9])"
)

# --- File exclusion -----------------------------------------------------------

# should_skip_file is defined in scripts/security-scan-lib.sh (sourced below).
# Keeping it there ensures the test suite always exercises the real production
# logic rather than a hand-maintained copy.
SCRIPT_DIR_HOOK="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/security-scan-lib.sh
source "$SCRIPT_DIR_HOOK/security-scan-lib.sh"

# Returns 0 if git diff considers this file binary (contains NUL bytes in the
# diff header), 1 if it is a text file.  Binary files are never scanned —
# text replacement on them corrupts the content.
is_binary_file() {
    local commit="$1"
    local filepath="$2"
    # git diff-tree --numstat reports "-  -  <file>" for binary files
    local numstat
    numstat=$(git diff-tree --numstat -r "$commit" -- "$filepath" 2>/dev/null || true)
    if [[ "$numstat" =~ ^-[[:space:]]+-[[:space:]] ]]; then
        return 0  # binary
    fi
    return 1  # text
}

# --- Scanning function --------------------------------------------------------

scan_diff_content() {
    local diff_content="$1"
    local commit_sha="$2"
    local short_sha="${commit_sha:0:8}"
    local current_file=""
    local line_num=0
    local in_hunk=0
    local hunk_start=0
    local skip_current_file=0

    while IFS= read -r line; do
        # Track current file
        if [[ "$line" =~ ^\+\+\+\ b/(.*) ]]; then
            current_file="${BASH_REMATCH[1]}"
            in_hunk=0
            # Pre-compute whether this file should be skipped.
            # This avoids calling should_skip_file and is_binary_file on every line.
            if should_skip_file "$current_file"; then
                skip_current_file=1
            elif is_binary_file "$commit_sha" "$current_file"; then
                skip_current_file=1
            else
                skip_current_file=0
            fi
            continue
        fi

        # Track hunk position for line numbers
        if [[ "$line" =~ ^@@\ -[0-9]+(,[0-9]+)?\ \+([0-9]+)(,[0-9]+)?\ @@ ]]; then
            hunk_start="${BASH_REMATCH[2]}"
            line_num=$((hunk_start - 1))
            in_hunk=1
            continue
        fi

        # Only scan added lines (lines starting with +)
        if [[ $in_hunk -eq 1 ]]; then
            if [[ "$line" =~ ^\+ ]]; then
                ((line_num++)) || true
                local added_content="${line:1}"  # strip leading +

                # Skip empty lines
                [[ -z "${added_content// /}" ]] && continue

                # Skip this file if flagged above
                if [[ $skip_current_file -eq 1 ]]; then
                    continue
                fi

                local match_context="${current_file}:${line_num}: ${added_content}"

                # Check allowlist
                if is_allowlisted "$match_context"; then
                    continue
                fi

                # --- Check CRITICAL patterns ---
                for entry in "${CRITICAL_PATTERNS[@]}"; do
                    local label="${entry%%:::*}"
                    local pattern="${entry#*:::}"

                    if echo "$added_content" | grep -qP -- "$pattern" 2>/dev/null; then
                        # Additional false-positive filters

                        # Skip if line is a comment (unless it contains a high-confidence
                        # token prefix like AKIA, sk-ant-, ghp_, sk_live_, pk_live_)
                        if [[ "$added_content" =~ ^[[:space:]]*(#|//|/\*|\*|<!--) ]]; then
                            if ! echo "$added_content" | grep -qP -- "(?:AKIA|sk-ant-|ghp_|sk_live_|pk_live_)" 2>/dev/null; then
                                continue
                            fi
                        fi

                        # Skip obvious placeholder/fake/test values.
                        # Also skip the scanner's own redaction placeholder.
                        # Patterns: "fake", "dummy", "example", "placeholder",
                        # "your_*_here" style, "CHANGEME", "TODO", "FIXME",
                        # and <REDACTED (the scanner's own output string).
                        if echo "$added_content" | grep -qiP -- "(?:example|placeholder|your[_-]?\w*[_-]?here|your[_-]?(?:key|token|secret)|xxx|dummy|fake|test[_-]?key|CHANGEME|TODO|FIXME|<REDACTED)" 2>/dev/null; then
                            continue
                        fi

                        echo -e "${RED}  CRITICAL [${label}]${RESET}"
                        echo -e "    ${CYAN}Commit:${RESET} ${short_sha}"
                        echo -e "    ${CYAN}File:${RESET}   ${current_file}:${line_num}"
                        echo -e "    ${CYAN}Line:${RESET}   ${added_content:0:120}"
                        echo ""
                        CRITICAL_FOUND=$((CRITICAL_FOUND + 1))
                        break  # One match per line is enough
                    fi
                done

                # --- Check PII patterns ---
                for entry in "${PII_PATTERNS[@]}"; do
                    local label="${entry%%:::*}"
                    local pattern="${entry#*:::}"

                    if echo "$added_content" | grep -qP -- "$pattern" 2>/dev/null; then
                        # PII false-positive filters
                        # Skip common test/example data
                        if echo "$added_content" | grep -qiP -- "(?:example\\.com|example\\.org|test@|noreply@|localhost|user@|foo@|bar@|john@example|jane@example|127\\.0\\.0\\.1|0\\.0\\.0\\.0)" 2>/dev/null; then
                            continue
                        fi
                        # Skip if line is clearly a regex pattern definition
                        if echo "$added_content" | grep -qP -- '(?:regex|pattern|PATTERN|regexp|re\.compile|grep|match|search)\s*[:=(]' 2>/dev/null; then
                            continue
                        fi
                        # Skip the hook script itself to avoid self-flagging
                        if [[ "$current_file" == *"pre-push"* || "$current_file" == *"security-scan"* || "$current_file" == *".security-allowlist"* ]]; then
                            continue
                        fi

                        echo -e "${YELLOW}  WARNING [${label}]${RESET}"
                        echo -e "    ${CYAN}Commit:${RESET} ${short_sha}"
                        echo -e "    ${CYAN}File:${RESET}   ${current_file}:${line_num}"
                        echo -e "    ${CYAN}Line:${RESET}   ${added_content:0:120}"
                        echo ""
                        WARNING_FOUND=$((WARNING_FOUND + 1))
                        break  # One match per line is enough
                    fi
                done

            elif [[ ! "$line" =~ ^- ]]; then
                # Context line (no + or -)
                ((line_num++)) || true
            fi
            # Lines starting with - don't increment line_num (removed lines)
        fi
    done <<< "$diff_content"
}

# --- Check for .env files in commits -----------------------------------------

check_env_files() {
    local commit="$1"
    local short_sha="${commit:0:8}"
    local env_files
    env_files=$(git diff-tree --no-commit-id -r --name-only "$commit" 2>/dev/null | grep -E '\.env($|\.)' || true)

    if [[ -n "$env_files" ]]; then
        while IFS= read -r env_file; do
            if is_allowlisted "$env_file"; then
                continue
            fi
            echo -e "${RED}  CRITICAL [.env File Committed]${RESET}"
            echo -e "    ${CYAN}Commit:${RESET} ${short_sha}"
            echo -e "    ${CYAN}File:${RESET}   ${env_file}"
            echo -e "    ${CYAN}Note:${RESET}   .env files typically contain secrets and should be in .gitignore"
            echo ""
            CRITICAL_FOUND=$((CRITICAL_FOUND + 1))
        done <<< "$env_files"
    fi
}

# =============================================================================
# MAIN
# =============================================================================

echo -e "${BOLD}${CYAN}[security-scan]${RESET} Scanning commits for secrets and PII..."
echo ""

# Read stdin (pre-push hook receives: local_ref local_sha remote_ref remote_sha)
COMMITS=$(get_commits_to_scan "$@")

if [[ -z "$COMMITS" ]]; then
    echo -e "${GREEN}[security-scan]${RESET} No new commits to scan."
    exit 0
fi

COMMIT_COUNT=$(echo "$COMMITS" | wc -l)
echo -e "${CYAN}[security-scan]${RESET} Scanning ${COMMIT_COUNT} commit(s)..."
echo ""

while IFS= read -r commit; do
    [[ -z "$commit" ]] && continue

    # Get the diff for this commit (added lines only matter)
    diff_output=$(git diff-tree -p "$commit" 2>/dev/null || true)

    if [[ -n "$diff_output" ]]; then
        scan_diff_content "$diff_output" "$commit"
    fi

    # Check for .env files
    check_env_files "$commit"

done <<< "$COMMITS"

# --- Summary ------------------------------------------------------------------

echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"

if [[ $CRITICAL_FOUND -eq 0 && $WARNING_FOUND -eq 0 ]]; then
    echo -e "${GREEN}[security-scan] All clear - no secrets or PII detected.${RESET}"
    exit 0
fi

if [[ $WARNING_FOUND -gt 0 ]]; then
    echo -e "${YELLOW}[security-scan] ${WARNING_FOUND} PII warning(s) found.${RESET}"
    echo -e "${YELLOW}  These are informational - push will proceed.${RESET}"
    echo -e "${YELLOW}  Review and consider whether this data should be committed.${RESET}"
fi

if [[ $CRITICAL_FOUND -gt 0 ]]; then
    echo -e "${RED}${BOLD}[security-scan] BLOCKED: ${CRITICAL_FOUND} secret(s)/credential(s) found!${RESET}"
    echo ""
    echo -e "${RED}  Your push has been blocked to prevent leaking secrets.${RESET}"
    echo -e "${RED}  The file has NOT been modified — fix the issue manually.${RESET}"
    echo ""
    echo -e "  ${BOLD}To fix:${RESET}"
    echo -e "    1. Remove the secret from your code"
    echo -e "    2. Use environment variables or a secret manager instead"
    echo -e "    3. Amend/rewrite the commit(s) to remove the secret"
    echo ""
    echo -e "  ${BOLD}If this is a false positive:${RESET}"
    echo -e "    Add the triggering text to ${CYAN}.security-allowlist${RESET} in repo root"
    echo -e "    (one pattern per line; lines matching the pattern are skipped)"
    echo ""
    echo -e "  ${BOLD}Emergency bypass (use with extreme caution):${RESET}"
    echo -e "    ${CYAN}SECURITY_SCAN_SKIP=1 git push${RESET}"
    echo ""
    exit 1
fi

# Only warnings (PII) -- allow push
exit 0
