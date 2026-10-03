#!/usr/bin/env bash
# =============================================================================
# test_pre_commit_untracked_root_files.sh
# Behavioral tests for the "Untracked Suspicious Root File Check" added to
# .githooks/pre-commit (issue #2263, live_full_dump_now.json incident).
#
# These tests exercise the REAL hook file against REAL throwaway git repos —
# not a synthetic re-implementation — by invoking `git commit` end-to-end with
# core.hooksPath pointed at the repo's .githooks directory.
#
# Covers:
#   1. No suspicious untracked file present        -> no-op, no warning
#   2. Suspicious untracked file present (root),
#      non-interactive commit                       -> warns; commit still
#                                                        succeeds (CI policy)
#   3. Suspicious untracked file present, INTERACTIVE
#      (real pty via expect)                         -> confirmation prompt
#                                                        actually gates the
#                                                        commit ('n' aborts,
#                                                        'y' proceeds)
#   4. Suspicious file lives in a SUBDIRECTORY, not
#      repo root                                     -> not flagged (scope is
#                                                        root-level only)
#   5. Suspicious-named file is TRACKED (already
#      committed), not untracked                     -> not flagged (this
#                                                        check only looks at
#                                                        `git ls-files
#                                                        --others`)
#   6. Suspicious-named file is covered by a
#      .gitignore rule                                -> not flagged (defense
#                                                        in depth: gitignore
#                                                        is the primary layer,
#                                                        this check covers the
#                                                        residual gap)
#   7. Ordinary untracked file (no suspicious name)   -> not flagged
#
# Run: bash tests/unit/test_pre_commit_untracked_root_files.sh
# Requires: bash, git, expect (for the interactive-prompt test)
# =============================================================================

set -uo pipefail

PASS=0
FAIL=0
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
HOOK_SRC="$REPO_ROOT/.githooks/pre-commit"

WARNING_MARKER="suspicious untracked file"

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
# Helper: build a throwaway repo wired to the real .githooks/pre-commit hook.
# An unset LOBSTER_USER_CONFIG_DIR pointed at an empty dir keeps the
# commit-author-email check (which runs earlier in the same hook) a no-op so
# it can't interfere with these tests.
# ---------------------------------------------------------------------------
make_repo() {
    local repo_dir
    repo_dir="$(mktemp -d /tmp/test_pre_commit_untracked_repo_XXXXXX)"
    TMP_DIRS+=("$repo_dir")

    git init -q "$repo_dir"
    mkdir -p "$repo_dir/.githooks"
    cp "$HOOK_SRC" "$repo_dir/.githooks/pre-commit"
    chmod +x "$repo_dir/.githooks/pre-commit"

    (
        cd "$repo_dir" || exit 1
        git config core.hooksPath .githooks
        git config user.name "Test User"
        git config user.email "clean-dev@example.com"
    )

    echo "$repo_dir"
}

empty_config_dir() {
    local cfg_dir
    cfg_dir="$(mktemp -d /tmp/test_pre_commit_untracked_cfg_XXXXXX)"
    TMP_DIRS+=("$cfg_dir")
    echo "$cfg_dir"
}

# ---------------------------------------------------------------------------
# Test 1: no suspicious untracked file -> no-op
# ---------------------------------------------------------------------------
echo ""
echo "=== Test 1: no suspicious untracked file -> silent no-op ==="

REPO1="$(make_repo)"
CFG1="$(empty_config_dir)"

(
    cd "$REPO1" || exit 1
    echo "hello" > file.txt
    git add file.txt
    LOBSTER_USER_CONFIG_DIR="$CFG1" git commit -m "test commit" < /dev/null > /tmp/test1_out.txt 2>&1
)
EXIT1=$?
OUT1="$(cat /tmp/test1_out.txt)"
rm -f /tmp/test1_out.txt

if [ "$EXIT1" -eq 0 ] && ! echo "$OUT1" | grep -qi "$WARNING_MARKER"; then
    ok "no suspicious untracked file -> commit succeeds with no warning"
else
    fail "expected clean commit with no warning; exit=$EXIT1 output='$OUT1'"
fi

