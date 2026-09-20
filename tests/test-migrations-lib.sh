#!/bin/bash
#===============================================================================
# Test Suite: Migration Runner (scripts/lib/migrations.sh)
#
# Verifies (issue #2200):
#   1. run_migrations() is idempotent and correctly applies a representative
#      migration (Migration 96: CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT=0) to a
#      fake config.env in isolation, without touching the real host.
#   2. install.sh actually sources scripts/lib/migrations.sh and calls
#      run_migrations() unconditionally — this is the actual bug fix: prior
#      to this change, install.sh never invoked the migration runner at all,
#      so a reimage-via-restore silently skipped every config.env migration.
#   3. upgrade.sh still sources the shared lib and calls run_migrations().
#
# Also verifies (issue #2208): Migration 101 sets
# mcpServers."lobster-inbox".timeout in ~/.claude.json when missing, is a
# byte-for-byte no-op once set, and refuses to fabricate a server entry when
# lobster-inbox is not registered at all. Run against $TEST_TMPDIR fixtures via
# the CLAUDE_JSON override so the real ~/.claude.json is never written.
#
# Also verifies (issue #2246): run_migrations() runs all ~99 migrations
# unconditionally, and several of them shell out directly to the real
# `crontab` binary and to `sudo` (systemctl, usermod, tee) - commands that
# are NOT sandboxed by faking $LOBSTER_DIR/$WORKSPACE_DIR/etc, since
# `crontab -l`/`crontab -` always read/write the real per-user system
# crontab regardless of those variables. Running this test previously wrote
# real entries (built from the fake temp path) into the actual system
# crontab. This suite stubs `crontab` and `sudo` via a fake-bin directory
# prepended to $PATH (catches both direct shell calls and the
# subprocess.run(["sudo", ...]) calls made by the embedded Python migration
# block) and asserts the real system crontab is byte-for-byte unchanged
# after run_migrations() executes.
#
# Usage: bash tests/test-migrations-lib.sh
#        (run from repo root or any directory)
#===============================================================================

set -uo pipefail

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BOLD='\033[1m'
NC='\033[0m'

PASS=0
FAIL=0
TOTAL=0

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LIB="$REPO_ROOT/scripts/lib/migrations.sh"

TEST_TMPDIR=$(mktemp -d /tmp/lobster-test-migrations-XXXXXX)
cleanup() { rm -rf "$TEST_TMPDIR"; }
trap cleanup EXIT

# Resolve the real `crontab` binary's absolute path BEFORE any $PATH
# stubbing happens below, and snapshot the real crontab's current contents.
# After run_migrations() executes (with the fake-bin stub shadowing
# `crontab` in $PATH), we re-snapshot via this same absolute path and
# assert byte-for-byte equality - proving the stub, not the real binary,
# absorbed every migration's crontab read/write (issue #2246).
REAL_CRONTAB_BIN="$(command -v crontab || true)"
if [ -n "$REAL_CRONTAB_BIN" ]; then
    REAL_CRONTAB_BEFORE="$("$REAL_CRONTAB_BIN" -l 2>/dev/null || true)"
fi

pass() { PASS=$((PASS + 1)); TOTAL=$((TOTAL + 1)); echo -e "  ${GREEN}PASS${NC} $1"; }
fail() { FAIL=$((FAIL + 1)); TOTAL=$((TOTAL + 1)); echo -e "  ${RED}FAIL${NC} $1"; echo -e "       ${YELLOW}$2${NC}"; }

assert_file_contains() {
    local label="$1" file="$2" pattern="$3"
    if grep -qF "$pattern" "$file" 2>/dev/null; then
        pass "$label"
    else
        fail "$label" "expected '$file' to contain: $pattern"
    fi
}

assert_equal() {
    local label="$1" expected="$2" actual="$3"
    if [ "$expected" = "$actual" ]; then
        pass "$label"
    else
        fail "$label" "expected: $expected / got: $actual"
    fi
}

assert_count() {
    local label="$1" expected="$2" actual="$3"
    if [ "$expected" = "$actual" ]; then
        pass "$label"
    else
        fail "$label" "expected count: $expected / got: $actual"
    fi
}

