#!/bin/bash
#===============================================================================
# Test Suite: upgrade.sh syncs the venv from uv.lock, not from a floating
#             hand-maintained pip list.
#
# The gap this guards against:
#   update_python_deps() refreshed the venv with
#       pip install --upgrade mcp python-telegram-bot watchdog python-dotenv
#       pip install --upgrade playwright
#   so (1) every package floated to whatever was newest on PyPI, against the
#   exact-pin policy in DEPENDENCY_NOTES.md, and (2) optional extras declared
#   in pyproject.toml (e.g. `slack` -> slack-sdk/slack-bolt) were never
#   installed, so a venv rebuilt during an upgrade could lose them.
#
#   Fix: `uv sync --all-extras --frozen` against pyproject.toml/uv.lock. If
#   uv is absent, or the frozen sync fails, fall back to pip with exact pins
#   that match uv.lock. Never fall back to an unfrozen `uv sync`, which would
#   re-resolve the lockfile.
#
# The real update_python_deps() runs with `uv`, `pip` and `python3` replaced
# by fakes on PATH that record their argv. upgrade.sh is loaded in library
# mode the same way tests/test-upgrade-migrations-reload.sh does it: the
# final `main "$@"` line is stripped, so no step of the real upgrade runs.
#
# Usage: bash tests/test-upgrade-python-deps.sh
#===============================================================================

set -u

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

PASS=0
FAIL=0
TOTAL=0
test_name=""

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UPGRADE_SH="$REPO_ROOT/scripts/upgrade.sh"
UV_LOCK="$REPO_ROOT/uv.lock"

# Packages the pip fallback must cover: the core runtime set plus the
# optional `slack` and `browser` extras.
FALLBACK_PACKAGES=(mcp python-telegram-bot watchdog python-dotenv slack-sdk slack-bolt playwright)

begin_test() { TOTAL=$((TOTAL + 1)); test_name="$1"; }
pass() { PASS=$((PASS + 1)); echo -e "  ${GREEN}PASS${NC} $test_name"; }
fail() { FAIL=$((FAIL + 1)); echo -e "  ${RED}FAIL${NC} $test_name: $1"; }

assert_contains() {
    local haystack="$1" needle="$2"
    case "$haystack" in
        *"$needle"*) pass ;;
        *) fail "expected to find '$needle' in: ${haystack:-<empty>}" ;;
    esac
}

assert_not_contains() {
    local haystack="$1" needle="$2"
    case "$haystack" in
        *"$needle"*) fail "did not expect to find '$needle' in: $haystack" ;;
        *) pass ;;
    esac
}

assert_eq() {
    local actual="$1" expected="$2"
    if [[ "$actual" == "$expected" ]]; then pass; else fail "expected '$expected', got '$actual'"; fi
}

# Version pinned for a package in uv.lock (empty if absent).
lock_version() {
    grep -A1 "^name = \"$1\"$" "$UV_LOCK" | sed -n 's/^version = "\(.*\)"$/\1/p' | head -1
}

echo "=== upgrade.sh update_python_deps: uv sync --all-extras --frozen ==="
echo ""

begin_test "upgrade.sh passes bash -n"
if bash -n "$UPGRADE_SH" 2>/dev/null; then pass; else fail "syntax error"; fi

# Library-mode load requires the dispatch line to be the last line; refuse to
# run anything otherwise (sourcing the full script would run a real upgrade).
begin_test "upgrade.sh ends with the main dispatch line (library-mode load is safe)"
LAST_LINE=$(tail -n 1 "$UPGRADE_SH")
if [[ "$LAST_LINE" == 'main "$@"' ]]; then
    pass
else
    fail "last line is '$LAST_LINE'; aborting before sourcing anything"
    echo "=== Results: $PASS/$TOTAL passed ==="
    exit 1
fi

FIXTURE=$(mktemp -d)
trap 'rm -rf "$FIXTURE"' EXIT
LIB="$FIXTURE/upgrade-lib.sh"
head -n -1 "$UPGRADE_SH" > "$LIB"

# ---------------------------------------------------------------------------
# Fakes. Each records one line per invocation: its argv, space-joined.
#   uv      exit status from FAKE_UV_EXIT (default 0)
#   pip     always succeeds
#   python3 `-m venv DIR` creates a minimal venv layout; nothing else
# ---------------------------------------------------------------------------
STUB_BIN="$FIXTURE/stub-bin"
NO_UV_BIN="$FIXTURE/stub-bin-no-uv"
mkdir -p "$STUB_BIN" "$NO_UV_BIN"

cat > "$STUB_BIN/uv" <<'EOF'
#!/bin/bash
echo "$*" >> "$CALL_LOG_DIR/uv"
echo "fake uv: simulated failure output" >&2
exit "${FAKE_UV_EXIT:-0}"
EOF

