#!/usr/bin/env bash
# =============================================================================
# test_pre_commit_email_check.sh
# Behavioral tests for the commit-author-email blocklist check added to
# .githooks/pre-commit (issue #2162).
#
# These tests exercise the REAL hook file against REAL throwaway git repos —
# not a synthetic re-implementation — by invoking `git commit` end-to-end with
# core.hooksPath pointed at the repo's .githooks directory.
#
# Covers:
#   1. No blocklist file present               -> no-op (commit succeeds, no warning)
#   2. Blocklist present, user.email matches    -> warns; non-interactive commit
#                                                   still succeeds (CI policy)
#   3. Blocklist present, user.email matches    -> interactive prompt via a real
#                                                   pty (expect) blocks on "n",
#                                                   proceeds on "y"
#   4. Blocklist present, --author matches      -> warns (GIT_AUTHOR_EMAIL path)
#   5. Blocklist present, user.email does NOT
#      match any entry                          -> silent pass, no warning
#   6. Comments / blank lines in blocklist file are ignored
#
# Run: bash tests/unit/test_pre_commit_email_check.sh
# Requires: bash, git, expect (for the interactive-prompt test)
# =============================================================================

set -uo pipefail

PASS=0
FAIL=0
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
HOOK_SRC="$REPO_ROOT/.githooks/pre-commit"

# Placeholder/example addresses only — never the real leaked addresses from
# this repo's history. See issue #2162: do not hardcode real leaked emails
# anywhere that gets committed to the repo.
TEST_BLOCKED_EMAIL="leaked-example@example.com"
TEST_BLOCKED_EMAIL_2="other-leaked@example.org"
TEST_CLEAN_EMAIL="clean-dev@example.com"

WARNING_MARKER="leaked-email blocklist"

ok()   { echo "  PASS: $1"; ((PASS++)) || true; }
fail() { echo "  FAIL: $1"; ((FAIL++)) || true; }

TMP_DIRS=()
cleanup() {
    for d in "${TMP_DIRS[@]:-}"; do
        [ -n "$d" ] && rm -rf "$d"
    done
}
trap cleanup EXIT

# ---------------------------------------------------------------------------
# Helper: build a throwaway repo wired to the real .githooks/pre-commit hook,
# with an isolated LOBSTER_USER_CONFIG_DIR (so we never touch the real
# ~/lobster-user-config on the machine running these tests).
# ---------------------------------------------------------------------------
make_repo() {
    local repo_dir
    repo_dir="$(mktemp -d /tmp/test_pre_commit_email_repo_XXXXXX)"
    TMP_DIRS+=("$repo_dir")

    git init -q "$repo_dir"
    mkdir -p "$repo_dir/.githooks"
    cp "$HOOK_SRC" "$repo_dir/.githooks/pre-commit"
    chmod +x "$repo_dir/.githooks/pre-commit"

    (
        cd "$repo_dir" || exit 1
        git config core.hooksPath .githooks
        git config user.name "Test User"
        git config user.email "$TEST_CLEAN_EMAIL"
    )

    echo "$repo_dir"
}

# Isolated config dir per test — never the real user config dir.
make_config_dir() {
    local cfg_dir
    cfg_dir="$(mktemp -d /tmp/test_pre_commit_email_cfg_XXXXXX)"
    TMP_DIRS+=("$cfg_dir")
    echo "$cfg_dir"
}

# ---------------------------------------------------------------------------
# Test 1: no blocklist file present -> no-op
# ---------------------------------------------------------------------------
echo ""
echo "=== Test 1: no blocklist file -> silent no-op ==="

REPO1="$(make_repo)"
CFG1="$(make_config_dir)"   # empty dir, no blocked-commit-emails.txt inside it

(
    cd "$REPO1" || exit 1
    echo "hello" > file.txt
    git add file.txt
    LOBSTER_USER_CONFIG_DIR="$CFG1" git commit -m "test commit" < /dev/null > /tmp/test1_out.txt 2>&1
)
EXIT1=$?
OUT1="$(cat /tmp/test1_out.txt)"
rm -f /tmp/test1_out.txt

if [ "$EXIT1" -eq 0 ] && ! echo "$OUT1" | grep -qi "blocklist"; then
    ok "no blocklist file -> commit succeeds with no blocklist-related output"
else
    fail "expected clean commit with no blocklist mention; exit=$EXIT1 output='$OUT1'"
