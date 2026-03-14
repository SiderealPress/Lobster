#!/usr/bin/env bash
# =============================================================================
# security-scan-lib.sh
# Shared helper functions for the pre-push security scanner.
#
# Sourced by:
#   - scripts/pre-push-security-scan.sh  (the hook itself)
#   - tests/test-security-scan.sh        (the test suite)
#
# Keeping the logic here ensures that tests always exercise the real
# production code rather than a hand-maintained copy.
# =============================================================================

# Returns 0 (should skip) or 1 (should scan) for a given file path.
# Skips:
#   - Test directories: any path starting with tests/, test/, spec/, or
#     containing /test/ anywhere in the path
#   - Documentation: .md, .mdx, .rst, .txt, .adoc files
#   - Known binary/generated extensions: lock files, compiled artifacts, images
should_skip_file() {
    local filepath="$1"

    # Skip test directories — fake/mock values are intentional there.
    # Pattern: path starts with tests/, test/, spec/, or contains /test anywhere.
    case "$filepath" in
        tests/*|test/*|spec/*)
            return 0
            ;;
    esac
    if [[ "$filepath" == */test/* || "$filepath" == */tests/* || "$filepath" == */spec/* ]]; then
        return 0
    fi

    # Skip documentation files — these use example/placeholder values by design.
    case "$filepath" in
        *.md|*.mdx|*.rst|*.txt|*.adoc)
            return 0
            ;;
    esac

    # Skip known binary/generated extensions and lock files.
    case "$filepath" in
        *.lock|*.min.js|*.min.css|*.map|*.png|*.jpg|*.jpeg|*.gif|*.ico| \
        *.woff|*.woff2|*.ttf|*.eot|*.svg|*.pdf|*.zip|*.tar|*.gz|*.bz2| \
        *.exe|*.dll|*.so|*.dylib|*.pyc|*.pyo|*.class|*.o|*.a| \
        package-lock.json|yarn.lock|Cargo.lock|go.sum|poetry.lock|Gemfile.lock| \
        *.pb.go|*_generated.*|*.gen.*|vendor/*|node_modules/*)
            return 0
            ;;
    esac

    return 1
}
