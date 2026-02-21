#!/bin/bash
#===============================================================================
# Lobster Incident Investigator
#
# Triggered by health-check-v3.sh BEFORE a restart. Collects diagnostic info
# and produces a structured incident report. The report is saved to
# ~/lobster/incidents/ and optionally filed as a GitHub issue.
#
# Usage:
#   ~/lobster/scripts/investigate-incident.sh "stale inbox (>3m)"
#   ~/lobster/scripts/investigate-incident.sh "no Claude process in lobster tmux"
#   ~/lobster/scripts/investigate-incident.sh "systemd service not active"
#
# Arguments:
#   $1 - Alert reason string (from health-check-v3.sh)
#
# Environment:
#   INCIDENT_DIR      - Override incident storage (default: ~/lobster/incidents)
#   SKIP_GITHUB_ISSUE - Set to "1" to skip filing a GitHub issue
#
# Output:
#   - Writes incident report to $INCIDENT_DIR/YYYY-MM-DD_HHMMSS_<slug>.md
#   - Optionally creates a GitHub issue in the Lobster repo
#   - Prints the report path to stdout
#===============================================================================

set -o pipefail

#===============================================================================
# Configuration
#===============================================================================
INCIDENT_DIR="${INCIDENT_DIR:-${LOBSTER_INSTALL_DIR:-$HOME/lobster}/incidents}"
LOGS_DIR="${LOBSTER_WORKSPACE:-$HOME/lobster-workspace}/logs"
INBOX_DIR="$HOME/messages/inbox"
PROCESSING_DIR="$HOME/messages/processing"
TMUX_SOCKET="lobster"
TMUX_SESSION="lobster"

HEALTH_LOG="$LOGS_DIR/health-check.log"
MCP_LOG="$LOGS_DIR/mcp-server.log"
TELEGRAM_LOG="$LOGS_DIR/telegram-bot.log"
AUDIT_LOG="$LOGS_DIR/audit.jsonl"

# GitHub issue filing (optional)
GITHUB_REPO="SiderealPress/Lobster"
SKIP_GITHUB_ISSUE="${SKIP_GITHUB_ISSUE:-1}"

# Alert reason from caller
ALERT_REASON="${1:-unknown}"
INCIDENT_TIME=$(date -u '+%Y-%m-%d %H:%M:%S UTC')
INCIDENT_EPOCH=$(date +%s)
INCIDENT_SLUG=$(echo "$ALERT_REASON" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9]/-/g' | sed 's/--*/-/g' | sed 's/^-//;s/-$//')
INCIDENT_FILE="$INCIDENT_DIR/$(date -u '+%Y-%m-%d_%H%M%S')_${INCIDENT_SLUG}.md"

mkdir -p "$INCIDENT_DIR"

#===============================================================================
# Utility: safe log tail (handles missing files)
#===============================================================================
safe_tail() {
    local file="$1"
    local lines="${2:-50}"
    if [[ -f "$file" ]]; then
        tail -n "$lines" "$file" 2>/dev/null || echo "(unable to read)"
    else
        echo "(file not found: $file)"
    fi
}

#===============================================================================
# Collect: System metrics
#===============================================================================
collect_system_metrics() {
    local mem_total mem_used mem_pct swap_total swap_used
    mem_total=$(free -m | awk '/^Mem:/ {print $2}')
    mem_used=$(free -m | awk '/^Mem:/ {print $3}')
    mem_pct=$(free | awk '/^Mem:/ {printf "%.1f", $3/$2 * 100}')
    swap_total=$(free -m | awk '/^Swap:/ {print $2}')
    swap_used=$(free -m | awk '/^Swap:/ {print $3}')

    local disk_pct disk_avail
    disk_pct=$(df -h "$HOME" | awk 'NR==2 {print $5}')
    disk_avail=$(df -h "$HOME" | awk 'NR==2 {print $4}')

    local load_1 load_5 load_15
    read -r load_1 load_5 load_15 _ < /proc/loadavg

    local uptime_str
    uptime_str=$(uptime -p 2>/dev/null || uptime)

    cat <<EOF
### System Metrics

| Metric | Value |
|--------|-------|
| Memory | ${mem_used}MB / ${mem_total}MB (${mem_pct}%) |
| Swap | ${swap_used}MB / ${swap_total}MB |
| Disk | ${disk_pct} used (${disk_avail} available) |
| Load Average | ${load_1} / ${load_5} / ${load_15} |
| System Uptime | ${uptime_str} |
EOF
}

