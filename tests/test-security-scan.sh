#!/usr/bin/env bash
# =============================================================================
# test-security-scan.sh
# Unit-style tests for the pre-push-security-scan.sh hook logic.
#
# Tests the should_skip_file and false-positive filter functions in isolation
# without requiring a git repo or actual push.
#
# Usage: bash tests/test-security-scan.sh
# Exit code: 0 = all pass, 1 = one or more failures
# =============================================================================

set -euo pipefail

PASS=0
FAIL=0
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
HOOK="$REPO_ROOT/scripts/pre-push-security-scan.sh"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

ok() {
    local desc="$1"
    echo "  PASS: $desc"
    ((PASS++)) || true
}

fail() {
    local desc="$1"
    echo "  FAIL: $desc"
    ((FAIL++)) || true
}

assert_skipped() {
    local desc="$1"
    local filepath="$2"
    if (source_should_skip_file "$filepath"); then
        ok "$desc"
    else
        fail "$desc — expected skip, but got scan"
    fi
}

assert_scanned() {
    local desc="$1"
    local filepath="$2"
    if ! (source_should_skip_file "$filepath"); then
        ok "$desc"
    else
        fail "$desc — expected scan, but got skip"
    fi
}

assert_line_ignored() {
    local desc="$1"
    local line="$2"
    # Run the pattern check — if it would be caught, the test fails
    # We check the false-positive filter: if it matches the filter, the line is safe
    if echo "$line" | grep -qiP -- "(?:example|placeholder|your[_-]?\w*[_-]?here|your[_-]?(?:key|token|secret)|xxx|dummy|fake|test[_-]?key|CHANGEME|TODO|FIXME|<REDACTED)" 2>/dev/null; then
        ok "$desc (fake filter matches)"
    else
        fail "$desc — fake filter did NOT match '$line'"
    fi
}

assert_line_detected() {
    local desc="$1"
    local line="$2"
    local pattern="$3"
    if echo "$line" | grep -qP -- "$pattern" 2>/dev/null; then
        ok "$desc"
    else
        fail "$desc — pattern '$pattern' did NOT match '$line'"
    fi
}

# Source should_skip_file from the shared library so tests always exercise the
# real production logic. Any changes to the function in security-scan-lib.sh
# are automatically picked up here — no manual sync required.
# shellcheck source=../scripts/security-scan-lib.sh
source "$REPO_ROOT/scripts/security-scan-lib.sh"

source_should_skip_file() {
    should_skip_file "$@"
}

# ---------------------------------------------------------------------------
# Test suite: should_skip_file
# ---------------------------------------------------------------------------

echo ""
echo "=== should_skip_file: test directory exclusion ==="

assert_skipped "tests/ prefix" "tests/unit/test_google_calendar_client.py"
assert_skipped "tests/ nested" "tests/unit/test_integrations/test_google_calendar_callback_server.py"
assert_skipped "test/ prefix" "test/api_test.py"
assert_skipped "spec/ prefix" "spec/models/user_spec.rb"
assert_skipped "path with /test/" "src/integrations/test/mock_client.py"
assert_skipped "path with /tests/" "src/integrations/tests/mock_client.py"

echo ""
echo "=== should_skip_file: documentation file exclusion ==="

assert_skipped "root .md file" "README.md"
assert_skipped "agents .md file" "agents/eloso-deployer.md"
assert_skipped "docs .md file" "docs/google-calendar-setup.md"
assert_skipped "nested .md file" ".claude/agents/functional-engineer.md"
assert_skipped ".mdx file" "docs/guide.mdx"
assert_skipped ".rst file" "docs/api.rst"
assert_skipped ".txt file" "docs/notes.txt"
assert_skipped ".adoc file" "docs/readme.adoc"

echo ""
echo "=== should_skip_file: binary extension exclusion ==="

assert_skipped ".png image" "assets/logo.png"
assert_skipped ".so library" "lib/libsomething.so"
assert_skipped ".dylib library" "lib/libsomething.dylib"
assert_skipped ".exe binary" "bin/app.exe"
assert_skipped ".pyc compiled" "src/__pycache__/foo.pyc"
assert_skipped "poetry.lock" "poetry.lock"
assert_skipped "go.sum" "go.sum"

echo ""
echo "=== should_skip_file: source files should be scanned ==="

assert_scanned "Python source" "src/bot/lobster_bot.py"
assert_scanned "shell script" "scripts/pre-push-security-scan.sh"
assert_scanned "JSON config" "config/config.env.example"
assert_scanned "TOML file" "config/owner.toml.example"
assert_scanned "TypeScript file" "src/frontend/app.ts"

# ---------------------------------------------------------------------------
# Test suite: false-positive line filter
# ---------------------------------------------------------------------------

echo ""
echo "=== false-positive filter: fake/test tokens should be ignored ==="

assert_line_ignored \
    "ya29.fake access token" \
    "_FAKE_ACCESS_TOKEN = \"ya29.fake-access-token\""

assert_line_ignored \
    "fake-client-secret" \
    "_FAKE_CLIENT_SECRET = \"fake-client-secret\""

assert_line_ignored \
    "1//fake-refresh-token" \
    "    refresh_token=\"1//fake-refresh-token\","

assert_line_ignored \
    "your-token-here placeholder" \
    "TELEGRAM_BOT_TOKEN=your-token-here"

assert_line_ignored \
    "your_api_key_here placeholder" \
    "api_key = \"your_api_key_here\""

assert_line_ignored \
    "CHANGEME placeholder" \
    "password = \"CHANGEME\""

assert_line_ignored \
    "example keyword" \
    "token = \"sk-example-key-for-docs\""

assert_line_ignored \
    "<REDACTED_SECRET> self-match prevention" \
    "access_token = \"<REDACTED_SECRET>\""

echo ""
echo "=== pattern detection: real secrets should be caught ==="
# These test strings are intentionally fake keys used ONLY to verify that
# the scanner's regex patterns match their intended format.
# They are safe because: (a) this file is in tests/ (excluded from scanning),
# (b) these values are not valid credentials for any real service, and
# (c) they contain deliberate structural markers (TEST_, _LOBSTER_) to make
# their purpose unambiguous.

assert_line_detected \
    "AKIA AWS key" \
    "AWS_ACCESS_KEY_ID=AKIATEST1234567890AB" \
    "AKIA[0-9A-Z]{16}"

assert_line_detected \
    "Anthropic sk-ant- key" \
    "ANTHROPIC_API_KEY=sk-ant-LOBSTER-TEST-NOT-REAL-XXXXXXXXXXXXXXXXXXXXXXXXXX" \
    "sk-ant-[a-zA-Z0-9_-]{20,}"

assert_line_detected \
    "GitHub PAT ghp_" \
    "GITHUB_TOKEN=ghp_LOBSTERTEST1234567890ABCDEFGHIJKLMNOPQRS" \
    "ghp_[A-Za-z0-9]{36,}"

assert_line_detected \
    "RSA private key header" \
    "-----BEGIN RSA PRIVATE KEY-----" \
    "-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"

assert_line_detected \
    "Slack token xoxb-" \
    "SLACK_TOKEN=xoxb-LOBSTER-TEST-NOT-A-REAL-SLACK-TOKEN" \
    "xox[bpsorta]-[0-9a-zA-Z-]{10,}"

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
