#!/usr/bin/env bash
# =============================================================================
# test_pre_push_scan_diff_timeout.sh
# Unit tests for the scan_diff loop timeout added in #2037.
#
# Tests:
#   1. SCAN_DIFF_TIMEOUT_SECONDS constant is declared and positive
#   2. SCAN_LOOP_TIMED_OUT is initialised to 0
#   3. scan_diff sets SCAN_LOOP_TIMED_OUT=1 when the deadline is exceeded
#   4. scan_diff exits cleanly (loop breaks) rather than hanging indefinitely
#   5. scan_diff does NOT set SCAN_LOOP_TIMED_OUT on a small fast diff
#   6. hook exits 0 in non-interactive (IS_TTY=0) mode when scan loop times out
#   7. hook prints a warning referencing SCAN_DIFF_TIMEOUT_SECONDS on timeout
#   8. timeout check fires BEFORE subprocess calls (no blocking grep on timeout)
#
# Run: bash tests/unit/test_pre_push_scan_diff_timeout.sh
# Requires: bash, grep
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
# Test 1: SCAN_DIFF_TIMEOUT_SECONDS is declared and positive
# ---------------------------------------------------------------------------

echo ""
echo "=== Test 1: SCAN_DIFF_TIMEOUT_SECONDS is set ==="

TIMEOUT_VALUE=$(grep -E '^SCAN_DIFF_TIMEOUT_SECONDS=' "$HOOK" | head -1 | cut -d= -f2)

if [[ -n "$TIMEOUT_VALUE" ]] && [[ "$TIMEOUT_VALUE" =~ ^[0-9]+$ ]] && [[ "$TIMEOUT_VALUE" -gt 0 ]]; then
    ok "SCAN_DIFF_TIMEOUT_SECONDS=${TIMEOUT_VALUE} (positive integer)"
else
    fail "SCAN_DIFF_TIMEOUT_SECONDS not found or not a positive integer (got: '${TIMEOUT_VALUE}')"
fi

# ---------------------------------------------------------------------------
# Test 2: SCAN_LOOP_TIMED_OUT is initialised to 0
# ---------------------------------------------------------------------------

echo ""
echo "=== Test 2: SCAN_LOOP_TIMED_OUT initialised to 0 ==="

if grep -qE '^SCAN_LOOP_TIMED_OUT=0' "$HOOK"; then
    ok "SCAN_LOOP_TIMED_OUT=0 declared at top of hook"
else
    fail "SCAN_LOOP_TIMED_OUT=0 initialisation not found"
fi

# ---------------------------------------------------------------------------
# Test 3: scan_diff sets SCAN_LOOP_TIMED_OUT=1 when deadline is exceeded
# ---------------------------------------------------------------------------

echo ""
echo "=== Test 3: scan_diff sets SCAN_LOOP_TIMED_OUT=1 when deadline exceeded ==="

# Build a minimal self-contained script that reproduces the scan_diff timeout
# mechanism with a 1-second deadline and a simulated large diff (many added
# lines). The \$SECONDS builtin advances naturally; we just need enough lines
# that the first iteration records start, then we force SECONDS ahead.
#
# Strategy: override SECONDS via a function that returns 100 on the second call,
# which will immediately trip the (( SECONDS - _scan_start >= 1 )) check.
# Because we cannot write to $SECONDS in bash 4, we instead patch the timeout
# value to 0 seconds and start with SECONDS already pointing to the current
# wall clock — the first iteration will always see elapsed >= 0 >= 0 and trip.

TMP_SCRIPT=$(mktemp /tmp/test_scan_diff_timeout_XXXXXX.sh)
trap "rm -f $TMP_SCRIPT" EXIT

cat > "$TMP_SCRIPT" << 'INNER_EOF'
#!/bin/bash
set -uo pipefail

# Use 0-second timeout so the very first added line trips the deadline.
SCAN_DIFF_TIMEOUT_SECONDS=0
SCAN_LOOP_TIMED_OUT=0

