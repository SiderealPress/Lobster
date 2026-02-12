#!/bin/bash
#===============================================================================
# Test Suite: lobster-sync
#
# Integration tests for the lobster-sync backup system:
#   1. lobster-sync-repo.sh   (core single-repo sync)
#   2. lobster-sync-daemon.sh (daemon wrapper)
#   3. Config schema validation
#
# Usage: bash tests/test_lobster_sync.sh
#===============================================================================

set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BOLD='\033[1m'
NC='\033[0m'

# Counters
PASS=0
FAIL=0
SKIP=0
TOTAL=0

# Script locations
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/scripts"
SYNC_SCRIPT="$SCRIPT_DIR/lobster-sync-repo.sh"
DAEMON_SCRIPT="$SCRIPT_DIR/lobster-sync-daemon.sh"

# Test isolation: use temp directories
TEST_TMPDIR=$(mktemp -d /tmp/lobster-sync-test-XXXXXX)

cleanup() {
    rm -rf "$TEST_TMPDIR"
}
trap cleanup EXIT

#===============================================================================
# Test Helpers
#===============================================================================

test_name=""

begin_test() {
    test_name="$1"
    TOTAL=$((TOTAL + 1))
}

pass() {
    PASS=$((PASS + 1))
    echo -e "  ${GREEN}PASS${NC} $test_name"
}

fail() {
    FAIL=$((FAIL + 1))
    local msg="${1:-}"
    if [ -n "$msg" ]; then
        echo -e "  ${RED}FAIL${NC} $test_name: $msg"
    else
        echo -e "  ${RED}FAIL${NC} $test_name"
    fi
}

skip() {
    SKIP=$((SKIP + 1))
    local msg="${1:-}"
    if [ -n "$msg" ]; then
        echo -e "  ${YELLOW}SKIP${NC} $test_name: $msg"
    else
        echo -e "  ${YELLOW}SKIP${NC} $test_name"
    fi
}

# Create a fresh test git repo with an initial commit
# Returns the repo path on stdout
create_test_repo() {
    local repo_dir="$TEST_TMPDIR/repo-$(date +%s%N)"
    mkdir -p "$repo_dir"
    git -C "$repo_dir" init -b main --quiet
    git -C "$repo_dir" config user.email "test@lobster.test"
    git -C "$repo_dir" config user.name "Lobster Test"
    echo "initial" > "$repo_dir/README.md"
    git -C "$repo_dir" add README.md
    git -C "$repo_dir" commit -m "initial commit" --quiet
    echo "$repo_dir"
}

# Create a bare remote for a test repo (for push testing)
create_bare_remote() {
    local remote_dir="$TEST_TMPDIR/remote-$(date +%s%N)"
    git init --bare --quiet "$remote_dir"
    echo "$remote_dir"
}

#===============================================================================
# Tests: lobster-sync-repo.sh -- Core Sync
#===============================================================================

echo ""
echo -e "${BOLD}=== lobster-sync-repo.sh (core sync) ===${NC}"

# Test 1: Exits with error when no arguments given
begin_test "Exits with usage error when no repo path given"
if "$SYNC_SCRIPT" 2>/dev/null; then
    fail "Expected non-zero exit code"
else
    EXIT_CODE=$?
    if [ "$EXIT_CODE" -eq 2 ]; then
        pass
    else
        fail "Expected exit code 2, got $EXIT_CODE"
    fi
fi

# Test 2: Exits with error for non-existent path
begin_test "Exits with error for non-existent path"
if "$SYNC_SCRIPT" "/tmp/nonexistent-lobster-path-$$" 2>/dev/null; then
    fail "Expected non-zero exit code"
else
    pass
fi

# Test 3: Exits with error for non-git directory
begin_test "Exits with error for non-git directory"
NOT_GIT="$TEST_TMPDIR/not-a-repo"
mkdir -p "$NOT_GIT"
if "$SYNC_SCRIPT" "$NOT_GIT" 2>/dev/null; then
    fail "Expected non-zero exit code"
else
    pass
fi

