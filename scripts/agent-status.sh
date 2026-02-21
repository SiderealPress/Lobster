#!/bin/bash
#===============================================================================
# Agent Status Scanner
#
# Scans background agent output files and produces a concise status summary.
# Designed to be sourced by self-check scripts to include agent info in messages.
#
# Usage:
#   source agent-status.sh
#   summary=$(scan_agent_status)
#   # Returns: "Agents: abc123 (52 turns, last activity 30s ago), def456 (49 turns, stale 2h)"
#   # Returns: "" (empty string) if no agents found
#
# Environment:
#   AGENT_TASKS_DIR - Override the agent output directory (for testing)
#===============================================================================

# Staleness threshold: 15 minutes in seconds
AGENT_STALE_THRESHOLD=900

# Maximum agents to show in summary (keep messages concise)
AGENT_MAX_DISPLAY=5

# Format seconds into human-readable duration: 30s, 5m, 2h
_format_duration() {
    local seconds="$1"
    if [ "$seconds" -lt 60 ]; then
        echo "${seconds}s"
    elif [ "$seconds" -lt 3600 ]; then
        echo "$(( seconds / 60 ))m"
    else
        echo "$(( seconds / 3600 ))h"
    fi
}

# Scan agent output files and return a summary string.
# Returns empty string if no agents found.
scan_agent_status() {
    local tasks_dir="${AGENT_TASKS_DIR:-/tmp/claude-1000/-home-ec2-user-lobster-workspace/tasks}"

    # No directory or no .output files -> empty
    if [ ! -d "$tasks_dir" ]; then
        return 0
    fi

    local output_files=()
    while IFS= read -r -d '' f; do
        output_files+=("$f")
    done < <(find "$tasks_dir" -maxdepth 1 -name "*.output" -print0 2>/dev/null)

    if [ ${#output_files[@]} -eq 0 ]; then
        return 0
    fi

    local now
    now=$(date +%s)
    local entries=()
    local total_count=${#output_files[@]}

    # Sort by mtime descending (most recently active first) and take top N
    local sorted_files=()
    while IFS= read -r f; do
        sorted_files+=("$f")
    done < <(ls -t "${output_files[@]}" 2>/dev/null)

    local display_count=0
    for filepath in "${sorted_files[@]}"; do
        if [ "$display_count" -ge "$AGENT_MAX_DISPLAY" ]; then
            break
        fi

        local basename_f
        basename_f=$(basename "$filepath" .output)

        # Count assistant turns
        local turns
        turns=$(grep -c '"type":"assistant"' "$filepath" 2>/dev/null) || turns=0

        # Calculate age from mtime
        local file_mtime
        file_mtime=$(stat -c %Y "$filepath" 2>/dev/null || echo "$now")
        local age=$(( now - file_mtime ))

        # Format the entry
        local duration
        duration=$(_format_duration "$age")

        local status_text
        if [ "$age" -ge "$AGENT_STALE_THRESHOLD" ]; then
            status_text="stale ${duration}"
        else
            status_text="last activity ${duration} ago"
        fi

        entries+=("${basename_f} (${turns} turns, ${status_text})")
        display_count=$(( display_count + 1 ))
    done

    if [ ${#entries[@]} -eq 0 ]; then
        return 0
    fi

    # Join entries with ", "
    local result="Agents: "
    local first=true
    for entry in "${entries[@]}"; do
        if [ "$first" = true ]; then
            result+="$entry"
            first=false
        else
            result+=", $entry"
        fi
    done

    # Add "+N more" if we capped the display
    local remaining=$(( total_count - display_count ))
    if [ "$remaining" -gt 0 ]; then
        result+=", +${remaining} more"
    fi

    echo "$result"
}