#===============================================================================
# Collect: Process information
#===============================================================================
collect_process_info() {
    echo "### Process State"
    echo ""

    # Claude processes
    echo "**Claude processes:**"
    echo '```'
    ps aux --no-headers 2>/dev/null | grep -E "claude" | grep -v grep || echo "(none found)"
    echo '```'
    echo ""

    # Top memory consumers
    echo "**Top 10 memory consumers:**"
    echo '```'
    ps aux --sort=-%mem 2>/dev/null | head -11
    echo '```'
    echo ""

    # Tmux session state
    echo "**Tmux session state:**"
    echo '```'
    tmux -L "$TMUX_SOCKET" list-sessions 2>/dev/null || echo "(no lobster tmux sessions)"
    echo ""
    tmux -L "$TMUX_SOCKET" list-panes -t "$TMUX_SESSION" -F '#{pane_pid} #{pane_current_command} #{pane_dead}' 2>/dev/null || echo "(no panes)"
    echo '```'
    echo ""

    # Systemd service status
    echo "**Systemd service status:**"
    echo '```'
    systemctl status lobster-claude --no-pager 2>/dev/null | head -20 || echo "(service not found)"
    echo ""
    systemctl status lobster-router --no-pager 2>/dev/null | head -20 || echo "(service not found)"
    echo '```'
}

#===============================================================================
# Collect: Recent journalctl logs
#===============================================================================
collect_journal_logs() {
    echo "### Journalctl (lobster-claude, last 30 lines)"
    echo '```'
    journalctl -u lobster-claude --no-pager -n 30 --since "-30min" 2>/dev/null || echo "(no journal entries)"
    echo '```'
    echo ""

    echo "### Journalctl (lobster-router, last 15 lines)"
    echo '```'
    journalctl -u lobster-router --no-pager -n 15 --since "-30min" 2>/dev/null || echo "(no journal entries)"
    echo '```'
}

#===============================================================================
# Collect: Application logs
#===============================================================================
collect_app_logs() {
    echo "### Health Check Log (last 30 lines)"
    echo '```'
    safe_tail "$HEALTH_LOG" 30
    echo '```'
    echo ""

    echo "### MCP Server Log (last 40 lines)"
    echo '```'
    safe_tail "$MCP_LOG" 40
    echo '```'
    echo ""

    echo "### Telegram Bot Log (last 20 lines)"
    echo '```'
    safe_tail "$TELEGRAM_LOG" 20
    echo '```'
}