# Test 4: Creates sync branch with correct content
begin_test "Creates lobster-sync branch with working tree content"
REPO="$(create_test_repo)"
echo "uncommitted change" > "$REPO/README.md"
echo "new file" > "$REPO/newfile.txt"
LOBSTER_SYNC_DRY_RUN=1 "$SYNC_SCRIPT" "$REPO" 2>/dev/null
if git -C "$REPO" rev-parse refs/heads/lobster-sync > /dev/null 2>&1; then
    # Verify content on lobster-sync branch
    SYNC_README="$(git -C "$REPO" show lobster-sync:README.md)"
    SYNC_NEWFILE="$(git -C "$REPO" show lobster-sync:newfile.txt)"
    if [[ "$SYNC_README" == "uncommitted change" ]] && [[ "$SYNC_NEWFILE" == "new file" ]]; then
        pass
    else
        fail "Content mismatch: README='$SYNC_README' newfile='$SYNC_NEWFILE'"
    fi
else
    fail "lobster-sync branch was not created"
fi

# Test 5: Working directory is untouched after sync
begin_test "Working directory is untouched after sync"
REPO="$(create_test_repo)"
echo "my work in progress" > "$REPO/wip.txt"
# Record state before
BEFORE_CONTENT="$(cat "$REPO/wip.txt")"
BEFORE_LS="$(ls -la "$REPO")"
LOBSTER_SYNC_DRY_RUN=1 "$SYNC_SCRIPT" "$REPO" 2>/dev/null
# Verify state after
AFTER_CONTENT="$(cat "$REPO/wip.txt")"
AFTER_LS="$(ls -la "$REPO")"
if [[ "$BEFORE_CONTENT" == "$AFTER_CONTENT" ]] && [[ "$BEFORE_LS" == "$AFTER_LS" ]]; then
    pass
else
    fail "Working directory was modified"
fi

# Test 6: Staged changes survive sync intact
begin_test "Staged changes in real index survive sync intact"
REPO="$(create_test_repo)"
echo "staged content" > "$REPO/staged.txt"
git -C "$REPO" add staged.txt
# Record the real index state before sync
BEFORE_INDEX="$(git -C "$REPO" ls-files --stage)"
LOBSTER_SYNC_DRY_RUN=1 "$SYNC_SCRIPT" "$REPO" 2>/dev/null
# Verify the real index is unchanged
AFTER_INDEX="$(git -C "$REPO" ls-files --stage)"
if [[ "$BEFORE_INDEX" == "$AFTER_INDEX" ]]; then
    pass
else
    fail "Real index was modified: before='$BEFORE_INDEX' after='$AFTER_INDEX'"
fi

# Test 7: Current branch is not changed
begin_test "Current branch is unchanged after sync"
REPO="$(create_test_repo)"
git -C "$REPO" checkout -b feature-branch --quiet
BEFORE_BRANCH="$(git -C "$REPO" branch --show-current)"
LOBSTER_SYNC_DRY_RUN=1 "$SYNC_SCRIPT" "$REPO" 2>/dev/null
AFTER_BRANCH="$(git -C "$REPO" branch --show-current)"
if [[ "$BEFORE_BRANCH" == "$AFTER_BRANCH" ]]; then
    pass
else
    fail "Branch changed from '$BEFORE_BRANCH' to '$AFTER_BRANCH'"
fi

# Test 8: Untracked files are captured
begin_test "Untracked files are captured in sync"
REPO="$(create_test_repo)"
echo "untracked content" > "$REPO/untracked.txt"
mkdir -p "$REPO/subdir"
echo "nested untracked" > "$REPO/subdir/nested.txt"
LOBSTER_SYNC_DRY_RUN=1 "$SYNC_SCRIPT" "$REPO" 2>/dev/null
SYNC_UNTRACKED="$(git -C "$REPO" show lobster-sync:untracked.txt 2>/dev/null)"
SYNC_NESTED="$(git -C "$REPO" show lobster-sync:subdir/nested.txt 2>/dev/null)"
if [[ "$SYNC_UNTRACKED" == "untracked content" ]] && [[ "$SYNC_NESTED" == "nested untracked" ]]; then
    pass
else
    fail "Untracked files not captured"
fi

# Test 9: No-op when nothing has changed (idempotent)
begin_test "No-op when tree is unchanged (idempotent)"
REPO="$(create_test_repo)"
echo "some content" > "$REPO/file.txt"
LOBSTER_SYNC_DRY_RUN=1 "$SYNC_SCRIPT" "$REPO" 2>/dev/null
FIRST_COMMIT="$(git -C "$REPO" rev-parse lobster-sync)"
# Run again without changes
LOBSTER_SYNC_DRY_RUN=1 "$SYNC_SCRIPT" "$REPO" 2>/dev/null
SECOND_COMMIT="$(git -C "$REPO" rev-parse lobster-sync)"
if [[ "$FIRST_COMMIT" == "$SECOND_COMMIT" ]]; then
    pass
