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
#   # Returns: "Agents: abc123 (52 turns, running), def456 (49 turns, done)"
#   # Returns: "" (empty string) if no agents found
#
#   completed=$(scan_completed_tasks)
#   # Returns: JSON-like summary of completed tasks not yet reported
#   # Returns: "" (empty string) if no newly completed tasks
#
# Environment:
#   AGENT_TASKS_DIR - Override the agent output directory (for testing)
#
# Key insight: Claude Code writes a stop_reason field to JSONL output files.
#   "end_turn"  = agent definitively finished
#   "tool_use"  = agent is actively running
# This is zero-cooperation, deterministic, and scans all agents in ~3ms.
#===============================================================================

# State directory for tracking reported completions
AGENT_STATE_DIR="${LOBSTER_INSTALL_DIR:-$HOME/lobster}/.state"

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

# Extract the stop_reason from the last 4KB of a JSONL file.
# Prints "end_turn", "tool_use", or "" (if not yet written / file empty).
_get_stop_reason() {
    local filepath="$1"

    # Resolve symlink to real JSONL path
    local real_path
    real_path=$(readlink -f "$filepath" 2>/dev/null)
    if [ -z "$real_path" ] || [ ! -f "$real_path" ]; then
        # Not a symlink — try the file directly
        real_path="$filepath"
    fi

    if [ ! -f "$real_path" ] || [ ! -s "$real_path" ]; then
        echo ""
        return
    fi

    # Read last 4KB and find the last stop_reason value
    tail -c 4096 "$real_path" 2>/dev/null \
        | grep -o '"stop_reason":"[^"]*"' \
        | tail -1 \
        | grep -o '"[^"]*"$' \
        | tr -d '"'
}

# Scan agent output files and return a summary string.
# Returns empty string if no agents found.
scan_agent_status() {
    local tasks_dir="${AGENT_TASKS_DIR:-/tmp/claude-1000/-home-admin-lobster-workspace/tasks}"

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

        # Determine agent status from stop_reason (deterministic, ~1ms)
        local stop_reason
        stop_reason=$(_get_stop_reason "$filepath")

        local status_text
        if [ "$stop_reason" = "end_turn" ]; then
            status_text="done"
        elif [ -z "$stop_reason" ]; then
            status_text="starting"
        else
            # "tool_use" or any other value = actively running
            status_text="running"
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

# Scan for completed tasks that haven't been reported yet.
# A task is "completed" when its last stop_reason is "end_turn" (deterministic).
# Previously used mtime heuristic — now uses the JSONL stop_reason field.
#
# Returns a structured completion summary or empty string if nothing new.
scan_completed_tasks() {
    local tasks_dir="${AGENT_TASKS_DIR:-/tmp/claude-1000/-home-admin-lobster-workspace/tasks}"
    local reported_file="$AGENT_STATE_DIR/reported-tasks"

    mkdir -p "$AGENT_STATE_DIR"
    touch "$reported_file" 2>/dev/null

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

    local completed=()

    for filepath in "${output_files[@]}"; do
        local basename_f
        basename_f=$(basename "$filepath" .output)

        # Skip if already reported
        if grep -q "^${basename_f}$" "$reported_file" 2>/dev/null; then
            continue
        fi

        # Check stop_reason — only "end_turn" means definitively done
        local stop_reason
        stop_reason=$(_get_stop_reason "$filepath")

        if [ "$stop_reason" != "end_turn" ]; then
            continue
        fi

        # Extract the last assistant message text for a brief summary
        local last_msg
        last_msg=$(grep '"type":"assistant"' "$filepath" 2>/dev/null | tail -1 | \
            python3 -c "
import json, sys
try:
    line = sys.stdin.readline()
    d = json.loads(line)
    msg = d.get('message', {})
    content = msg.get('content', [])
    texts = [c.get('text', '') for c in content if c.get('type') == 'text']
    result = ' '.join(texts).strip()
    # Truncate to 200 chars for concise reporting
    if len(result) > 200:
        result = result[:197] + '...'
    print(result)
except Exception:
    print('')
" 2>/dev/null)

        local turns
        turns=$(grep -c '"type":"assistant"' "$filepath" 2>/dev/null) || turns=0

        # Mark as reported
        echo "$basename_f" >> "$reported_file"

        completed+=("Task ${basename_f} completed (${turns} turns, stop_reason=end_turn): ${last_msg}")
    done

    if [ ${#completed[@]} -eq 0 ]; then
        return 0
    fi

    # Build structured result
    local result=""
    for entry in "${completed[@]}"; do
        if [ -z "$result" ]; then
            result="$entry"
        else
            result="$result | $entry"
        fi
    done

    echo "$result"
}