cat > "$STUB_BIN/pip" <<'EOF'
#!/bin/bash
echo "$*" >> "$CALL_LOG_DIR/pip"
exit 0
EOF

cat > "$STUB_BIN/python3" <<'EOF'
#!/bin/bash
echo "$*" >> "$CALL_LOG_DIR/python3"
if [ "${1:-}" = "-m" ] && [ "${2:-}" = "venv" ] && [ -n "${3:-}" ]; then
    mkdir -p "$3/bin"
    printf 'deactivate() { :; }\n' > "$3/bin/activate"
    : > "$3/bin/python"
fi
exit 0
EOF
chmod +x "$STUB_BIN/uv" "$STUB_BIN/pip" "$STUB_BIN/python3"
cp "$STUB_BIN/pip" "$STUB_BIN/python3" "$NO_UV_BIN/"

# Run the real update_python_deps() in a subshell against a fresh fake install.
#   $1  stub dir to put first on PATH (with or without uv)
#   $2  "venv" to pre-create the venv, "novenv" to leave it missing
#   remaining args: VAR=value assignments applied after the library loads
#                   (DRY_RUN=true, SKIP_PLAYWRIGHT=true)
# Sets UV_CALLS, PIP_CALLS, PY_CALLS, OUTPUT, RUN_EXIT, FAKE_LOBSTER.
run_update() {
    local stub_dir="$1" venv_mode="$2"
    shift 2
    local case_dir
    case_dir=$(mktemp -d "$FIXTURE/case.XXXXXX")
    FAKE_LOBSTER="$case_dir/lobster"
    mkdir -p "$FAKE_LOBSTER" "$case_dir/home" "$case_dir/calls"
    if [ "$venv_mode" = "venv" ]; then
        mkdir -p "$FAKE_LOBSTER/.venv/bin"
        printf 'deactivate() { :; }\n' > "$FAKE_LOBSTER/.venv/bin/activate"
        : > "$FAKE_LOBSTER/.venv/bin/python"
    fi

    OUTPUT=$(
        export CALL_LOG_DIR="$case_dir/calls"
        export HOME="$case_dir/home"
        export LOBSTER_INSTALL_DIR="$FAKE_LOBSTER"
        export LOBSTER_WORKSPACE="$case_dir/home/workspace"
        export LOBSTER_MESSAGES="$case_dir/home/messages"
        export LOBSTER_CONFIG_DIR="$case_dir/home/config"
        export LOBSTER_USER_CONFIG="$case_dir/home/user-config"
        export PATH="$stub_dir:/usr/bin:/bin"
        # shellcheck source=/dev/null
        source "$LIB"
        for assignment in "$@"; do
            eval "$assignment"
        done
        update_python_deps 2>&1
    )
    RUN_EXIT=$?
    UV_CALLS=$(cat "$case_dir/calls/uv" 2>/dev/null || true)
    PIP_CALLS=$(cat "$case_dir/calls/pip" 2>/dev/null | tr '\n' ' ' || true)
    PIP_CALLS="${PIP_CALLS% }"
    PY_CALLS=$(cat "$case_dir/calls/python3" 2>/dev/null || true)
}

dump_on_fail() {
    local before="$1"
    if [ "$FAIL" -gt "$before" ]; then
        echo "    --- uv calls ---";  echo "${UV_CALLS:-<none>}"  | sed 's/^/    /'
        echo "    --- pip calls ---"; echo "${PIP_CALLS:-<none>}" | sed 's/^/    /'
        echo "    --- output ---";    echo "$OUTPUT" | tail -n 15 | sed 's/^/    /'
    fi
}

# ===================================================================
# (a) uv present and succeeds: one frozen all-extras sync, no pip
# ===================================================================
echo "--- (a) uv present: frozen all-extras sync, pip never runs ---"
F0=$FAIL
run_update "$STUB_BIN" venv

begin_test "update_python_deps exits 0"
assert_eq "$RUN_EXIT" "0"

begin_test "uv is called with sync --all-extras --frozen"
assert_contains "$UV_CALLS" "sync --all-extras --frozen"

begin_test "uv sync targets the install's venv interpreter"
assert_contains "$UV_CALLS" "--python $FAKE_LOBSTER/.venv/bin/python"

begin_test "uv is called exactly once"
assert_eq "$(printf '%s' "$UV_CALLS" | grep -c . || true)" "1"

# An exact sync would uninstall anything uv.lock does not list: packages that
# skill installers put into the venv, or playwright on a --skip-playwright run.
# The upgrade must only add/realign pinned packages, never remove others.
begin_test "uv sync never removes packages it does not manage (--inexact)"
assert_contains "$UV_CALLS" "--inexact"

begin_test "pip is never called"
assert_eq "${PIP_CALLS:-<none>}" "<none>"

begin_test "the browser extra is not excluded by default"
assert_not_contains "$UV_CALLS" "--no-extra browser"
dump_on_fail "$F0"
echo ""