else
    fail "Created new commit despite no changes"
fi

# Test 10: Does create new commit when content changes
begin_test "Creates new commit when content changes between syncs"
REPO="$(create_test_repo)"
echo "version 1" > "$REPO/file.txt"
LOBSTER_SYNC_DRY_RUN=1 "$SYNC_SCRIPT" "$REPO" 2>/dev/null
FIRST_COMMIT="$(git -C "$REPO" rev-parse lobster-sync)"
echo "version 2" > "$REPO/file.txt"
LOBSTER_SYNC_DRY_RUN=1 "$SYNC_SCRIPT" "$REPO" 2>/dev/null
SECOND_COMMIT="$(git -C "$REPO" rev-parse lobster-sync)"
if [[ "$FIRST_COMMIT" != "$SECOND_COMMIT" ]]; then
    pass
else
    fail "Did not create new commit after changes"
fi

# Test 11: Commit message includes timestamp and branch name
begin_test "Commit message includes timestamp and branch name"
REPO="$(create_test_repo)"
git -C "$REPO" checkout -b my-feature --quiet
echo "feature work" > "$REPO/feature.txt"
LOBSTER_SYNC_DRY_RUN=1 "$SYNC_SCRIPT" "$REPO" 2>/dev/null
COMMIT_MSG="$(git -C "$REPO" log -1 --format='%s' lobster-sync)"
if [[ "$COMMIT_MSG" == sync:* ]] && [[ "$COMMIT_MSG" == *"my-feature"* ]]; then
    pass
else
    fail "Commit message: '$COMMIT_MSG'"
fi

# Test 12: Sync branch has linear history (parent chain)
begin_test "Sync branch maintains parent chain (linear history)"
REPO="$(create_test_repo)"
echo "v1" > "$REPO/file.txt"
LOBSTER_SYNC_DRY_RUN=1 "$SYNC_SCRIPT" "$REPO" 2>/dev/null
echo "v2" > "$REPO/file.txt"
LOBSTER_SYNC_DRY_RUN=1 "$SYNC_SCRIPT" "$REPO" 2>/dev/null
echo "v3" > "$REPO/file.txt"
LOBSTER_SYNC_DRY_RUN=1 "$SYNC_SCRIPT" "$REPO" 2>/dev/null
COMMIT_COUNT="$(git -C "$REPO" log --oneline lobster-sync | wc -l)"
if [[ "$COMMIT_COUNT" -eq 3 ]]; then
    pass
else
    fail "Expected 3 commits, got $COMMIT_COUNT"
fi

# Test 13: .gitignore patterns are respected
begin_test ".gitignore patterns are respected"
REPO="$(create_test_repo)"
echo "*.log" > "$REPO/.gitignore"
git -C "$REPO" add .gitignore
git -C "$REPO" commit -m "add gitignore" --quiet
echo "should be ignored" > "$REPO/debug.log"
echo "should be captured" > "$REPO/source.txt"
LOBSTER_SYNC_DRY_RUN=1 "$SYNC_SCRIPT" "$REPO" 2>/dev/null
# debug.log should NOT be in the sync branch
if git -C "$REPO" show lobster-sync:debug.log > /dev/null 2>&1; then
    fail "debug.log should have been ignored"
else
    # source.txt SHOULD be there
    SYNC_SOURCE="$(git -C "$REPO" show lobster-sync:source.txt 2>/dev/null)"
    if [[ "$SYNC_SOURCE" == "should be captured" ]]; then
        pass
    else
        fail "source.txt was not captured"
    fi
fi

# Test 14: .lobster-sync-exclude patterns are respected
begin_test ".lobster-sync-exclude patterns are respected"
REPO="$(create_test_repo)"
echo "*.secret" > "$REPO/.lobster-sync-exclude"
echo "supersecret" > "$REPO/passwords.secret"
echo "normal file" > "$REPO/normal.txt"
LOBSTER_SYNC_DRY_RUN=1 "$SYNC_SCRIPT" "$REPO" 2>/dev/null
# passwords.secret should NOT be in the sync branch
if git -C "$REPO" show lobster-sync:passwords.secret > /dev/null 2>&1; then
    fail "passwords.secret should have been excluded by .lobster-sync-exclude"