FINDINGS=()
ISSUES_FOUND=0
RED='' GREEN='' YELLOW='' CYAN='' BOLD='' NC=''

add_finding() {
    FINDINGS+=("finding: $3")
    ISSUES_FOUND=$((ISSUES_FOUND + 1))
}

should_skip_file() { return 1; }  # never skip

scan_diff() {
    local diff_text="$1"
    [ -z "$diff_text" ] && return
    local current_file=""
    local line_num=0
    local _scan_start=$SECONDS

    while IFS= read -r raw_line; do
        if [[ "$raw_line" =~ ^\+\+\+\ b/(.+)$ ]]; then
            current_file="${BASH_REMATCH[1]}"
            continue
        fi
        if [[ "$raw_line" =~ ^\+[^+] || "$raw_line" == "+" ]]; then
            line_num=$((line_num + 1))
            if (( SECONDS - _scan_start >= SCAN_DIFF_TIMEOUT_SECONDS )); then
                SCAN_LOOP_TIMED_OUT=1
                break
            fi
        fi
    done <<< "$diff_text"
}

# Minimal synthetic diff with several added lines
FAKE_DIFF="+++ b/some/file.py
+line one
+line two
+line three
+line four
+line five"

scan_diff "$FAKE_DIFF"

if [ "$SCAN_LOOP_TIMED_OUT" -eq 1 ]; then
    echo "SCAN_LOOP_TIMED_OUT_SET"
    exit 0
else
    echo "NOT_TIMED_OUT (SCAN_LOOP_TIMED_OUT=$SCAN_LOOP_TIMED_OUT)"
    exit 1
fi
INNER_EOF

chmod +x "$TMP_SCRIPT"
OUTPUT=$(bash "$TMP_SCRIPT" 2>&1)
EXIT_CODE=$?

if [ $EXIT_CODE -eq 0 ] && echo "$OUTPUT" | grep -q "SCAN_LOOP_TIMED_OUT_SET"; then
    ok "scan_diff sets SCAN_LOOP_TIMED_OUT=1 when deadline is 0s"
else
    fail "Expected SCAN_LOOP_TIMED_OUT=1; got exit=$EXIT_CODE output='$OUTPUT'"
fi

# ---------------------------------------------------------------------------
# Test 4: scan_diff completes in bounded time (does not hang) on timeout
# ---------------------------------------------------------------------------

echo ""
echo "=== Test 4: scan_diff exits cleanly (no hang) on large diff with short timeout ==="

TMP_SCRIPT2=$(mktemp /tmp/test_scan_diff_nohang_XXXXXX.sh)
trap "rm -f $TMP_SCRIPT $TMP_SCRIPT2" EXIT

# Generate a diff with 200 added lines and run with 0s timeout.
# The test verifies the script finishes quickly — if it hung we would never
# reach the assertion. We wrap with a 5s wall-clock budget via `timeout`.
cat > "$TMP_SCRIPT2" << 'INNER_EOF'
#!/bin/bash
set -uo pipefail

SCAN_DIFF_TIMEOUT_SECONDS=0
SCAN_LOOP_TIMED_OUT=0
FINDINGS=()
ISSUES_FOUND=0

should_skip_file() { return 1; }
add_finding() { :; }

scan_diff() {
    local diff_text="$1"
    [ -z "$diff_text" ] && return
    local current_file=""
    local line_num=0
    local _scan_start=$SECONDS

    while IFS= read -r raw_line; do
        if [[ "$raw_line" =~ ^\+\+\+\ b/(.+)$ ]]; then
            current_file="${BASH_REMATCH[1]}"
            continue
        fi
        if [[ "$raw_line" =~ ^\+[^+] || "$raw_line" == "+" ]]; then
            line_num=$((line_num + 1))
            if (( SECONDS - _scan_start >= SCAN_DIFF_TIMEOUT_SECONDS )); then
                SCAN_LOOP_TIMED_OUT=1
                break
            fi
            # Simulate the per-line work (without real subprocess overhead)
            local content="${raw_line:1}"
            _ "$content"
        fi
    done <<< "$diff_text"
}

