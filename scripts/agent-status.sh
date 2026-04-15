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
#   # Returns: "Agents: abc123 (52 turns, running), def456 (3 turns, starting)"
#   # Returns: "" (empty string) if no agents found or all are done
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

# Derive the Claude Code tmp base for this user and workspace.
# Claude Code stores session data under /tmp/claude-<uid>/<workspace-hash>/
# where workspace-hash = workspace path with '/' replaced by '-'.
# Task output files live in per-session UUID subdirs: <base>/<uuid>/tasks/*.output
# If AGENT_TASKS_DIR is set, it overrides this (used by tests).
_default_tasks_glob() {
    if [ -n "${AGENT_TASKS_DIR:-}" ]; then
        echo "${AGENT_TASKS_DIR}/*.output"
        return
    fi
    local claude_tmp_base="/tmp/claude-$(id -u)"
    local workspace_hash
    workspace_hash=$(echo "${LOBSTER_WORKSPACE:-$HOME/lobster-workspace}" | sed 's|/|-|g')
    echo "${claude_tmp_base}/${workspace_hash}/*/tasks/*.output"
}

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
# Only includes running/starting agents — completed (end_turn) agents are excluded.
# Returns empty string if no agents found or all are done.
scan_agent_status() {
    local tasks_glob
    tasks_glob=$(_default_tasks_glob)

    local output_files=()
    while IFS= read -r f; do
        output_files+=("$f")
    done < <(compgen -G "$tasks_glob" 2>/dev/null | sort)

    if [ ${#output_files[@]} -eq 0 ]; then
        return 0
    fi

    local entries=()
    local running_count=0
    local running_skipped=0

    # Sort by mtime descending (most recently active first) and take top N
    local sorted_files=()
    while IFS= read -r f; do
        sorted_files+=("$f")
    done < <(ls -t "${output_files[@]}" 2>/dev/null)

    local display_count=0
    for filepath in "${sorted_files[@]}"; do
        local basename_f
        basename_f=$(basename "$filepath" .output)

        # Skip bash tool output files — only symlinks are real subagent outputs
        if [ ! -L "$filepath" ]; then
            continue
        fi

        # Determine agent status from stop_reason (deterministic, ~1ms)
        local stop_reason
        stop_reason=$(_get_stop_reason "$filepath")

        # Skip completed agents — self-check is only for active work.
        # Terminal stop reasons: end_turn (normal), stop_sequence (hit stop seq),
        # max_tokens (hit token limit). All mean the agent is done.
        if [ "$stop_reason" = "end_turn" ] || [ "$stop_reason" = "stop_sequence" ] || [ "$stop_reason" = "max_tokens" ]; then
            continue
        fi

        # Agents whose output files are stale are treated as dead.
        # - stop_reason=tool_use: stale after 30 min (crashed mid-tool-call)
        # - empty stop_reason:    stale after 60 min (never progressed past "starting")
        # Without these checks a crashed agent would show as "running"/"starting" forever.
        local STALE_TOOL_USE_SECONDS=$(( 30 * 60 ))
        local STALE_STARTING_SECONDS=$(( 60 * 60 ))
        if [ "$stop_reason" = "tool_use" ] || [ -z "$stop_reason" ]; then
            local now file_mtime file_age_seconds
            now=$(date +%s)
            # stat -c %Y is GNU coreutils; stat -f %m is BSD/macOS fallback
            file_mtime=$(stat -c %Y "$filepath" 2>/dev/null || stat -f %m "$filepath" 2>/dev/null || echo "$now")
            file_age_seconds=$(( now - file_mtime ))
            local threshold
            if [ "$stop_reason" = "tool_use" ]; then
                threshold=$STALE_TOOL_USE_SECONDS
            else
                threshold=$STALE_STARTING_SECONDS
            fi
            if [ "$file_age_seconds" -gt "$threshold" ]; then
                continue  # file too old — agent is dead, not running
            fi
        fi

        local status_text
        if [ -z "$stop_reason" ]; then
            status_text="starting"
        else
            # "tool_use" (recently active) or any other value = actively running
            status_text="running"
        fi

        running_count=$(( running_count + 1 ))

        if [ "$display_count" -ge "$AGENT_MAX_DISPLAY" ]; then
            running_skipped=$(( running_skipped + 1 ))
            continue
        fi

        # Count assistant turns
        local turns
        turns=$(grep -c '"type":"assistant"' "$filepath" 2>/dev/null) || turns=0

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

    # Add "+N more" if we capped the display (only running agents count)
    if [ "$running_skipped" -gt 0 ]; then
        result+=", +${running_skipped} more"
    fi

    echo "$result"
}

# Maximum completed tasks to surface per self-check cycle.
# Any backlog beyond this is silently marked as reported to prevent message bloat.
COMPLETED_MAX_REPORT=3

# Scan for completed tasks that haven't been reported yet.
# A task is "completed" when its last stop_reason is "end_turn" (deterministic).
# Previously used mtime heuristic — now uses the JSONL stop_reason field.
#
# Cap: reports at most COMPLETED_MAX_REPORT tasks per cycle. Any backlog beyond
# that is immediately marked as reported so it never accumulates.
#
# Returns a structured completion summary or empty string if nothing new.
scan_completed_tasks() {
    local tasks_glob
    tasks_glob=$(_default_tasks_glob)
    local reported_file="$AGENT_STATE_DIR/reported-tasks"

    mkdir -p "$AGENT_STATE_DIR"
    touch "$reported_file" 2>/dev/null

    local output_files=()
    while IFS= read -r f; do
        output_files+=("$f")
    done < <(compgen -G "$tasks_glob" 2>/dev/null | sort)

    if [ ${#output_files[@]} -eq 0 ]; then
        return 0
    fi

    # Collect all unreported completed task paths first (to allow batch-marking overflow)
    local unreported_completed=()
    for filepath in "${output_files[@]}"; do
        local basename_f
        basename_f=$(basename "$filepath" .output)

        # Skip bash tool output files — only symlinks are real subagent outputs
        if [ ! -L "$filepath" ]; then
            continue
        fi

        # Skip if already reported
        if grep -q "^${basename_f}$" "$reported_file" 2>/dev/null; then
            continue
        fi

        # Check stop_reason — terminal states mean definitively done.
        # end_turn: normal completion; stop_sequence: hit a stop sequence;
        # max_tokens: hit token limit. All are terminal.
        local stop_reason
        stop_reason=$(_get_stop_reason "$filepath")

        if [ "$stop_reason" = "end_turn" ] || [ "$stop_reason" = "stop_sequence" ] || [ "$stop_reason" = "max_tokens" ]; then
            unreported_completed+=("$filepath")
        fi
    done

    if [ ${#unreported_completed[@]} -eq 0 ]; then
        return 0
    fi

    # If backlog exceeds cap, mark all overflow as reported silently.
    # This prevents a large backlog from ever producing an oversized message.
    local total_unreported=${#unreported_completed[@]}
    if [ "$total_unreported" -gt "$COMPLETED_MAX_REPORT" ]; then
        local overflow_start=$COMPLETED_MAX_REPORT
        for i in "${!unreported_completed[@]}"; do
            if [ "$i" -ge "$overflow_start" ]; then
                local overflow_base
                overflow_base=$(basename "${unreported_completed[$i]}" .output)
                echo "$overflow_base" >> "$reported_file"
            fi
        done
    fi

    # Report at most COMPLETED_MAX_REPORT entries
    local completed=()
    local report_count=0
    for filepath in "${unreported_completed[@]}"; do
        if [ "$report_count" -ge "$COMPLETED_MAX_REPORT" ]; then
            break
        fi

        local basename_f
        basename_f=$(basename "$filepath" .output)

        local turns
        turns=$(grep -c '"type":"assistant"' "$filepath" 2>/dev/null) || turns=0

        # Mark as reported
        echo "$basename_f" >> "$reported_file"
        report_count=$(( report_count + 1 ))

        completed+=("Task ${basename_f} completed (${turns} turns)")
    done

    if [ ${#completed[@]} -eq 0 ]; then
        return 0
    fi

    # Build structured result — no transcript content, just task ID and turn count
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