else
    SYNC_NORMAL="$(git -C "$REPO" show lobster-sync:normal.txt 2>/dev/null)"
    if [[ "$SYNC_NORMAL" == "normal file" ]]; then
        pass
    else
        fail "normal.txt was not captured"
    fi
fi

# Test 15: Custom sync branch name works
begin_test "Custom sync branch name works"
REPO="$(create_test_repo)"
echo "content" > "$REPO/file.txt"
LOBSTER_SYNC_DRY_RUN=1 "$SYNC_SCRIPT" "$REPO" "my-custom-sync" 2>/dev/null
if git -C "$REPO" rev-parse refs/heads/my-custom-sync > /dev/null 2>&1; then
    pass
else
    fail "Custom branch my-custom-sync was not created"
fi

# Test 16: Push to a bare remote works
begin_test "Push to bare remote succeeds"
REPO="$(create_test_repo)"
REMOTE="$(create_bare_remote)"
git -C "$REPO" remote add test-remote "$REMOTE"
echo "push test" > "$REPO/push.txt"
"$SYNC_SCRIPT" "$REPO" "lobster-sync" "test-remote" 2>/dev/null
# Verify the remote has the sync branch
if git -C "$REMOTE" rev-parse refs/heads/lobster-sync > /dev/null 2>&1; then
    pass
else
    fail "Sync branch not found on remote"
fi

# Test 17: Temp index is cleaned up after sync
begin_test "Temp index file is cleaned up"
REPO="$(create_test_repo)"
echo "temp test" > "$REPO/file.txt"
# Count lobster-sync-index temp files before
BEFORE_COUNT="$(ls /tmp/lobster-sync-index.* 2>/dev/null | wc -l)"
LOBSTER_SYNC_DRY_RUN=1 "$SYNC_SCRIPT" "$REPO" 2>/dev/null
AFTER_COUNT="$(ls /tmp/lobster-sync-index.* 2>/dev/null | wc -l)"
if [[ "$AFTER_COUNT" -le "$BEFORE_COUNT" ]]; then
    pass
else
    fail "Temp files leaked: before=$BEFORE_COUNT after=$AFTER_COUNT"
fi

# Test 18: QUIET mode suppresses output
begin_test "QUIET mode suppresses informational output"
REPO="$(create_test_repo)"
echo "quiet test" > "$REPO/file.txt"
OUTPUT="$(LOBSTER_SYNC_DRY_RUN=1 LOBSTER_SYNC_QUIET=1 "$SYNC_SCRIPT" "$REPO" 2>&1)"
if [[ -z "$OUTPUT" ]]; then
    pass
else
    fail "Expected no output, got: '$OUTPUT'"
fi

#===============================================================================
# Tests: lobster-sync-daemon.sh -- Daemon
#===============================================================================

echo ""
echo -e "${BOLD}=== lobster-sync-daemon.sh (daemon) ===${NC}"

# Test 19: --help exits cleanly
begin_test "--help exits with code 0"
if "$DAEMON_SCRIPT" --help > /dev/null 2>&1; then
    pass
else
    fail "Expected exit code 0"
fi

# Test 20: --status reports not running when daemon is not active
begin_test "--status reports not running when no daemon"
LOBSTER_SYNC_HOME="$TEST_TMPDIR/daemon-test-status" "$DAEMON_SCRIPT" --status > /dev/null 2>&1 || STATUS_CODE=$?
if [[ "${STATUS_CODE:-0}" -eq 1 ]]; then
    pass
else
    fail "Expected exit code 1 for not-running"
fi

# Test 21: --once fails with missing config
begin_test "--once fails when config file is missing"
DAEMON_HOME="$TEST_TMPDIR/daemon-no-config"
mkdir -p "$DAEMON_HOME"
if LOBSTER_SYNC_HOME="$DAEMON_HOME" "$DAEMON_SCRIPT" --once 2>/dev/null; then
    fail "Expected failure with missing config"
else
    pass
fi

# Test 22: --once runs a sync cycle with valid config
begin_test "--once runs a sync cycle with valid config"
REPO="$(create_test_repo)"
REMOTE="$(create_bare_remote)"
git -C "$REPO" remote add origin "$REMOTE" 2>/dev/null || true
DAEMON_HOME="$TEST_TMPDIR/daemon-once-test"
mkdir -p "$DAEMON_HOME"
cat > "$DAEMON_HOME/sync-config.json" << EOF
{
    "sync_interval_seconds": 10,
    "sync_branch": "lobster-sync",
    "repos": [
        {"path": "$REPO", "remote": "origin", "enabled": true}
    ]
}
EOF
echo "daemon sync content" > "$REPO/daemon-test.txt"
if LOBSTER_SYNC_HOME="$DAEMON_HOME" "$DAEMON_SCRIPT" --once 2>/dev/null; then
    # Verify sync happened
    if git -C "$REPO" rev-parse refs/heads/lobster-sync > /dev/null 2>&1; then
        pass
    else
        fail "Sync branch not created after --once"
    fi
