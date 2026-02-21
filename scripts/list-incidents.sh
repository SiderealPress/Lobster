#!/bin/bash
#===============================================================================
# List Lobster Incident Reports
#
# Usage:
#   ~/lobster/scripts/list-incidents.sh          # List all incidents
#   ~/lobster/scripts/list-incidents.sh --last 5  # Last 5 incidents
#   ~/lobster/scripts/list-incidents.sh --today   # Today's incidents
#===============================================================================

INCIDENT_DIR="${INCIDENT_DIR:-${LOBSTER_INSTALL_DIR:-$HOME/lobster}/incidents}"

if [[ ! -d "$INCIDENT_DIR" ]]; then
    echo "No incidents directory found at $INCIDENT_DIR"
    exit 0
fi

count=$(find "$INCIDENT_DIR" -maxdepth 1 -name "*.md" 2>/dev/null | wc -l)
if [[ $count -eq 0 ]]; then
    echo "No incident reports found."
    exit 0
fi

limit=""
date_filter=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --last)
            limit="$2"
            shift 2
            ;;
        --today)
            date_filter=$(date -u '+%Y-%m-%d')
            shift
            ;;
        *)
            shift
            ;;
    esac
done

echo "=== Lobster Incident Reports ($INCIDENT_DIR) ==="
echo ""
printf "%-40s  %-20s  %s\n" "File" "Timestamp" "Reason"
printf "%-40s  %-20s  %s\n" "----" "---------" "------"

files=$(ls -1t "$INCIDENT_DIR"/*.md 2>/dev/null)

if [[ -n "$date_filter" ]]; then
    files=$(echo "$files" | grep "$date_filter")
fi

if [[ -n "$limit" ]]; then
    files=$(echo "$files" | head -n "$limit")
fi

for f in $files; do
    basename_f=$(basename "$f")
    # Extract timestamp from filename: YYYY-MM-DD_HHMMSS_slug.md
    ts=$(echo "$basename_f" | sed 's/_/ /' | sed 's/_/ /' | cut -d' ' -f1-2 | sed 's/ / /')
    # Extract reason from the report
    reason=$(grep "Alert Reason" "$f" | head -1 | sed 's/.*| //' | sed 's/ |$//')
    printf "%-40s  %-20s  %s\n" "$basename_f" "$ts" "$reason"
done

echo ""
echo "Total: $count incident report(s)"