# ---------------------------------------------------------------------------
# Test 2: suspicious untracked file in repo root, non-interactive -> warns,
# commit still succeeds (CI/non-interactive policy: never hang, never block)
# ---------------------------------------------------------------------------
echo ""
echo "=== Test 2: live_full_dump_now.json-shaped untracked root file, non-interactive -> warns but does not block ==="

REPO2="$(make_repo)"
CFG2="$(empty_config_dir)"

(
    cd "$REPO2" || exit 1
    echo '{"presentationId": "abc123fakeid", "slides": []}' > live_full_dump_now.json
    echo "hello" > unrelated.txt
    git add unrelated.txt
    LOBSTER_USER_CONFIG_DIR="$CFG2" git commit -m "unrelated change" < /dev/null > /tmp/test2_out.txt 2>&1
)
EXIT2=$?
OUT2="$(cat /tmp/test2_out.txt)"
rm -f /tmp/test2_out.txt

if [ "$EXIT2" -eq 0 ] && echo "$OUT2" | grep -qi "$WARNING_MARKER" && echo "$OUT2" | grep -q "live_full_dump_now.json"; then
    ok "suspicious untracked root file (non-interactive) -> warning printed, commit not blocked"
else
    fail "expected warning + successful commit; exit=$EXIT2 output='$OUT2'"
fi

if (cd "$REPO2" && git log --oneline 2>/dev/null | grep -q "unrelated change"); then
    ok "commit exists in history (non-interactive never blocks)"
else
    fail "commit should exist in history after non-interactive warning"
fi

# ---------------------------------------------------------------------------
# Test 3: suspicious untracked file, INTERACTIVE (real pty via expect) ->
# confirmation prompt actually gates the commit
# ---------------------------------------------------------------------------
echo ""
echo "=== Test 3: suspicious untracked root file, interactive pty -> confirmation prompt gates commit ==="

if ! command -v expect >/dev/null 2>&1; then
    echo "  SKIP: 'expect' not installed — cannot drive a real pty for this test"
else
    REPO3A="$(make_repo)"
    CFG3="$(empty_config_dir)"
    (
        cd "$REPO3A" || exit 1
        echo '{"presentationId": "abc123fakeid"}' > secret_export_dump.json
        echo "hello" > unrelated.txt
        git add unrelated.txt
    )

    # 3a: answer "n" (or default Enter) -> commit is ABORTED
    EXPECT_SCRIPT_N=$(mktemp /tmp/test_pre_commit_untracked_expect_n_XXXXXX.exp)
    TMP_DIRS+=("$EXPECT_SCRIPT_N")
    cat > "$EXPECT_SCRIPT_N" << EOF
set timeout 10
spawn env LOBSTER_USER_CONFIG_DIR=$CFG3 git -C $REPO3A commit -m "test commit"
expect "Continue committing anyway?"
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
        echo '{"presentationId": "abc123fakeid"}' > secret_export_dump.json
        echo "hello" > unrelated.txt
        git add unrelated.txt
    )

    EXPECT_SCRIPT_Y=$(mktemp /tmp/test_pre_commit_untracked_expect_y_XXXXXX.exp)
    TMP_DIRS+=("$EXPECT_SCRIPT_Y")
    cat > "$EXPECT_SCRIPT_Y" << EOF
set timeout 10
spawn env LOBSTER_USER_CONFIG_DIR=$CFG3 git -C $REPO3B commit -m "test commit"
expect "Continue committing anyway?"
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
# Test 4: suspicious file lives in a subdirectory, not repo root -> not
# flagged (this check is scoped to root-level files only)
# ---------------------------------------------------------------------------
echo ""
echo "=== Test 4: suspicious-named file in a subdirectory -> not flagged (root-only scope) ==="

REPO4="$(make_repo)"
CFG4="$(empty_config_dir)"

(
    cd "$REPO4" || exit 1
    mkdir -p scratch
    echo '{"presentationId": "abc123fakeid"}' > scratch/live_full_dump_now.json
    echo "hello" > unrelated.txt
    git add unrelated.txt
    LOBSTER_USER_CONFIG_DIR="$CFG4" git commit -m "test commit" < /dev/null > /tmp/test4_out.txt 2>&1
)
EXIT4=$?
OUT4="$(cat /tmp/test4_out.txt)"
rm -f /tmp/test4_out.txt