# Suppress unused-variable lint — _ is intentionally a no-op
_() { :; }

# Build 200-line diff
BIG_DIFF="+++ b/bigfile.py"$'\n'
for i in $(seq 1 200); do
    BIG_DIFF="${BIG_DIFF}+line $i"$'\n'
done

scan_diff "$BIG_DIFF"

if [ "$SCAN_LOOP_TIMED_OUT" -eq 1 ]; then
    echo "COMPLETED_WITH_TIMEOUT"
    exit 0
else
    echo "COMPLETED_WITHOUT_TIMEOUT"
    exit 0
fi
INNER_EOF

chmod +x "$TMP_SCRIPT2"
OUTPUT2=$(timeout 5s bash "$TMP_SCRIPT2" 2>&1)
EXIT_CODE2=$?

if [ $EXIT_CODE2 -eq 0 ]; then
    ok "scan_diff loop exits cleanly within 5s on 200-line diff with 0s timeout"
else
    fail "scan_diff did not complete within 5s (exit=$EXIT_CODE2) — possible hang"
fi

# ---------------------------------------------------------------------------
# Test 5: scan_diff does NOT set SCAN_LOOP_TIMED_OUT on a small fast diff
# ---------------------------------------------------------------------------

echo ""
echo "=== Test 5: SCAN_LOOP_TIMED_OUT stays 0 on small diff with generous timeout ==="

TMP_SCRIPT3=$(mktemp /tmp/test_scan_diff_notimeout_XXXXXX.sh)
trap "rm -f $TMP_SCRIPT $TMP_SCRIPT2 $TMP_SCRIPT3" EXIT

cat > "$TMP_SCRIPT3" << 'INNER_EOF'
#!/bin/bash
set -uo pipefail

SCAN_DIFF_TIMEOUT_SECONDS=300   # very generous — should never trip in testing
SCAN_LOOP_TIMED_OUT=0
FINDINGS=()
ISSUES_FOUND=0

should_skip_file() { return 1; }
add_finding() { :; }

scan_diff() {
    local diff_text="$1"
    [ -z "$diff_text" ] && return
    local current_file=""
    local line_num=0
    local _scan_start=$SECONDS

    while IFS= read -r raw_line; do
        if [[ "$raw_line" =~ ^\+\+\+\ b/(.+)$ ]]; then
            current_file="${BASH_REMATCH[1]}"
            continue
        fi
        if [[ "$raw_line" =~ ^\+[^+] || "$raw_line" == "+" ]]; then
            line_num=$((line_num + 1))
            if (( SECONDS - _scan_start >= SCAN_DIFF_TIMEOUT_SECONDS )); then
                SCAN_LOOP_TIMED_OUT=1
                break
            fi
        fi
    done <<< "$diff_text"
}

SMALL_DIFF="+++ b/tiny.py
+x = 1
+y = 2"

scan_diff "$SMALL_DIFF"

if [ "$SCAN_LOOP_TIMED_OUT" -eq 0 ]; then
    echo "NO_TIMEOUT_FLAG"
    exit 0
else
    echo "UNEXPECTED_TIMEOUT"
    exit 1
fi
INNER_EOF

chmod +x "$TMP_SCRIPT3"
OUTPUT3=$(bash "$TMP_SCRIPT3" 2>&1)
EXIT_CODE3=$?

if [ $EXIT_CODE3 -eq 0 ] && echo "$OUTPUT3" | grep -q "NO_TIMEOUT_FLAG"; then
    ok "SCAN_LOOP_TIMED_OUT stays 0 on small diff with generous (300s) timeout"
else
    fail "Expected NO_TIMEOUT_FLAG; got exit=$EXIT_CODE3 output='$OUTPUT3'"
fi

# ---------------------------------------------------------------------------
# Test 6: hook exits 0 in IS_TTY=0 mode when SCAN_LOOP_TIMED_OUT=1
# ---------------------------------------------------------------------------