echo ""
echo -e "${BOLD}Migration Runner Tests${NC}"
echo "lib: $LIB"
echo ""

#===============================================================================
# Group 1: run_migrations() behavior, in isolation
#===============================================================================
echo "-- run_migrations() applies and is idempotent --"

FAKE_LOBSTER_DIR="$TEST_TMPDIR/lobster"
FAKE_WORKSPACE_DIR="$TEST_TMPDIR/lobster-workspace"
FAKE_MESSAGES_DIR="$TEST_TMPDIR/messages"
FAKE_CONFIG_DIR="$TEST_TMPDIR/lobster-config"
FAKE_USER_CONFIG_DIR="$TEST_TMPDIR/lobster-user-config"
FAKE_CLAUDE_SETTINGS="$TEST_TMPDIR/settings.json"
# Migration 101 (issue #2208) patches ~/.claude.json, which the fake
# $LOBSTER_DIR/$WORKSPACE_DIR sandbox does NOT cover — same class of
# non-sandboxed host resource as `crontab`/`sudo` above. migrations.sh honours
# an optional CLAUDE_JSON override for exactly this reason, so point it at
# fixtures under $TEST_TMPDIR and leave the real ~/.claude.json untouched.
FAKE_CLAUDE_JSON="$TEST_TMPDIR/claude.json"
FAKE_CLAUDE_JSON_NO_SERVER="$TEST_TMPDIR/claude-no-server.json"
# The value the spec (issue #2208 / install.sh) requires: ~20.8h in ms,
# comfortably above wait_for_messages' own 20h max.
MCP_IDLE_TIMEOUT_MS=75000000
FAKE_VENV_DIR="$TEST_TMPDIR/lobster/.venv"
mkdir -p "$FAKE_LOBSTER_DIR" "$FAKE_WORKSPACE_DIR" "$FAKE_MESSAGES_DIR/inbox" "$FAKE_CONFIG_DIR" "$FAKE_USER_CONFIG_DIR" "$FAKE_VENV_DIR/bin"

# Pre-existing config.env with everything except the migration-96 key,
# mirroring a host whose config.env predates issue #2142's fix.
cat > "$FAKE_CONFIG_DIR/config.env" <<'EOF'
TELEGRAM_BOT_TOKEN=fake-token
LOBSTER_ADMIN_CHAT_ID=12345
EOF

# Minimal jobs.json (issue #2246 follow-up): seeds a single enabled job with
# an absolute `command` path and a `schedule`, matching the exact shape
# Migration 55 (systemd-timer-from-jobs.json, scripts/lib/migrations.sh
# ~line 1055) requires to actually run its migration branch rather than be
# skipped because $WORKSPACE_DIR/scheduled-jobs/jobs.json doesn't exist. That
# branch is the one embedding the Python subprocess.run(["sudo", "tee", ...])
# call this suite's sudo stub needs to intercept and prove it caught.
mkdir -p "$FAKE_WORKSPACE_DIR/scheduled-jobs"
cat > "$FAKE_WORKSPACE_DIR/scheduled-jobs/jobs.json" <<'EOF'
{
  "jobs": {
    "test-follow-up-job": {
      "enabled": true,
      "command": "/usr/local/bin/lobster-test-follow-up-job.sh",
      "schedule": "*-*-* 04:00:00",
      "description": "Fake job fixture for migration test (issue #2246 follow-up)"
    }
  }
}
EOF

# Minimal logging/env contract required by scripts/lib/migrations.sh (see its
# header docstring) - install.sh's own stubs, reused here 1:1.
info()    { :; }
success() { :; }
warn()    { :; }
error()   { :; }
step()    { :; }
substep() { :; }
log_to_file() { :; }

