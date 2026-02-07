# Lobster Health Monitoring System

## Overview

The health monitoring system provides LLM-independent checks to detect and auto-recover from failures in the Lobster always-on Claude session.

## Components

### 1. Health Check Script (`scripts/health-check-v2.sh`)

Runs every 2 minutes via cron and performs the following checks:

| Check | What it detects | Action |
|-------|-----------------|--------|
| Tmux session | Session not running | Restart |
| Claude process | Process crashed/missing | Restart |
| Heartbeat | Claude stuck (not polling) | Restart |
| Stale messages | Messages >15min old in inbox | Restart |
| MCP server | Inbox server not running | Warning only |
| Memory | >90% memory usage | Warning only |
| Claude active | Stuck at prompt after errors | Restart |

### 2. Heartbeat System

The MCP inbox server automatically touches a heartbeat file (`~/lobster-workspace/logs/claude-heartbeat`) every 60 seconds while Claude is in the `wait_for_messages` loop. If the heartbeat becomes stale (>10 minutes old), it indicates Claude is stuck.

**Heartbeat locations:**
- `~/lobster-workspace/logs/claude-heartbeat` - Main heartbeat (updated by MCP server)
- `~/lobster-workspace/logs/health-check.heartbeat` - Health check is running

### 3. Restart Rate Limiting

To prevent restart loops:
- Maximum 3 restart attempts per 5-minute window
- If exceeded, logs error and sends alert
- Manual intervention required after rate limit

### 4. Alerting (`scripts/alert.sh`)

Sends alerts when critical issues occur:
- Logs to `~/lobster-workspace/logs/alerts.log`
- Optionally sends to Telegram (set `LOBSTER_ADMIN_CHAT_ID` env var)

## Configuration

Edit `scripts/health-check-v2.sh` to adjust:

```bash
HEARTBEAT_MAX_AGE_SECONDS=600   # 10 minutes
STALE_MESSAGE_THRESHOLD_MINUTES=15
MAX_RESTART_ATTEMPTS=3
RESTART_COOLDOWN_SECONDS=300    # 5 minutes
MEMORY_THRESHOLD=90             # percentage
```

## Cron Setup

The health check runs every 2 minutes:

```cron
*/2 * * * * /home/admin/lobster/scripts/health-check-v2.sh
```

## Log Files

- `~/lobster-workspace/logs/health-check.log` - Health check results
- `~/lobster-workspace/logs/heartbeat.log` - Heartbeat status messages
- `~/lobster-workspace/logs/alerts.log` - Alert history
- `~/lobster-workspace/logs/health-restart-state` - Restart rate limiting state

## Manual Commands

```bash
# Run health check manually
~/lobster/scripts/health-check-v2.sh

# Check current status
~/lobster/scripts/lobster-status.sh

# View recent health check logs
tail -50 ~/lobster-workspace/logs/health-check.log

# Touch heartbeat manually (for testing)
~/lobster/scripts/heartbeat.sh "Manual heartbeat"
```

## Failure Scenarios Handled

### API Errors
When Claude hits Anthropic API 500 errors and stops responding:
1. Heartbeat becomes stale (no `wait_for_messages` calls)
2. Health check detects stale heartbeat after 10 minutes
3. Automatic restart triggered

### Process Crash
If Claude process dies:
1. Health check detects missing process
2. Immediate restart triggered

### Stuck at Prompt
If Claude is at prompt but not processing:
1. Tmux output check detects "API Error" + idle prompt
2. Automatic restart triggered

### Memory Exhaustion
If memory usage exceeds threshold:
1. Warning logged
2. Does not auto-restart (may cause data loss)
3. Consider adding swap or increasing instance size

## Improvements Over v1

| v1 (old) | v2 (new) |
|----------|----------|
| Only checked file age in inbox | Multi-layered health checks |
| No heartbeat | Automatic heartbeat from MCP server |
| No rate limiting | Rate-limited restarts |
| No alerting | Alert system |
| Single check | 7 independent checks |
| No stuck detection | Detects Claude stuck at prompt |
