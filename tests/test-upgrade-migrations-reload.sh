#!/bin/bash
#===============================================================================
# Test Suite: upgrade.sh runs the migrations.sh it just pulled, not the one
#             that was on disk when the run started.
#
# The gap this guards against:
#   scripts/upgrade.sh sourced scripts/lib/migrations.sh at top level, i.e.
#   before main() -- and therefore before git_pull() had fetched anything.
#   run_migrations() was already resolved in memory from the PRE-pull copy of
#   the library, so a run that pulled in a brand-new migration (or a fix TO an
#   existing one) still executed the stale definition for the remainder of that
#   run. The new code only took effect on the NEXT invocation of upgrade.sh --
#   which, for a user who runs "lobster update" once and walks away, means the
#   migration silently never ran at all.
#
#   Fix: source the library from inside main(), after git_pull() returns, so
#   the definition in memory is always the one from the code this run just
#   installed.
#
# Part A: structural -- the source statement lives inside main(), after the
#         git_pull call, and NOT at top level.
# Part B: behavioural -- a hermetic fake install whose on-disk migrations.sh
#         announces itself as OLD and whose origin/main announces itself as
#         NEW. The real scripts/upgrade.sh is run against it with every step
#         except git_pull stubbed out. run_migrations must print NEW.
#         Against the pre-fix script this part fails, printing OLD.
#
# Usage: bash tests/test-upgrade-migrations-reload.sh
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

begin_test() { TOTAL=$((TOTAL + 1)); test_name="$1"; }
pass() { PASS=$((PASS + 1)); echo -e "  ${GREEN}PASS${NC} $test_name"; }
fail() { FAIL=$((FAIL + 1)); echo -e "  ${RED}FAIL${NC} $test_name: $1"; }

assert_contains() {
    local haystack="$1" needle="$2"
    case "$haystack" in
        *"$needle"*) pass ;;
        *) fail "expected to find '$needle'" ;;
    esac
}

assert_not_contains() {
    local haystack="$1" needle="$2"
    case "$haystack" in
        *"$needle"*) fail "did not expect to find '$needle'" ;;
        *) pass ;;
    esac
}

assert_eq() {
    local actual="$1" expected="$2"
    if [[ "$actual" == "$expected" ]]; then pass; else fail "expected '$expected', got '$actual'"; fi
}

echo "=== upgrade.sh re-sources migrations.sh after git_pull ==="
echo ""

# ===================================================================
# Part A: structural placement of the source statement
# ===================================================================
echo "--- Part A: source statement placement ---"

# Line number of the source statement, of "main() {", and of the git_pull call
# inside main. Bare greps are enough here: the file has exactly one of each.
SOURCE_LINE=$(grep -n 'source "\$LOBSTER_DIR/scripts/lib/migrations.sh"' "$UPGRADE_SH" | cut -d: -f1)
MAIN_LINE=$(grep -n '^main() {' "$UPGRADE_SH" | cut -d: -f1)
GIT_PULL_CALL_LINE=$(grep -n '^ *git_pull  *#' "$UPGRADE_SH" | cut -d: -f1)
RUN_MIGRATIONS_CALL_LINE=$(grep -n '^ *run_migrations  *#' "$UPGRADE_SH" | cut -d: -f1)

begin_test "upgrade.sh sources scripts/lib/migrations.sh exactly once"
assert_eq "$(grep -c 'source "\$LOBSTER_DIR/scripts/lib/migrations.sh"' "$UPGRADE_SH")" "1"

begin_test "the source statement is inside main(), not at top level"
if [[ -n "$SOURCE_LINE" && -n "$MAIN_LINE" && "$SOURCE_LINE" -gt "$MAIN_LINE" ]]; then
    pass
else
    fail "source at line ${SOURCE_LINE:-none}, main() at line ${MAIN_LINE:-none}"
fi

begin_test "the source statement runs after git_pull"
if [[ -n "$SOURCE_LINE" && -n "$GIT_PULL_CALL_LINE" && "$SOURCE_LINE" -gt "$GIT_PULL_CALL_LINE" ]]; then
    pass