#===============================================================================
# Collect: Inbox state (what was being processed)
#===============================================================================
collect_inbox_state() {
    echo "### Inbox State at Time of Failure"
    echo ""

    local inbox_count processing_count
    inbox_count=$(find "$INBOX_DIR" -maxdepth 1 -name "*.json" 2>/dev/null | wc -l)
    processing_count=$(find "$PROCESSING_DIR" -maxdepth 1 -name "*.json" 2>/dev/null | wc -l)

    echo "| Queue | Count |"
    echo "|-------|-------|"
    echo "| Inbox | $inbox_count |"
    echo "| Processing | $processing_count |"
    echo ""

    # Show stale messages with age
    if [[ $inbox_count -gt 0 ]]; then
        echo "**Stale inbox messages:**"
        echo '```'
        local now
        now=$(date +%s)
        find "$INBOX_DIR" -maxdepth 1 -name "*.json" -print0 2>/dev/null | while IFS= read -r -d '' f; do
            local file_time age basename_f
            file_time=$(stat -c %Y "$f" 2>/dev/null)
            [[ -z "$file_time" ]] && continue
            age=$((now - file_time))
            basename_f=$(basename "$f")
            # Extract text preview from JSON
            local preview
            preview=$(python3 -c "
import json, sys
try:
    d = json.load(open('$f'))
    text = d.get('text', '(no text)')[:80]
    src = d.get('source', '?')
    user = d.get('username', '?')
    print(f'{src}/{user}: {text}')
except:
    print('(unreadable)')
" 2>/dev/null || echo "(parse error)")
            printf "  [%3ds old] %-40s %s\n" "$age" "$basename_f" "$preview"
        done
        echo '```'
        echo ""
    fi

    # Show messages currently being processed
    if [[ $processing_count -gt 0 ]]; then
        echo "**Messages in processing:**"
        echo '```'
        find "$PROCESSING_DIR" -maxdepth 1 -name "*.json" -print0 2>/dev/null | while IFS= read -r -d '' f; do
            local basename_f
            basename_f=$(basename "$f")
            local preview
            preview=$(python3 -c "
import json, sys
try:
    d = json.load(open('$f'))
    text = d.get('text', '(no text)')[:80]
    src = d.get('source', '?')
    print(f'{src}: {text}')
except:
    print('(unreadable)')
" 2>/dev/null || echo "(parse error)")
            echo "  $basename_f - $preview"
        done
        echo '```'
        echo ""
    fi
}

#===============================================================================
# Collect: Tmux scrollback capture (what Claude was doing)
#===============================================================================
collect_tmux_scrollback() {
    echo "### Tmux Scrollback (last 50 lines of Claude session)"
    echo '```'
    if tmux -L "$TMUX_SOCKET" has-session -t "$TMUX_SESSION" 2>/dev/null; then
        tmux -L "$TMUX_SOCKET" capture-pane -t "$TMUX_SESSION" -p -S -50 2>/dev/null || echo "(capture failed)"
    else
        echo "(no tmux session to capture)"
    fi
    echo '```'
}

#===============================================================================
# Collect: Recent restart history
#===============================================================================
collect_restart_history() {
    echo "### Recent Restart History"
    echo '```'
    grep -E "(Restarting|Restart successful|Restart verification failed|BLACK|RED)" "$HEALTH_LOG" 2>/dev/null | tail -20 || echo "(no restart history)"
    echo '```'
    echo ""

    # Restart state file
    local restart_state_file="$LOGS_DIR/health-restart-state-v3"
    if [[ -f "$restart_state_file" ]]; then
        local last_restart_time restart_count
        read -r last_restart_time restart_count < "$restart_state_file" 2>/dev/null
        local now
        now=$(date +%s)
        local elapsed=$((now - last_restart_time))
        echo "**Restart rate state:** $restart_count restart(s) in last ${elapsed}s"
    fi
}

#===============================================================================
# Collect: OOM killer checks
#===============================================================================
collect_oom_info() {
    echo "### OOM Killer Activity (last 30 min)"
    echo '```'
    dmesg --time-format iso 2>/dev/null | grep -i -E "(oom|killed process|out of memory)" | tail -10 || \
    dmesg 2>/dev/null | grep -i -E "(oom|killed process|out of memory)" | tail -10 || \
    echo "(no OOM events or cannot read dmesg)"
    echo '```'
}

#===============================================================================
# Analyze: Determine likely root cause
#===============================================================================
analyze_root_cause() {
    local reasons=()
    local confidence="low"

    # Check if it's a memory issue
    local mem_pct
    mem_pct=$(free | awk '/^Mem:/ {printf "%.0f", $3/$2 * 100}')
    if [[ $mem_pct -gt 85 ]]; then
        reasons+=("High memory usage (${mem_pct}%) - possible OOM or memory pressure")
        confidence="medium"
    fi

    # Check for OOM in dmesg
    if dmesg 2>/dev/null | grep -qi "out of memory\|oom.*kill"; then
        reasons+=("OOM killer was active - Claude or MCP process was killed")
        confidence="high"
    fi

    # Check if Claude process is missing
    if ! pgrep -f "claude.*--dangerously-skip-permissions" > /dev/null 2>&1; then
        reasons+=("Claude process not running - may have crashed or exited")
    fi

    # Check for stale inbox pattern (most common)
    if echo "$ALERT_REASON" | grep -qi "stale inbox"; then
        # This is the most common case - Claude got stuck
        local claude_count
        claude_count=$(pgrep -c -f "claude" 2>/dev/null || echo "0")
        if [[ $claude_count -gt 1 ]]; then
            reasons+=("Stale inbox with multiple Claude processes ($claude_count) - likely context window exhaustion or blocked subagent")
            confidence="medium"
        elif [[ $claude_count -eq 1 ]]; then
            reasons+=("Stale inbox with single Claude process - likely hung on long operation (TaskOutput wait, API timeout, or context window limit)")
            confidence="medium"
        else
            reasons+=("Stale inbox with no Claude process - process crashed before watchdog could detect")
            confidence="medium"
        fi

        # Check MCP log for errors
        if [[ -f "$MCP_LOG" ]]; then
            local recent_errors
            recent_errors=$(tail -100 "$MCP_LOG" 2>/dev/null | grep -ciE "error|exception|traceback" || true)
            recent_errors=${recent_errors:-0}
            recent_errors=$(echo "$recent_errors" | tr -d '[:space:]')
            if [[ "$recent_errors" -gt 5 ]] 2>/dev/null; then
                reasons+=("MCP server showing $recent_errors errors in recent logs - possible MCP crash cascade")
                confidence="medium"
            fi
        fi
    fi

    # Check for systemd-related
    if echo "$ALERT_REASON" | grep -qi "systemd\|service"; then
        reasons+=("Systemd service failure - may be infrastructure issue or repeated crash loop")
    fi

    # Check for tmux-related
    if echo "$ALERT_REASON" | grep -qi "tmux"; then
        reasons+=("Tmux session missing - process exited cleanly or crashed")
    fi

    # Check journal for hints about the specific death
    local journal_hints
    journal_hints=$(journalctl -u lobster-claude --no-pager -n 5 --since "-10min" 2>/dev/null | grep -i "consumed\|memory peak\|exit\|fail\|kill" || true)
    if [[ -n "$journal_hints" ]]; then
        # Parse memory peak from systemd
        local peak_mem
        peak_mem=$(echo "$journal_hints" | grep -oP 'memory peak.*' | head -1)
        if [[ -n "$peak_mem" ]]; then
            reasons+=("Systemd reports: $peak_mem")
        fi
        local cpu_time
        cpu_time=$(echo "$journal_hints" | grep -oP 'Consumed.*CPU time' | head -1)
        if [[ -n "$cpu_time" ]]; then
            reasons+=("Session resource usage: $cpu_time")
        fi
    fi

    # Fallback
    if [[ ${#reasons[@]} -eq 0 ]]; then
        reasons+=("Unable to determine specific root cause from available diagnostics")
        confidence="low"
    fi

    echo "### Likely Root Cause"
    echo ""
    echo "**Confidence:** $confidence"
    echo ""
    for r in "${reasons[@]}"; do
        echo "- $r"
    done
}

#===============================================================================
# Collect: Downtime estimation
#===============================================================================
estimate_downtime() {
    echo "### Downtime Estimate"
    echo ""

    # Find when the problem likely started by looking at the oldest stale message
    local oldest_age=0
    local now
    now=$(date +%s)

    while IFS= read -r -d '' f; do
        local file_time
        file_time=$(stat -c %Y "$f" 2>/dev/null)
        [[ -z "$file_time" ]] && continue
        local age=$((now - file_time))
        [[ $age -gt $oldest_age ]] && oldest_age=$age
    done < <(find "$INBOX_DIR" -maxdepth 1 -name "*.json" -print0 2>/dev/null)

    if [[ $oldest_age -gt 0 ]]; then
        local minutes=$((oldest_age / 60))
        local seconds=$((oldest_age % 60))
        local approx_start
        approx_start=$(date -u -d "@$((now - oldest_age))" '+%Y-%m-%d %H:%M:%S UTC' 2>/dev/null || echo "~${minutes}m${seconds}s ago")
        echo "- Oldest stale message: **${oldest_age}s** (${minutes}m ${seconds}s)"
        echo "- Estimated issue start: **$approx_start**"
        echo "- Detection time: **$INCIDENT_TIME**"
        echo "- Estimated downtime: **~${minutes}m ${seconds}s** (minimum, from stale message age)"
    else
        echo "- No stale messages found (may have been a process crash caught early)"
        echo "- Detection time: **$INCIDENT_TIME**"
    fi
}

#===============================================================================
# Collect: Recent audit trail
#===============================================================================
collect_audit_trail() {
    echo "### Recent Audit Trail (last 15 entries)"
    echo '```'
    if [[ -f "$AUDIT_LOG" ]]; then
        tail -15 "$AUDIT_LOG" | python3 -c "
import json, sys
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        d = json.loads(line)
        ts = d.get('ts', d.get('timestamp', '?'))
        # Truncate timestamp for readability
        if len(ts) > 19:
            ts = ts[:19]
        tool = d.get('tool', d.get('action', '?'))
        args = d.get('args', {})
        result = d.get('result', '?')
        # Build a compact args summary
        args_str = ''
        if isinstance(args, dict):
            parts = []
            for k, v in args.items():
                sv = str(v)[:40]
                parts.append(f'{k}={sv}')
            args_str = ', '.join(parts)[:60]
        print(f'  {ts} | {tool:20s} | {result:4s} | {args_str}')
    except Exception as e:
        print(f'  (parse error: {line[:80]})')
" 2>/dev/null || echo "(unable to parse audit log)"
    else
        echo "(no audit log found)"
    fi
    echo '```'
}

#===============================================================================
# File GitHub Issue (optional)
#===============================================================================
file_github_issue() {
    local report_file="$1"

    if [[ "$SKIP_GITHUB_ISSUE" == "1" ]]; then
        return 0
    fi

    # Check if gh CLI is available and authenticated
    if ! command -v gh &>/dev/null; then
        echo "(gh CLI not found, skipping GitHub issue)" >&2
        return 0
    fi

    if ! gh auth status &>/dev/null 2>&1; then
        echo "(gh not authenticated, skipping GitHub issue)" >&2
        return 0
    fi

    # Create a concise issue body (GitHub has limits)
    local title="Incident Report: ${ALERT_REASON} ($(date -u '+%Y-%m-%d %H:%M UTC'))"

    # Truncate the report for the issue body (keep under 60KB)
    local body
    body=$(head -c 60000 "$report_file")

    local issue_url
    issue_url=$(gh issue create \
        --repo "$GITHUB_REPO" \
        --title "$title" \
        --label "incident" \
        --body "$body" \
        2>/dev/null) || {
        echo "(failed to create GitHub issue)" >&2
        return 0
    }

    echo "$issue_url"
}

#===============================================================================
# Ensure 'incident' label exists on the repo
#===============================================================================
ensure_incident_label() {
    if [[ "$SKIP_GITHUB_ISSUE" == "1" ]]; then
        return 0
    fi
    if ! command -v gh &>/dev/null || ! gh auth status &>/dev/null 2>&1; then
        return 0
    fi

    # Create label if it doesn't exist (ignore errors if it already exists)
    gh label create "incident" \
        --repo "$GITHUB_REPO" \
        --description "Automated incident report from health monitoring" \
        --color "D93F0B" \
        2>/dev/null || true
}

#===============================================================================
# Main: Assemble the incident report
#===============================================================================
main() {
    # Ensure incident label exists (runs once, idempotent)
    ensure_incident_label

    # Build the report
    {
        echo "# Incident Report"
        echo ""
        echo "| Field | Value |"
        echo "|-------|-------|"
        echo "| Timestamp | $INCIDENT_TIME |"
        echo "| Alert Reason | $ALERT_REASON |"
        echo "| Report File | \`$(basename "$INCIDENT_FILE")\` |"
        echo ""

        echo "---"
        echo ""

        # Root cause analysis (run this first so it's prominent)
        analyze_root_cause
        echo ""

        echo "---"
        echo ""

        # Downtime
        estimate_downtime
        echo ""

        echo "---"
        echo ""

        # System metrics
        collect_system_metrics
        echo ""

        echo "---"
        echo ""

        # Process info
        collect_process_info
        echo ""

        echo "---"
        echo ""

        # Inbox state
        collect_inbox_state
        echo ""

        echo "---"
        echo ""

        # Tmux scrollback
        collect_tmux_scrollback
        echo ""

        echo "---"
        echo ""

        # OOM info
        collect_oom_info
        echo ""

        echo "---"
        echo ""

        # Restart history
        collect_restart_history
        echo ""

        echo "---"
        echo ""

        # Audit trail
        collect_audit_trail
        echo ""

        echo "---"
        echo ""

        # Application logs
        collect_app_logs
        echo ""

        echo "---"
        echo ""

        # Journal logs
        collect_journal_logs
        echo ""

    } > "$INCIDENT_FILE"

    # File GitHub issue
    local issue_url=""
    issue_url=$(file_github_issue "$INCIDENT_FILE")

    # Append GitHub issue link if created
    if [[ -n "$issue_url" ]]; then
        echo "" >> "$INCIDENT_FILE"
        echo "---" >> "$INCIDENT_FILE"
        echo "" >> "$INCIDENT_FILE"
        echo "**GitHub Issue:** $issue_url" >> "$INCIDENT_FILE"
    fi

    # Output the report path (and issue URL if created)
    echo "$INCIDENT_FILE"
    if [[ -n "$issue_url" ]]; then
        echo "$issue_url"
    fi
}

main "$@"