# ---------------------------------------------------------------------------
# Command isolation (issue #2246): several migrations shell out directly to
# `crontab` and `sudo` (systemctl, usermod, tee) - real system binaries that
# are NOT sandboxed by the fake $LOBSTER_DIR/$WORKSPACE_DIR/etc above. A
# fake-bin directory prepended to $PATH intercepts these at the command
# resolution boundary, so it catches both plain shell invocations in
# migrations.sh AND the subprocess.run(["sudo", ...]) calls made by the
# embedded Python migration block (a bash function override would only
# catch the former).
FAKE_BIN_DIR="$TEST_TMPDIR/fake-bin"
FAKE_CRONTAB_STATE="$TEST_TMPDIR/fake-crontab-state"
mkdir -p "$FAKE_BIN_DIR"
: > "$FAKE_CRONTAB_STATE"

cat > "$FAKE_BIN_DIR/crontab" <<EOF
#!/bin/bash
# Fake crontab stub (tests/test-migrations-lib.sh, issue #2246) - never
# touches the real system crontab. Mimics the subset of \`crontab\` usage
# migrations.sh relies on: \`crontab -l\`, \`crontab -\` (write stdin),
# and \`crontab <file>\`.
#
# Several migrations chain \`crontab -l | grep ... | crontab -\` - bash
# starts every pipeline stage concurrently, so the read-side (\`-l\`) and
# write-side (\`-\`) invocations of this stub run at the same time. Writing
# via a plain \`cat > "\$STATE"\` truncates the state file at open time,
# racing the concurrent read-side's \`cat "\$STATE"\` and intermittently
# handing it a truncated/empty file. Write to a temp file and \`mv\` it into
# place instead (same technique the real crontab binary uses) so the state
# file is always replaced atomically: a concurrent reader sees either the
# complete old content or the complete new content, never a partial file.
STATE="$FAKE_CRONTAB_STATE"
case "\${1:-}" in
    -l)
        [ -s "\$STATE" ] && cat "\$STATE" || exit 1
        ;;
    -|"")
        TMP="\$STATE.tmp.\$\$"
        cat > "\$TMP" && mv "\$TMP" "\$STATE"
        ;;
    *)
        [ -f "\$1" ] && cp "\$1" "\$STATE" || exit 1
        ;;
esac
EOF
chmod +x "$FAKE_BIN_DIR/crontab"

FAKE_SUDO_TEE_LOG="$TEST_TMPDIR/fake-sudo-tee-log"
: > "$FAKE_SUDO_TEE_LOG"

cat > "$FAKE_BIN_DIR/sudo" <<EOF
#!/bin/bash
# Fake sudo stub (tests/test-migrations-lib.sh, issue #2246) - swallows all
# privileged calls (systemctl, usermod, cp, tee, ldconfig, ...) so tests
# never mutate the real host. \`sudo -n true\` (passwordless-sudo probe)
# succeeds; \`sudo tee ...\` consumes stdin so pipelines don't block.
#
# For \`tee\`, also record the target path into a log file (issue #2246
# follow-up) - this is how the test proves the embedded Python migration's
# subprocess.run(["sudo", "tee", ...]) call (Migration 55,
# systemd-timer-from-jobs.json) was actually caught by this stub, the same
# way the crontab stub's state file proves migrations 28/52 were caught.
TEE_LOG="$FAKE_SUDO_TEE_LOG"
if [ "\${1:-}" = "-n" ] && [ "\${2:-}" = "true" ]; then
    exit 0
fi
if [ "\${1:-}" = "tee" ]; then
    echo "\${2:-}" >> "\$TEE_LOG"
    cat >/dev/null
    exit 0
fi
exit 0
EOF
chmod +x "$FAKE_BIN_DIR/sudo"