else
    fail "Daemon --once exited with error"
fi

# Test 23: Daemon skips disabled repos
begin_test "Daemon skips disabled repos"
REPO_ENABLED="$(create_test_repo)"
REPO_DISABLED="$(create_test_repo)"
DAEMON_HOME="$TEST_TMPDIR/daemon-skip-test"
mkdir -p "$DAEMON_HOME"
cat > "$DAEMON_HOME/sync-config.json" << EOF
{
    "sync_interval_seconds": 10,
    "sync_branch": "lobster-sync",
    "repos": [
        {"path": "$REPO_ENABLED", "remote": "origin", "enabled": true},
        {"path": "$REPO_DISABLED", "remote": "origin", "enabled": false}
    ]
}
EOF
echo "enabled" > "$REPO_ENABLED/file.txt"
echo "disabled" > "$REPO_DISABLED/file.txt"
LOBSTER_SYNC_DRY_RUN=1 LOBSTER_SYNC_HOME="$DAEMON_HOME" "$DAEMON_SCRIPT" --once 2>/dev/null
ENABLED_HAS_BRANCH=$(git -C "$REPO_ENABLED" rev-parse --verify refs/heads/lobster-sync > /dev/null 2>&1 && echo "yes" || echo "no")
DISABLED_HAS_BRANCH=$(git -C "$REPO_DISABLED" rev-parse --verify refs/heads/lobster-sync > /dev/null 2>&1 && echo "yes" || echo "no")
if [[ "$ENABLED_HAS_BRANCH" == "yes" ]] && [[ "$DISABLED_HAS_BRANCH" == "no" ]]; then
    pass
else
    fail "enabled=$ENABLED_HAS_BRANCH disabled=$DISABLED_HAS_BRANCH"
fi

# Test 24: Daemon handles missing repo paths gracefully
begin_test "Daemon handles missing repo paths gracefully"
DAEMON_HOME="$TEST_TMPDIR/daemon-missing-test"
mkdir -p "$DAEMON_HOME"
cat > "$DAEMON_HOME/sync-config.json" << EOF
{
    "sync_interval_seconds": 10,
    "sync_branch": "lobster-sync",
    "repos": [
        {"path": "/tmp/nonexistent-lobster-$$", "remote": "origin", "enabled": true}
    ]
}
EOF
# Should not crash
if LOBSTER_SYNC_DRY_RUN=1 LOBSTER_SYNC_HOME="$DAEMON_HOME" "$DAEMON_SCRIPT" --once 2>/dev/null; then
    pass
else
    fail "Daemon crashed on missing repo path"
fi

# Test 25: Daemon creates log file
begin_test "Daemon creates log file"
REPO="$(create_test_repo)"
DAEMON_HOME="$TEST_TMPDIR/daemon-log-test"
mkdir -p "$DAEMON_HOME"
cat > "$DAEMON_HOME/sync-config.json" << EOF
{
    "sync_interval_seconds": 10,
    "sync_branch": "lobster-sync",
    "repos": [
        {"path": "$REPO", "remote": "origin", "enabled": true}
    ]
}
EOF
echo "log test" > "$REPO/file.txt"
LOBSTER_SYNC_DRY_RUN=1 LOBSTER_SYNC_HOME="$DAEMON_HOME" "$DAEMON_SCRIPT" --once 2>/dev/null
if [[ -f "$DAEMON_HOME/logs/sync.log" ]]; then
    pass
else
    fail "Log file not created at $DAEMON_HOME/logs/sync.log"
fi