if [ "$EXIT4" -eq 0 ] && ! echo "$OUT4" | grep -qi "$WARNING_MARKER"; then
    ok "suspicious file in subdirectory -> not flagged (root-only scope)"
else
    fail "expected no warning for a subdirectory file; exit=$EXIT4 output='$OUT4'"
fi

# ---------------------------------------------------------------------------
# Test 5: suspicious-named file is already TRACKED -> not flagged (this
# check only looks at `git ls-files --others`, i.e. untracked files)
# ---------------------------------------------------------------------------
echo ""
echo "=== Test 5: suspicious-named file already tracked -> not flagged ==="

REPO5="$(make_repo)"
CFG5="$(empty_config_dir)"

(
    cd "$REPO5" || exit 1
    echo '{"presentationId": "abc123fakeid"}' > data_export.json
    git add data_export.json
    git commit -q -m "add tracked export file" < /dev/null
    echo "more" >> data_export.json
    git add data_export.json
    LOBSTER_USER_CONFIG_DIR="$CFG5" git commit -m "update tracked file" < /dev/null > /tmp/test5_out.txt 2>&1
)
EXIT5=$?
OUT5="$(cat /tmp/test5_out.txt)"
rm -f /tmp/test5_out.txt

if [ "$EXIT5" -eq 0 ] && ! echo "$OUT5" | grep -qi "$WARNING_MARKER"; then
    ok "already-tracked suspicious-named file -> not flagged"
else
    fail "expected no warning for a tracked file; exit=$EXIT5 output='$OUT5'"
fi

# ---------------------------------------------------------------------------
# Test 6: suspicious file covered by a .gitignore rule -> not flagged
# (defense in depth: .gitignore is the primary layer; this check exists for
# the residual gap of filename shapes nobody has thought to ignore yet)
# ---------------------------------------------------------------------------
echo ""
echo "=== Test 6: suspicious file already covered by .gitignore -> not flagged ==="

REPO6="$(make_repo)"
CFG6="$(empty_config_dir)"

(
    cd "$REPO6" || exit 1
    printf '*dump*.json\n' > .gitignore
    git add .gitignore
    git commit -q -m "add gitignore rule" < /dev/null
    echo '{"presentationId": "abc123fakeid"}' > live_full_dump_now.json
    echo "hello" > unrelated.txt
    git add unrelated.txt
    LOBSTER_USER_CONFIG_DIR="$CFG6" git commit -m "test commit" < /dev/null > /tmp/test6_out.txt 2>&1
)
EXIT6=$?
OUT6="$(cat /tmp/test6_out.txt)"
rm -f /tmp/test6_out.txt

if [ "$EXIT6" -eq 0 ] && ! echo "$OUT6" | grep -qi "$WARNING_MARKER"; then
    ok "gitignore-covered suspicious file -> not flagged (primary layer handles it)"
else
    fail "expected no warning once .gitignore covers the file; exit=$EXIT6 output='$OUT6'"
fi

# ---------------------------------------------------------------------------
# Test 7: ordinary untracked file with no suspicious name -> not flagged
# ---------------------------------------------------------------------------
echo ""
echo "=== Test 7: ordinary untracked file, no suspicious name -> not flagged ==="

REPO7="$(make_repo)"
CFG7="$(empty_config_dir)"

(
    cd "$REPO7" || exit 1
    echo "just some notes" > scratch_notes.md
    echo "hello" > unrelated.txt
    git add unrelated.txt
    LOBSTER_USER_CONFIG_DIR="$CFG7" git commit -m "test commit" < /dev/null > /tmp/test7_out.txt 2>&1
)
EXIT7=$?
OUT7="$(cat /tmp/test7_out.txt)"
rm -f /tmp/test7_out.txt

if [ "$EXIT7" -eq 0 ] && ! echo "$OUT7" | grep -qi "$WARNING_MARKER"; then
    ok "ordinary untracked file with no suspicious name -> not flagged"
else
    fail "expected no warning for an ordinary untracked file; exit=$EXIT7 output='$OUT7'"
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