# Fake pidof stub (tests/test-migrations-lib.sh, issue #2246 follow-up) -
# Migration 55 (systemd-timer-from-jobs.json) and a couple of other
# migrations gate their body on `pidof systemd >/dev/null 2>&1` succeeding,
# i.e. "are we actually running under systemd as PID 1". That's true on a
# real Lobster host but false in this suite's Docker test container
# (tests/docker/Dockerfile.test runs debian:bookworm-slim without systemd as
# PID 1), so those migration branches - including the one this suite's
# sudo-tee assertion below depends on - would otherwise be silently skipped
# there, and the assertion would hard-fail even though the crontab/sudo
# isolation itself works correctly. Stub `pidof` to always report systemd as
# present so Migration 55 actually runs and gets exercised in every
# environment this suite runs in, the same way `crontab`/`sudo` are stubbed
# above rather than conditionally skipped.
cat > "$FAKE_BIN_DIR/pidof" <<'EOF'
#!/bin/bash
# Fake pidof stub (tests/test-migrations-lib.sh, issue #2246 follow-up) -
# always reports the queried process as running (exit 0), so
# `pidof systemd >/dev/null 2>&1` succeeds regardless of whether this test
# runs on a real systemd host or in a systemd-less container.
exit 0
EOF
chmod +x "$FAKE_BIN_DIR/pidof"

export PATH="$FAKE_BIN_DIR:$PATH"

DRY_RUN=false
LOBSTER_DIR="$FAKE_LOBSTER_DIR"
WORKSPACE_DIR="$FAKE_WORKSPACE_DIR"
MESSAGES_DIR="$FAKE_MESSAGES_DIR"
LOBSTER_CONFIG_DIR="$FAKE_CONFIG_DIR"
USER_CONFIG_DIR="$FAKE_USER_CONFIG_DIR"
CONFIG_FILE="$FAKE_CONFIG_DIR/config.env"
CLAUDE_SETTINGS="$FAKE_CLAUDE_SETTINGS"
CLAUDE_JSON="$FAKE_CLAUDE_JSON"
VENV_DIR="$FAKE_VENV_DIR"

# Fixture: a host whose lobster-inbox MCP server is registered but has no
# per-server "timeout" key — i.e. installed before the install.sh patch landed
# and only ever updated via upgrade.sh (the exact gap issue #2208 describes).
cat > "$FAKE_CLAUDE_JSON" <<'EOF'
{
  "hasCompletedOnboarding": true,
  "mcpServers": {
    "lobster-inbox": {
      "type": "http",
      "url": "http://localhost:8766/mcp"
    }
  }
}
EOF

# Fixture: lobster-inbox is not registered at all. Setting a nested .timeout
# here would fabricate a server entry with no transport/url, so the migration
# must leave this file alone.
cat > "$FAKE_CLAUDE_JSON_NO_SERVER" <<'EOF'
{
  "hasCompletedOnboarding": true,
  "mcpServers": {}
}
EOF

# Migrations 96 and 100 gate on `[ -z "${VAR:-}" ]` after sourcing the fake
# config.env, so they skip whenever the *ambient* environment already exports
# those keys — which is exactly the case when this suite is run from inside a
# live Lobster/Claude Code session that has them set. That made the two
# config.env assertions below fail for environmental reasons unrelated to the
# migrations. Clear them so the suite tests the migration, not the shell it
# happens to be launched from.
unset CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT CLAUDE_CODE_FORK_SUBAGENT

# shellcheck source=../scripts/lib/migrations.sh
source "$LIB"

run_migrations
assert_file_contains "Migration 96 applies CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT=0 to config.env" \
    "$CONFIG_FILE" "CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT=0"

run_migrations
occurrences=$(grep -c "^CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT=" "$CONFIG_FILE")
assert_count "Migration 96 is idempotent (running twice does not duplicate the key)" "1" "$occurrences"

assert_file_contains "Migration 100 applies CLAUDE_CODE_FORK_SUBAGENT=0 to config.env" \
    "$CONFIG_FILE" "CLAUDE_CODE_FORK_SUBAGENT=0"

run_migrations
occurrences=$(grep -c "^CLAUDE_CODE_FORK_SUBAGENT=" "$CONFIG_FILE")
assert_count "Migration 100 is idempotent (running twice does not duplicate the key)" "1" "$occurrences"

#===============================================================================
# Group 1c: Migration 101 — lobster-inbox MCP idle timeout in ~/.claude.json
# (issue #2208)
#===============================================================================
echo ""
echo "-- Migration 101 sets the lobster-inbox MCP idle timeout --"

if ! command -v jq >/dev/null 2>&1; then
    pass "Migration 101 assertions (skipped: jq not available on this host)"