fi

# ---------------------------------------------------------------------------
# Test 2: blocklist present, user.email matches, non-interactive -> warns,
# commit still succeeds (CI/non-interactive policy: never hang, never block)
# ---------------------------------------------------------------------------
echo ""
echo "=== Test 2: matching email, non-interactive -> warns but does not block ==="

REPO2="$(make_repo)"
CFG2="$(make_config_dir)"
printf '%s\n' "$TEST_BLOCKED_EMAIL" > "$CFG2/blocked-commit-emails.txt"

(
    cd "$REPO2" || exit 1
    git config user.email "$TEST_BLOCKED_EMAIL"
    echo "hello" > file.txt
    git add file.txt
    LOBSTER_USER_CONFIG_DIR="$CFG2" git commit -m "test commit" < /dev/null > /tmp/test2_out.txt 2>&1
)
EXIT2=$?
OUT2="$(cat /tmp/test2_out.txt)"
rm -f /tmp/test2_out.txt

if [ "$EXIT2" -eq 0 ] && echo "$OUT2" | grep -qi "$WARNING_MARKER" && echo "$OUT2" | grep -q "$TEST_BLOCKED_EMAIL"; then
    ok "matching email (non-interactive) -> warning printed, commit not blocked"
else
    fail "expected warning + successful commit; exit=$EXIT2 output='$OUT2'"
fi

# ---------------------------------------------------------------------------
# Test 3: blocklist present, user.email matches, INTERACTIVE (real pty via
# expect) -> confirmation prompt actually gates the commit
# ---------------------------------------------------------------------------
echo ""
echo "=== Test 3: matching email, interactive pty -> confirmation prompt gates commit ==="

if ! command -v expect >/dev/null 2>&1; then
    echo "  SKIP: 'expect' not installed — cannot drive a real pty for this test"
else
    REPO3A="$(make_repo)"
    CFG3="$(make_config_dir)"
    printf '%s\n' "$TEST_BLOCKED_EMAIL" > "$CFG3/blocked-commit-emails.txt"
    (
        cd "$REPO3A" || exit 1
        git config user.email "$TEST_BLOCKED_EMAIL"
        echo "hello" > file.txt
        git add file.txt
    )

    # 3a: answer "n" (or default Enter) -> commit is ABORTED
    EXPECT_SCRIPT_N=$(mktemp /tmp/test_pre_commit_expect_n_XXXXXX.exp)
    TMP_DIRS+=("$EXPECT_SCRIPT_N")
    cat > "$EXPECT_SCRIPT_N" << EOF
set timeout 10
spawn env LOBSTER_USER_CONFIG_DIR=$CFG3 git -C $REPO3A commit -m "test commit"
expect "Continue committing with this email?"
send "n\r"
expect eof
catch wait result
exit [lindex \$result 3]
EOF
    OUT3A="$(expect "$EXPECT_SCRIPT_N" 2>&1)"
    EXIT3A=$?

    if [ "$EXIT3A" -ne 0 ] && echo "$OUT3A" | grep -qi "Commit aborted"; then
        ok "interactive 'n' response aborts the commit"
    else
        fail "expected 'n' to abort commit; exit=$EXIT3A output='$OUT3A'"
    fi

    if (cd "$REPO3A" && git log --oneline 2>/dev/null | grep -q "test commit"); then
        fail "commit should NOT exist in history after 'n' response"
    else
        ok "no commit was created in history after 'n' response"
    fi

    # 3b: answer "y" -> commit PROCEEDS
    REPO3B="$(make_repo)"
    (
        cd "$REPO3B" || exit 1
        git config user.email "$TEST_BLOCKED_EMAIL"
        echo "hello" > file.txt
        git add file.txt
    )

    EXPECT_SCRIPT_Y=$(mktemp /tmp/test_pre_commit_expect_y_XXXXXX.exp)
    TMP_DIRS+=("$EXPECT_SCRIPT_Y")
    cat > "$EXPECT_SCRIPT_Y" << EOF