else
    fail "source at line ${SOURCE_LINE:-none}, git_pull call at line ${GIT_PULL_CALL_LINE:-none}"
fi

begin_test "the source statement runs before run_migrations"
if [[ -n "$SOURCE_LINE" && -n "$RUN_MIGRATIONS_CALL_LINE" && "$SOURCE_LINE" -lt "$RUN_MIGRATIONS_CALL_LINE" ]]; then
    pass
else
    fail "source at line ${SOURCE_LINE:-none}, run_migrations call at line ${RUN_MIGRATIONS_CALL_LINE:-none}"
fi

begin_test "upgrade.sh still passes bash -n"
if bash -n "$UPGRADE_SH" 2>/dev/null; then pass; else fail "syntax error"; fi

echo ""

# ===================================================================
# Part B: behavioural -- the pulled library is the one that executes
# ===================================================================
echo "--- Part B: a run that pulls a new migrations.sh executes the new one ---"

FIXTURE=""
cleanup_fixture() { [ -n "$FIXTURE" ] && rm -rf "$FIXTURE"; FIXTURE=""; }
trap cleanup_fixture EXIT

FIXTURE=""
UPSTREAM=""
FAKE_LOBSTER=""
FAKE_HOME=""

# A migrations library that announces which copy of itself is running. Passing
# the marker "BROKEN" instead writes one that does not parse, to exercise the
# failure path of the post-pull load.
write_lib() {
    local path="$1" marker="$2"
    if [ "$marker" = "BROKEN" ]; then
        cat > "$path" <<'LIB'
#!/bin/bash
run_migrations() { echo "MIGRATIONS_LIB=BROKEN"
# deliberately unterminated function body
LIB
        return
    fi
    cat > "$path" <<LIB
#!/bin/bash
run_migrations() { echo "MIGRATIONS_LIB=$marker"; }
LIB
}

# The real script under test -- copied verbatim, then given stub definitions
# that override every step except git_pull. The stubs are appended AFTER the
# real definitions but BEFORE the final `main "$@"`, so main() itself, its
# step ordering, and the placement of the source statement are all the real
# thing.
build_harness() {
    local dest="$1"
    head -n -1 "$UPGRADE_SH" > "$dest"
    cat >> "$dest" <<'STUBS'
# ---- test stubs (appended by tests/test-upgrade-migrations-reload.sh) ----
acquire_lock()           { :; }
cleanup_lock()           { :; }
preflight_checks()       { INSTALL_MODE="git"; PREVIOUS_COMMIT=$(git -C "$LOBSTER_DIR" rev-parse --short HEAD); }
backup_config()          { :; }
show_whats_new()         { :; }
update_python_deps()     { :; }
create_new_directories() { :; }
setup_syncthing()        { :; }
install_playwright()     { :; }
update_systemd_services(){ :; }
health_check()           { :; }
restart_services()       { :; }
STUBS
    echo 'main "$@"' >> "$dest"
}

git() { command git -c user.email=t@t -c user.name=t -c init.defaultBranch=main "$@"; }

# Build a fake install that is exactly one fast-forward behind an origin/main
# carrying $1 as its migrations library, then run the real upgrade.sh against
# it. Sets OUTPUT and RUN_EXIT.
run_scenario() {
    local upstream_marker="$1"

    cleanup_fixture
    FIXTURE=$(mktemp -d)
    UPSTREAM="$FIXTURE/upstream"
    FAKE_LOBSTER="$FIXTURE/lobster"
    FAKE_HOME="$FIXTURE/home"
    mkdir -p "$UPSTREAM/scripts/lib" "$FAKE_HOME"

    # health-check-v3.sh is syntax-checked by git_pull() before it returns.
    cat > "$UPSTREAM/scripts/health-check-v3.sh" <<'HC'
#!/bin/bash
: # stub
HC

    # Upstream commit 1: the OLD library. The local install is cloned from
    # here, so it starts out holding OLD on a clean tree.
    write_lib "$UPSTREAM/scripts/lib/migrations.sh" "OLD"
    git -C "$UPSTREAM" init --quiet -b main
    git -C "$UPSTREAM" add -A
    git -C "$UPSTREAM" commit --quiet -m "old migrations lib"
    git clone --quiet "$UPSTREAM" "$FAKE_LOBSTER"

    # Upstream commit 2: the library under test. The local install is now one
    # fast-forward behind, and that fast-forward is what rewrites the file
    # mid-run -- the situation the fix is about.
    write_lib "$UPSTREAM/scripts/lib/migrations.sh" "$upstream_marker"
    git -C "$UPSTREAM" commit --quiet -am "upstream migrations lib: $upstream_marker"

    local harness="$FIXTURE/upgrade-harness.sh"
    build_harness "$harness"

    OUTPUT=$(
        cd "$FAKE_LOBSTER" && \
        HOME="$FAKE_HOME" \
        LOBSTER_INSTALL_DIR="$FAKE_LOBSTER" \
        LOBSTER_WORKSPACE="$FAKE_HOME/workspace" \
        LOBSTER_MESSAGES="$FAKE_HOME/messages" \
        LOBSTER_CONFIG_DIR="$FAKE_HOME/config" \
        LOBSTER_USER_CONFIG="$FAKE_HOME/user-config" \
        bash "$harness" --skip-syncthing --skip-playwright 2>&1
    )
    RUN_EXIT=$?
}

run_scenario "NEW"

begin_test "harness run exits 0"
assert_eq "$RUN_EXIT" "0"

begin_test "the pulled (NEW) migrations library is the one that ran"
assert_contains "$OUTPUT" "MIGRATIONS_LIB=NEW"

begin_test "the stale (OLD) migrations library did not run"
assert_not_contains "$OUTPUT" "MIGRATIONS_LIB=OLD"

begin_test "the pull actually replaced the library on disk"
assert_contains "$(cat "$FAKE_LOBSTER/scripts/lib/migrations.sh")" "MIGRATIONS_LIB=NEW"

if [ "$FAIL" -gt 0 ]; then
    echo ""
    echo "--- harness output ---"
    echo "$OUTPUT"
fi

echo ""

# ===================================================================
# Part C: a broken pulled library fails loudly, by name
#
# Loading the library is now a fallible mid-run step rather than something
# that happened before the banner printed, so it has to fail the way the
# script's other fallible steps do: a named cause and a non-zero exit, not a
# bare bash parse error from somewhere inside main().
# ===================================================================
echo "--- Part C: a pulled library that does not parse aborts the upgrade ---"

PART_C_FAIL_BEFORE=$FAIL
run_scenario "BROKEN"

begin_test "a broken pulled library makes the run exit non-zero"
if [ "$RUN_EXIT" -ne 0 ]; then pass; else fail "exited 0"; fi

# Without the explicit guard this still aborts, but only via bash's own parse
# error -- which is why the assertion is on the script's diagnostic, not merely
# on the library's filename appearing somewhere in the output.
begin_test "the failure is reported as a named diagnostic, not a bare parse error"
assert_contains "$OUTPUT" "Migration library failed syntax check"

begin_test "the diagnostic points at the migration library path"
assert_contains "$OUTPUT" "scripts/lib/migrations.sh"

begin_test "the upgrade does not report success"
assert_not_contains "$OUTPUT" "UPGRADE COMPLETE"

if [ "$FAIL" -gt "$PART_C_FAIL_BEFORE" ]; then
    echo ""
    echo "--- harness output ---"
    echo "$OUTPUT"
fi

cleanup_fixture

echo ""
echo "=== Results: $PASS/$TOTAL passed ==="
if [ "$FAIL" -gt 0 ]; then
    echo -e "${RED}$FAIL test(s) failed${NC}"
    exit 1
fi
echo -e "${GREEN}All tests passed${NC}"
exit 0