else
    # Branch 1: timeout absent -> migration sets it. (run_migrations already ran
    # above against $FAKE_CLAUDE_JSON.)
    assert_equal "Migration 101 sets lobster-inbox timeout when absent" \
        "$MCP_IDLE_TIMEOUT_MS" \
        "$(jq -r '.mcpServers."lobster-inbox".timeout' "$FAKE_CLAUDE_JSON")"

    assert_equal "Migration 101 preserves the existing server config (url untouched)" \
        "http://localhost:8766/mcp" \
        "$(jq -r '.mcpServers."lobster-inbox".url' "$FAKE_CLAUDE_JSON")"

    # Branch 2: already set -> no-op. Byte-compare the whole file across another
    # run: nothing is rewritten, reordered, or reformatted.
    cp "$FAKE_CLAUDE_JSON" "$TEST_TMPDIR/claude.json.before-noop"
    run_migrations
    if cmp -s "$TEST_TMPDIR/claude.json.before-noop" "$FAKE_CLAUDE_JSON"; then
        pass "Migration 101 is a byte-for-byte no-op when the timeout is already set"
    else
        fail "Migration 101 is a byte-for-byte no-op when the timeout is already set" \
            "~/.claude.json was rewritten on a run where nothing needed changing"
    fi

    # Branch 3: lobster-inbox not registered -> leave the file alone rather than
    # fabricating a transport-less server entry.
    CLAUDE_JSON="$FAKE_CLAUDE_JSON_NO_SERVER"
    cp "$FAKE_CLAUDE_JSON_NO_SERVER" "$TEST_TMPDIR/claude-no-server.json.before"
    run_migrations
    assert_equal "Migration 101 does not fabricate a lobster-inbox entry when unregistered" \
        "null" \
        "$(jq -r '.mcpServers."lobster-inbox" // "null"' "$FAKE_CLAUDE_JSON_NO_SERVER")"
    if cmp -s "$TEST_TMPDIR/claude-no-server.json.before" "$FAKE_CLAUDE_JSON_NO_SERVER"; then
        pass "Migration 101 leaves an unregistered-server ~/.claude.json byte-for-byte unchanged"
    else
        fail "Migration 101 leaves an unregistered-server ~/.claude.json byte-for-byte unchanged" \
            "file was modified even though lobster-inbox is not registered"
    fi
    CLAUDE_JSON="$FAKE_CLAUDE_JSON"

    # Branch 4: a symlinked ~/.claude.json must be followed, not replaced. The
    # migration writes by atomic rename, so without resolving the link first the
    # symlink itself would be clobbered by a regular file.
    FAKE_CLAUDE_JSON_TARGET="$TEST_TMPDIR/dotfiles-claude.json"
    FAKE_CLAUDE_JSON_LINK="$TEST_TMPDIR/claude-symlink.json"
    cat > "$FAKE_CLAUDE_JSON_TARGET" <<'EOF'
{
  "mcpServers": {
    "lobster-inbox": {
      "type": "http",
      "url": "http://localhost:8766/mcp"
    }
  }
}
EOF
    ln -sf "$FAKE_CLAUDE_JSON_TARGET" "$FAKE_CLAUDE_JSON_LINK"
    CLAUDE_JSON="$FAKE_CLAUDE_JSON_LINK"
    run_migrations
    assert_equal "Migration 101 writes through a symlinked ~/.claude.json" \
        "$MCP_IDLE_TIMEOUT_MS" \
        "$(jq -r '.mcpServers."lobster-inbox".timeout' "$FAKE_CLAUDE_JSON_TARGET")"
    if [ -L "$FAKE_CLAUDE_JSON_LINK" ]; then
        pass "Migration 101 leaves the symlink itself intact (does not replace it with a file)"
    else
        fail "Migration 101 leaves the symlink itself intact (does not replace it with a file)" \
            "the atomic rename clobbered the symlink"
    fi
    CLAUDE_JSON="$FAKE_CLAUDE_JSON"

    # Host isolation note: we deliberately do NOT checksum the real
    # ~/.claude.json before/after. On a live Lobster host the running Claude
    # Code process rewrites that file continuously (session/project state), so
    # such an assertion is inherently flaky and says nothing about this
    # migration. Isolation is instead established structurally: the migration
    # reads its target from $CLAUDE_JSON, which is pointed at $TEST_TMPDIR
    # fixtures above, and branches 1-3 prove the writes landed there.
