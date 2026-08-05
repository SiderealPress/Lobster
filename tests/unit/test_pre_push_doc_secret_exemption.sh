#!/usr/bin/env bash
# =============================================================================
# test_pre_push_doc_secret_exemption.sh
# Regression tests for issue #2160: .githooks/pre-push's should_skip_file()
# used to exempt ALL doc files (*.md, *.mdx, *.rst, *.txt, *.adoc) from EVERY
# check, including high-confidence secret/token patterns. That let a real
# Telegram bot token committed to docs/DOCKER-TESTING.md go undetected for
# months (2026-03-14 to 2026-07-17).
#
# The fix splits the exemption:
#   - Doc files REMAIN exempt from PII_PATTERNS / NAME_PATTERNS / INSTANCE_PATTERNS
#     (example names/emails/addresses are legitimate documentation content).
#   - Doc files are NO LONGER exempt from SECURITY_PATTERNS (API keys, private
#     keys, Telegram bot tokens, etc — high-confidence secret shapes).
#
# This test sources the REAL function/pattern definitions out of the live
# .githooks/pre-push file (everything before the "# Main" execution section)
# so it always exercises production logic rather than a reimplementation.
#
# Falsifiability check (see PR body for command transcript):
#   1. Run this test against the fixed hook -> all tests PASS.
#   2. `git stash` the .githooks/pre-push change (restore old should_skip_file
#      that exempts docs from everything) -> rerun -> the "real token in .md
#      file is flagged" test FAILS.
#   3. `git stash pop` to restore the fix -> rerun -> PASS again.
#
# Run: bash tests/unit/test_pre_push_doc_secret_exemption.sh
# Requires: bash (4.4+, for associative/indexed array features already used
#           by the hook itself), grep -P (PCRE support)
# =============================================================================

set -uo pipefail

PASS=0
FAIL=0
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
HOOK="$REPO_ROOT/.githooks/pre-push"

ok()   { echo "  PASS: $1"; ((PASS++)) || true; }
fail() { echo "  FAIL: $1"; ((FAIL++)) || true; }

# ---------------------------------------------------------------------------
# Extract only the function/pattern-array definitions from the real hook
# (everything before the "# Main" execution section marker) and source them
# into THIS process. We deliberately avoid sourcing the whole file because
# the "Main" section calls `exit` and reads from /dev/tty, which would kill
# or hang this test process.
# ---------------------------------------------------------------------------

FUNCS_FILE="$(mktemp /tmp/pre_push_funcs_XXXXXX.sh)"
trap 'rm -f "$FUNCS_FILE"' EXIT

awk '/^# Main$/{exit} {print}' "$HOOK" > "$FUNCS_FILE"

if ! grep -q "scan_diff()" "$FUNCS_FILE"; then
    echo "FATAL: could not extract scan_diff() from $HOOK — '# Main' marker missing or moved?"
    exit 2
fi

# shellcheck source=/dev/null
source "$FUNCS_FILE"

# ---------------------------------------------------------------------------
# Test helper: run scan_diff on a synthetic unified diff and report whether
# ANY finding mentions a given substring (e.g. "Telegram bot token").
# ---------------------------------------------------------------------------

run_scan() {
    local diff_text="$1"
    FINDINGS=()
    ISSUES_FOUND=0
    scan_diff "$diff_text"
}

findings_mention() {
    local needle="$1"
    local f
    for f in "${FINDINGS[@]}"; do
        if [[ "$f" == *"$needle"* ]]; then
            return 0
        fi
    done
    return 1
}

# A real-token-shaped string: 9 digits + ':' + exactly 35 chars of
# [A-Za-z0-9_-], matching the telegram_token pattern
# `\b\d{8,10}:[A-Za-z0-9_-]{35}\b`. (Deliberately fake — not a real credential.)
REAL_TELEGRAM_TOKEN="123456789:AAHrM5Nthistotallyisnotarealtoken12"

# ---------------------------------------------------------------------------
# Test 1: real Telegram-token-shaped string in a .md file IS flagged
# ---------------------------------------------------------------------------

echo ""
echo "=== Test 1: real-token-shaped string in .md file is flagged ==="

DIFF_TOKEN_IN_MD=$(cat <<EOF
diff --git a/docs/DOCKER-TESTING.md b/docs/DOCKER-TESTING.md
index 0000000..1111111 100644
--- a/docs/DOCKER-TESTING.md
+++ b/docs/DOCKER-TESTING.md
@@ -1,2 +1,3 @@
 # Docker Testing
+Bot token: ${REAL_TELEGRAM_TOKEN}
EOF
)

run_scan "$DIFF_TOKEN_IN_MD"

if [ "$ISSUES_FOUND" -gt 0 ] && findings_mention "Telegram bot token"; then
    ok "real Telegram-token-shaped string in docs/DOCKER-TESTING.md is flagged"
else
    fail "expected a Telegram bot token finding for docs/DOCKER-TESTING.md; got ISSUES_FOUND=$ISSUES_FOUND FINDINGS=${FINDINGS[*]:-<empty>}"
fi

# ---------------------------------------------------------------------------
# Test 2: an obvious placeholder token in a .md file stays clean
# ---------------------------------------------------------------------------

echo ""
echo "=== Test 2: placeholder token in .md file is NOT flagged ==="

DIFF_PLACEHOLDER_IN_MD=$(cat <<'EOF'
diff --git a/docs/DOCKER-TESTING.md b/docs/DOCKER-TESTING.md
index 0000000..2222222 100644
--- a/docs/DOCKER-TESTING.md
+++ b/docs/DOCKER-TESTING.md
@@ -1,2 +1,3 @@
 # Docker Testing
+Bot token: <YOUR_BOT_TOKEN>
EOF
)