set timeout 10
spawn env LOBSTER_USER_CONFIG_DIR=$CFG3 git -C $REPO3B commit -m "test commit"
expect "Continue committing with this email?"
send "y\r"
expect eof
catch wait result
exit [lindex \$result 3]
EOF
    OUT3B="$(expect "$EXPECT_SCRIPT_Y" 2>&1)"
    EXIT3B=$?

    if [ "$EXIT3B" -eq 0 ] && echo "$OUT3B" | grep -qi "Confirmed. Proceeding"; then
        ok "interactive 'y' response proceeds with the commit"
    else
        fail "expected 'y' to proceed with commit; exit=$EXIT3B output='$OUT3B'"
    fi

    if (cd "$REPO3B" && git log --oneline 2>/dev/null | grep -q "test commit"); then
        ok "commit exists in history after 'y' response"
    else
        fail "commit should exist in history after 'y' response"
    fi
fi

# ---------------------------------------------------------------------------
# Test 4: blocklist present, matched via `git commit --author=...`
# (GIT_AUTHOR_EMAIL path, not git config user.email)
# ---------------------------------------------------------------------------
echo ""
echo "=== Test 4: --author override matches blocklist -> warns ==="

REPO4="$(make_repo)"
CFG4="$(make_config_dir)"
printf '%s\n' "$TEST_BLOCKED_EMAIL_2" > "$CFG4/blocked-commit-emails.txt"

(
    cd "$REPO4" || exit 1
    # git config user.email stays clean; only --author is the leaked address
    echo "hello" > file.txt
    git add file.txt
    LOBSTER_USER_CONFIG_DIR="$CFG4" git commit --author="Someone <$TEST_BLOCKED_EMAIL_2>" -m "test commit" < /dev/null > /tmp/test4_out.txt 2>&1
)
EXIT4=$?
OUT4="$(cat /tmp/test4_out.txt)"
rm -f /tmp/test4_out.txt

if [ "$EXIT4" -eq 0 ] && echo "$OUT4" | grep -qi "$WARNING_MARKER" && echo "$OUT4" | grep -q "$TEST_BLOCKED_EMAIL_2"; then
    ok "--author=... override matching blocklist triggers warning (GIT_AUTHOR_EMAIL path)"
else
    fail "expected warning via --author path; exit=$EXIT4 output='$OUT4'"
fi

# ---------------------------------------------------------------------------
# Test 5: blocklist present, user.email does NOT match any entry -> silent
# ---------------------------------------------------------------------------
echo ""
echo "=== Test 5: non-matching email -> silent pass ==="

REPO5="$(make_repo)"
CFG5="$(make_config_dir)"
printf '%s\n%s\n' "$TEST_BLOCKED_EMAIL" "$TEST_BLOCKED_EMAIL_2" > "$CFG5/blocked-commit-emails.txt"

(
    cd "$REPO5" || exit 1
    git config user.email "$TEST_CLEAN_EMAIL"
    echo "hello" > file.txt
    git add file.txt
    LOBSTER_USER_CONFIG_DIR="$CFG5" git commit -m "test commit" < /dev/null > /tmp/test5_out.txt 2>&1
)
EXIT5=$?
OUT5="$(cat /tmp/test5_out.txt)"
rm -f /tmp/test5_out.txt

if [ "$EXIT5" -eq 0 ] && ! echo "$OUT5" | grep -qi "blocklist"; then
    ok "non-matching email -> commit succeeds with no blocklist warning"
else
    fail "expected silent successful commit; exit=$EXIT5 output='$OUT5'"
fi

# ---------------------------------------------------------------------------
# Test 6: comments and blank lines in the blocklist file are ignored
# ---------------------------------------------------------------------------
echo ""
echo "=== Test 6: comment lines and blank lines in blocklist are ignored ==="

REPO6="$(make_repo)"
CFG6="$(make_config_dir)"
cat > "$CFG6/blocked-commit-emails.txt" << CFGEOF
# This is a comment, not an email

$TEST_BLOCKED_EMAIL
CFGEOF

(
    cd "$REPO6" || exit 1
    git config user.email "$TEST_BLOCKED_EMAIL"
    echo "hello" > file.txt
    git add file.txt
    LOBSTER_USER_CONFIG_DIR="$CFG6" git commit -m "test commit" < /dev/null > /tmp/test6_out.txt 2>&1
)
EXIT6=$?
OUT6="$(cat /tmp/test6_out.txt)"
rm -f /tmp/test6_out.txt

if [ "$EXIT6" -eq 0 ] && echo "$OUT6" | grep -qi "$WARNING_MARKER"; then
    ok "comment/blank lines ignored; real entry on later line still matches"
else
    fail "expected warning despite leading comment/blank lines; exit=$EXIT6 output='$OUT6'"
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