# ===================================================================
# (b) uv present but the frozen sync fails: pinned pip, no unfrozen retry
# ===================================================================
echo "--- (b) frozen uv sync fails: pinned pip fallback, no unfrozen uv sync ---"
F0=$FAIL
export FAKE_UV_EXIT=1
run_update "$STUB_BIN" venv
export FAKE_UV_EXIT=0

begin_test "update_python_deps still exits 0 (fallback is non-fatal)"
assert_eq "$RUN_EXIT" "0"

begin_test "uv is called exactly once (no second sync attempt)"
assert_eq "$(printf '%s' "$UV_CALLS" | grep -c . || true)" "1"

begin_test "every uv invocation carries --frozen"
assert_eq "$(printf '%s\n' "$UV_CALLS" | grep -v -e '^$' -e '--frozen' | wc -l | tr -d ' ')" "0"

begin_test "the failed sync's output is surfaced"
assert_contains "$OUTPUT" "fake uv: simulated failure output"

begin_test "a warning is emitted for the frozen-sync failure"
assert_contains "$OUTPUT" "[WARN]"

for pkg in "${FALLBACK_PACKAGES[@]}"; do
    begin_test "pip fallback installs $pkg pinned to uv.lock ($pkg==$(lock_version "$pkg"))"
    assert_contains " $PIP_CALLS " " $pkg==$(lock_version "$pkg") "
done

begin_test "pip fallback never uses --upgrade (would float versions)"
assert_not_contains "$PIP_CALLS" "--upgrade"
dump_on_fail "$F0"
echo ""

# ===================================================================
# (c) uv absent: pinned pip path runs
# ===================================================================
echo "--- (c) uv absent: pinned pip fallback ---"
F0=$FAIL
run_update "$NO_UV_BIN" venv

begin_test "update_python_deps exits 0"
assert_eq "$RUN_EXIT" "0"

begin_test "uv was not on PATH (sanity)"
assert_eq "${UV_CALLS:-<none>}" "<none>"

for pkg in "${FALLBACK_PACKAGES[@]}"; do
    begin_test "pip installs $pkg pinned to uv.lock ($pkg==$(lock_version "$pkg"))"
    assert_contains " $PIP_CALLS " " $pkg==$(lock_version "$pkg") "
done

begin_test "pip never uses --upgrade"
assert_not_contains "$PIP_CALLS" "--upgrade"
dump_on_fail "$F0"
echo ""

# ===================================================================
# (d) uv absent and venv missing: venv is created, then pinned pip runs
# ===================================================================
echo "--- (d) uv absent, venv missing: venv created, then pinned pip ---"
F0=$FAIL
run_update "$NO_UV_BIN" novenv

begin_test "update_python_deps exits 0"
assert_eq "$RUN_EXIT" "0"

begin_test "python3 -m venv creates the install's venv"
assert_contains "$PY_CALLS" "-m venv $FAKE_LOBSTER/.venv"

begin_test "pip installs the pinned set after venv creation"
assert_contains " $PIP_CALLS " " mcp==$(lock_version mcp) "
dump_on_fail "$F0"
echo ""

# ===================================================================
# (e) --skip-playwright and --dry-run keep their meaning
# ===================================================================
echo "--- (e) SKIP_PLAYWRIGHT and DRY_RUN ---"
F0=$FAIL
run_update "$STUB_BIN" venv SKIP_PLAYWRIGHT=true

begin_test "SKIP_PLAYWRIGHT with uv: frozen sync excludes the browser extra"
assert_contains "$UV_CALLS" "--no-extra browser"

begin_test "SKIP_PLAYWRIGHT with uv: still --all-extras --frozen"
assert_contains "$UV_CALLS" "sync --all-extras --frozen"

run_update "$NO_UV_BIN" venv SKIP_PLAYWRIGHT=true

begin_test "SKIP_PLAYWRIGHT without uv: playwright is not pip-installed"
assert_not_contains "$PIP_CALLS" "playwright"

begin_test "SKIP_PLAYWRIGHT without uv: slack extra still pip-installed"
assert_contains " $PIP_CALLS " " slack-bolt==$(lock_version slack-bolt) "

run_update "$STUB_BIN" venv DRY_RUN=true

begin_test "DRY_RUN: uv is not called"
assert_eq "${UV_CALLS:-<none>}" "<none>"

begin_test "DRY_RUN: pip is not called"
assert_eq "${PIP_CALLS:-<none>}" "<none>"
dump_on_fail "$F0"
echo ""

echo "=== Results: $PASS/$TOTAL passed ==="
if [ "$FAIL" -gt 0 ]; then
    echo -e "${RED}$FAIL test(s) failed${NC}"
    exit 1
fi
echo -e "${GREEN}All tests passed${NC}"
exit 0