run_scan "$DIFF_PLACEHOLDER_IN_MD"

if [ "$ISSUES_FOUND" -eq 0 ]; then
    ok "placeholder token <YOUR_BOT_TOKEN> in .md file is not flagged"
else
    fail "expected no findings for placeholder token; got FINDINGS=${FINDINGS[*]}"
fi

echo ""
echo "=== Test 2b: sk-xxx-placeholder-do-not-use in .md file is NOT flagged ==="

DIFF_SK_PLACEHOLDER=$(cat <<'EOF'
diff --git a/docs/DOCKER-TESTING.md b/docs/DOCKER-TESTING.md
index 0000000..3333333 100644
--- a/docs/DOCKER-TESTING.md
+++ b/docs/DOCKER-TESTING.md
@@ -1,2 +1,3 @@
 # Docker Testing
+API key: sk-xxx-placeholder-do-not-use
EOF
)

run_scan "$DIFF_SK_PLACEHOLDER"

if [ "$ISSUES_FOUND" -eq 0 ]; then
    ok "sk-xxx-placeholder-do-not-use in .md file is not flagged"
else
    fail "expected no findings for sk-xxx placeholder; got FINDINGS=${FINDINGS[*]}"
fi

# ---------------------------------------------------------------------------
# Test 3 (regression): PII (email) in a .md file REMAINS exempt
# ---------------------------------------------------------------------------

echo ""
echo "=== Test 3: PII (email) in .md file remains exempt (regression) ==="

DIFF_EMAIL_IN_MD=$(cat <<'EOF'
diff --git a/docs/CONTRIBUTING.md b/docs/CONTRIBUTING.md
index 0000000..4444444 100644
--- a/docs/CONTRIBUTING.md
+++ b/docs/CONTRIBUTING.md
@@ -1,2 +1,3 @@
 # Contributing
+Contact: someone@not-an-allowlisted-domain.com
EOF
)

run_scan "$DIFF_EMAIL_IN_MD"

if [ "$ISSUES_FOUND" -eq 0 ]; then
    ok "email address in docs/CONTRIBUTING.md remains exempt from PII checks"
else
    fail "expected docs to remain PII-exempt; got FINDINGS=${FINDINGS[*]}"
fi

# ---------------------------------------------------------------------------
# Test 4 (regression): personal name in a .md file REMAINS exempt
# ---------------------------------------------------------------------------

echo ""
echo "=== Test 4: personal name in .md file remains exempt (regression) ==="

DIFF_NAME_IN_MD=$(cat <<'EOF'
diff --git a/docs/CONTRIBUTING.md b/docs/CONTRIBUTING.md
index 0000000..5555555 100644
--- a/docs/CONTRIBUTING.md
+++ b/docs/CONTRIBUTING.md
@@ -1,2 +1,3 @@
 # Contributing
+Thanks to alice for the original patch.
EOF
)

run_scan "$DIFF_NAME_IN_MD"

if [ "$ISSUES_FOUND" -eq 0 ]; then
    ok "personal name in docs/CONTRIBUTING.md remains exempt from name checks"
else
    fail "expected docs to remain name-exempt; got FINDINGS=${FINDINGS[*]}"
fi

# ---------------------------------------------------------------------------
# Test 5 (control): real token in a .py (non-doc) file is still flagged
# ---------------------------------------------------------------------------

echo ""
echo "=== Test 5: real-token-shaped string in .py file is still flagged (control) ==="

DIFF_TOKEN_IN_PY=$(cat <<EOF
diff --git a/src/bot/config.py b/src/bot/config.py
index 0000000..6666666 100644
--- a/src/bot/config.py
+++ b/src/bot/config.py
@@ -1,2 +1,3 @@
 # config
+BOT_TOKEN = "${REAL_TELEGRAM_TOKEN}"
EOF
)

run_scan "$DIFF_TOKEN_IN_PY"

if [ "$ISSUES_FOUND" -gt 0 ] && findings_mention "Telegram bot token"; then
    ok "real-token-shaped string in src/bot/config.py is flagged (unaffected control case)"
else
    fail "expected a Telegram bot token finding for a .py file; got FINDINGS=${FINDINGS[*]:-<empty>}"
fi

# ---------------------------------------------------------------------------
# Test 6 (regression): hook files themselves remain fully exempt, even from
# secret patterns — they legitimately contain the regexes as strings.
# ---------------------------------------------------------------------------

echo ""
echo "=== Test 6: .githooks/* files remain fully exempt, including from secret checks ==="

DIFF_TOKEN_IN_HOOK=$(cat <<EOF
diff --git a/.githooks/pre-push b/.githooks/pre-push
index 0000000..7777777 100644
--- a/.githooks/pre-push
+++ b/.githooks/pre-push
@@ -1,2 +1,3 @@
 # hook
+# example: ${REAL_TELEGRAM_TOKEN}
EOF
)

run_scan "$DIFF_TOKEN_IN_HOOK"

if [ "$ISSUES_FOUND" -eq 0 ]; then
    ok ".githooks/* files remain exempt from all checks, including secrets"
else
    fail "expected .githooks/* to remain fully exempt; got FINDINGS=${FINDINGS[*]}"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

echo ""
echo "============================================================"
echo "Results: $PASS passed, $FAIL failed"
echo "============================================================"

if [ "$FAIL" -gt 0 ]; then
    exit 1
fi
exit 0