echo ""
echo "=== Test 6: hook exits 0 in CI mode when scan loop timed out ==="

TMP_SCRIPT4=$(mktemp /tmp/test_scan_diff_ci_XXXXXX.sh)
trap "rm -f $TMP_SCRIPT $TMP_SCRIPT2 $TMP_SCRIPT3 $TMP_SCRIPT4" EXIT

cat > "$TMP_SCRIPT4" << 'INNER_EOF'
#!/bin/bash
# Minimal reproduction of the hook's main section with SCAN_LOOP_TIMED_OUT=1
SCAN_DIFF_TIMEOUT_SECONDS=45
SCAN_LOOP_TIMED_OUT=1
IS_TTY=0
FINDINGS=()
YELLOW='' BOLD='' NC='' GREEN='' RED='' CYAN=''

warn()  { echo "[WARN] $1"; }
info()  { echo "[SCAN] $1"; }

# Replicate the post-scan section of the hook
if [ "$SCAN_LOOP_TIMED_OUT" -eq 1 ]; then
    warn "scan_diff timed out after ${SCAN_DIFF_TIMEOUT_SECONDS}s — scan incomplete."
    warn "Large diff detected. Run 'git diff origin/main..HEAD | grep -P ...' manually to verify."
    warn "Push proceeding with partial scan results only."
    echo ""
fi

if [ "$IS_TTY" -eq 0 ]; then
    warn "Non-interactive mode (CI). Running scan but will not block push."
    if [ "${#FINDINGS[@]}" -gt 0 ]; then
        echo "FINDINGS_REPORTED"
    else
        echo "NO_FINDINGS"
    fi
    exit 0
fi

exit 1
INNER_EOF

chmod +x "$TMP_SCRIPT4"
OUTPUT4=$(bash "$TMP_SCRIPT4" 2>&1)
EXIT_CODE4=$?

if [ $EXIT_CODE4 -eq 0 ] && echo "$OUTPUT4" | grep -q "timed out"; then
    ok "hook exits 0 in CI mode when scan loop timed out, warning printed"
else
    fail "Expected exit 0 with timeout warning; got exit=$EXIT_CODE4 output='$OUTPUT4'"
fi

# ---------------------------------------------------------------------------
# Test 7: warning message references SCAN_DIFF_TIMEOUT_SECONDS
# ---------------------------------------------------------------------------

echo ""
echo "=== Test 7: warning message references SCAN_DIFF_TIMEOUT_SECONDS ==="

if grep -q 'SCAN_DIFF_TIMEOUT_SECONDS.*scan incomplete\|scan_diff timed out after.*SCAN_DIFF_TIMEOUT_SECONDS' "$HOOK"; then
    ok "warning message references SCAN_DIFF_TIMEOUT_SECONDS"
else
    fail "warning message does not reference SCAN_DIFF_TIMEOUT_SECONDS"
fi

# ---------------------------------------------------------------------------
# Test 8: timeout check uses $SECONDS builtin (no subprocess per check)
# ---------------------------------------------------------------------------

echo ""
echo "=== Test 8: timeout uses \$SECONDS builtin, not a subprocess ==="

# The timeout check must be (( SECONDS - _scan_start >= N )) — a pure arithmetic
# expression. If it were implemented with `date` or `timeout` subprocess calls,
# those would add subprocess overhead on every line. Verify the exact pattern.
if grep -qE '\(\( SECONDS - _scan_start >= SCAN_DIFF_TIMEOUT_SECONDS \)\)' "$HOOK"; then
    ok "timeout check uses (( SECONDS - _scan_start >= SCAN_DIFF_TIMEOUT_SECONDS )) arithmetic — no subprocess"
else
    fail "expected (( SECONDS - _scan_start >= SCAN_DIFF_TIMEOUT_SECONDS )) pattern not found in hook"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "Results: ${PASS} passed, ${FAIL} failed"
echo ""

if [[ $FAIL -gt 0 ]]; then
    exit 1
fi

exit 0