# Test 26: Daemon handles multiple repos in one cycle
begin_test "Daemon syncs multiple enabled repos in one cycle"
REPO_A="$(create_test_repo)"
REPO_B="$(create_test_repo)"
DAEMON_HOME="$TEST_TMPDIR/daemon-multi-test"
mkdir -p "$DAEMON_HOME"
cat > "$DAEMON_HOME/sync-config.json" << EOF
{
    "sync_interval_seconds": 10,
    "sync_branch": "lobster-sync",
    "repos": [
        {"path": "$REPO_A", "remote": "origin", "enabled": true},
        {"path": "$REPO_B", "remote": "origin", "enabled": true}
    ]
}
EOF
echo "repo a" > "$REPO_A/file.txt"
echo "repo b" > "$REPO_B/file.txt"
LOBSTER_SYNC_DRY_RUN=1 LOBSTER_SYNC_HOME="$DAEMON_HOME" "$DAEMON_SCRIPT" --once 2>/dev/null
A_HAS_BRANCH=$(git -C "$REPO_A" rev-parse --verify refs/heads/lobster-sync > /dev/null 2>&1 && echo "yes" || echo "no")
B_HAS_BRANCH=$(git -C "$REPO_B" rev-parse --verify refs/heads/lobster-sync > /dev/null 2>&1 && echo "yes" || echo "no")
if [[ "$A_HAS_BRANCH" == "yes" ]] && [[ "$B_HAS_BRANCH" == "yes" ]]; then
    pass
else
    fail "A=$A_HAS_BRANCH B=$B_HAS_BRANCH"
fi

#===============================================================================
# Tests: Config Schema
#===============================================================================

echo ""
echo -e "${BOLD}=== Config Schema ===${NC}"

# Test 27: Example config is valid JSON
begin_test "Example config file is valid JSON"
EXAMPLE_CONFIG="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/config/sync-config.example.json"
if [[ -f "$EXAMPLE_CONFIG" ]] && jq . "$EXAMPLE_CONFIG" > /dev/null 2>&1; then
    pass
else
    fail "Example config is not valid JSON or not found at $EXAMPLE_CONFIG"
fi

# Test 28: Example config has required fields
begin_test "Example config has all required fields"
EXAMPLE_CONFIG="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/config/sync-config.example.json"
if [[ ! -f "$EXAMPLE_CONFIG" ]]; then
    fail "Example config not found"
else
    HAS_INTERVAL=$(jq 'has("sync_interval_seconds")' "$EXAMPLE_CONFIG")
    HAS_BRANCH=$(jq 'has("sync_branch")' "$EXAMPLE_CONFIG")
    HAS_REPOS=$(jq 'has("repos")' "$EXAMPLE_CONFIG")
    if [[ "$HAS_INTERVAL" == "true" ]] && [[ "$HAS_BRANCH" == "true" ]] && [[ "$HAS_REPOS" == "true" ]]; then
        pass
    else
        fail "interval=$HAS_INTERVAL branch=$HAS_BRANCH repos=$HAS_REPOS"
    fi
fi

# Test 29: Example config repos have required fields
begin_test "Example config repo entries have path, remote, enabled"
EXAMPLE_CONFIG="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/config/sync-config.example.json"
if [[ ! -f "$EXAMPLE_CONFIG" ]]; then
    fail "Example config not found"
else
    FIRST_REPO_FIELDS=$(jq '.repos[0] | has("path") and has("remote") and has("enabled")' "$EXAMPLE_CONFIG")
    if [[ "$FIRST_REPO_FIELDS" == "true" ]]; then
        pass
    else
        fail "First repo entry missing required fields"
    fi
fi

# Test 30: Scripts are executable
begin_test "Both scripts are executable"
if [[ -x "$SYNC_SCRIPT" ]] && [[ -x "$DAEMON_SCRIPT" ]]; then
    pass
else
    ERRORS=""
    [[ ! -x "$SYNC_SCRIPT" ]] && ERRORS="$ERRORS lobster-sync-repo.sh"
    [[ ! -x "$DAEMON_SCRIPT" ]] && ERRORS="$ERRORS lobster-sync-daemon.sh"
    fail "Not executable:$ERRORS"
fi

#===============================================================================
# Summary
#===============================================================================

echo ""
echo -e "${BOLD}==============================${NC}"
echo -e "${BOLD}Results: $TOTAL tests${NC}"
echo -e "  ${GREEN}PASS: $PASS${NC}"
if [ "$FAIL" -gt 0 ]; then
    echo -e "  ${RED}FAIL: $FAIL${NC}"
fi
if [ "$SKIP" -gt 0 ]; then
    echo -e "  ${YELLOW}SKIP: $SKIP${NC}"
fi
echo -e "${BOLD}==============================${NC}"

if [ "$FAIL" -gt 0 ]; then
    exit 1
else
    echo -e "${GREEN}All tests passed!${NC}"
    exit 0
fi