fi

#===============================================================================
# Group 1b: crontab/sudo isolation (issue #2246)
#===============================================================================
echo ""
echo "-- run_migrations() never touches the real system crontab --"

# Sanity check: the crontab-writing migrations actually ran and exercised
# the stub (proves this is a meaningful regression test, not a no-op because
# the crontab code paths were skipped for some unrelated reason).
assert_file_contains "Migration 28 (LOBSTER-LOG-EXPORT) wrote to the fake crontab stub" \
    "$FAKE_CRONTAB_STATE" "LOBSTER-LOG-EXPORT"
assert_file_contains "Migration 52 (LOBSTER-GHOST-DETECTOR) wrote to the fake crontab stub" \
    "$FAKE_CRONTAB_STATE" "LOBSTER-GHOST-DETECTOR"
assert_file_contains "Migration 55 (systemd-timer-from-jobs.json) exercised the sudo tee stub" \
    "$FAKE_SUDO_TEE_LOG" "/etc/systemd/system/lobster-test-follow-up-job.timer"

if [ -n "$REAL_CRONTAB_BIN" ]; then
    REAL_CRONTAB_AFTER="$("$REAL_CRONTAB_BIN" -l 2>/dev/null || true)"
    if [ "$REAL_CRONTAB_BEFORE" = "$REAL_CRONTAB_AFTER" ]; then
        pass "Real system crontab is byte-for-byte unchanged after run_migrations()"
    else
        fail "Real system crontab is byte-for-byte unchanged after run_migrations()" \
            "real crontab changed during the test run - the crontab stub did not intercept every call"
    fi
else
    pass "Real system crontab is byte-for-byte unchanged after run_migrations() (skipped: no crontab binary on this host)"
fi

#===============================================================================
# Group 2: install.sh actually wires the runner in (the real bug fix)
#===============================================================================
echo ""
echo "-- install.sh calls run_migrations() unconditionally --"

INSTALL_SH="$REPO_ROOT/install.sh"
UPGRADE_SH="$REPO_ROOT/scripts/upgrade.sh"

assert_file_contains "install.sh sources scripts/lib/migrations.sh" \
    "$INSTALL_SH" 'source "${INSTALL_DIR}/scripts/lib/migrations.sh"'
assert_file_contains "install.sh calls run_migrations" \
    "$INSTALL_SH" "run_migrations"
assert_file_contains "upgrade.sh sources scripts/lib/migrations.sh" \
    "$UPGRADE_SH" 'source "$LOBSTER_DIR/scripts/lib/migrations.sh"'
assert_file_contains "upgrade.sh calls run_migrations" \
    "$UPGRADE_SH" "run_migrations"

# Guard against regressing back to a duplicated/local definition in either
# caller - the whole point of #2200's fix is a single shared implementation.
if grep -q "^run_migrations() {" "$INSTALL_SH" 2>/dev/null; then
    fail "install.sh does not redefine run_migrations() locally" "found a local definition - should source the shared lib instead"
else
    pass "install.sh does not redefine run_migrations() locally"
fi
if grep -q "^run_migrations() {" "$UPGRADE_SH" 2>/dev/null; then
    fail "upgrade.sh does not redefine run_migrations() locally" "found a local definition - should source the shared lib instead"
else
    pass "upgrade.sh does not redefine run_migrations() locally"
fi

#===============================================================================
# Summary
#===============================================================================
echo ""
echo -e "${BOLD}Results: $PASS/$TOTAL passed${NC}"
if [ "$FAIL" -gt 0 ]; then
    echo -e "${RED}$FAIL test(s) failed${NC}"
    exit 1
fi
echo -e "${GREEN}All tests passed${NC}"
exit 0
