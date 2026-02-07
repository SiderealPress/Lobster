# Lobster Health Monitoring System

## Overview

The health monitoring system provides LLM-independent checks to detect and auto-recover from failures in the Lobster always-on Claude session.

## Components

### 1. Health Check Script (`scripts/health-check-v3.sh`)

Runs every 2 minutes via cron and performs deterministic, LLM-independent health checks:

| Check | What it detects | Action |
|-------|-----------------|--------|
| Systemd services | Service not active | Restart |
| Tmux session | Session not running | Restart |
| Claude process | Process crashed/missing (verified in tmux) | Restart |
| Inbox drain | Messages >5min old (RED) or >2min (YELLOW) | Restart / Monitor |
| Memory | >90% memory usage | Restart |
| Disk | >95% disk usage | Warning only |

### 2. Inbox Drain (Primary Health Signal)

v3 replaces the heartbeat system with a simpler, fully deterministic approach: **inbox drain monitoring**. If messages sit in the inbox longer than the stale threshold, it means Claude is not processing them -- regardless of the reason.

**Escalation ladder:**
- **GREEN** - All checks pass, inbox is draining normally
- **YELLOW** - Messages exist but are < 5 minutes old (monitoring)
- **RED** - Messages > 5 minutes old, or infrastructure failure -- triggers restart
- **BLACK** - 3 restart failures in 10-minute window -- alerts operator, stops retrying

### 3. Restart Rate Limiting

To prevent restart loops:
- Maximum 3 restart attempts per 10-minute window
- If exceeded (BLACK level), sends Telegram alert and stops retrying
- Manual intervention required after rate limit

### 4. Alerting (Direct Telegram)

v3 sends alerts directly via Telegram API (curl), bypassing the outbox and MCP server entirely. This ensures alerts work even when those subsystems are broken.

- Reads bot token and chat ID from `config/config.env`
- Sends alerts on successful recovery and on BLACK-level failures
- Falls back to log-only if Telegram credentials are unavailable

## Configuration

Edit `scripts/health-check-v3.sh` to adjust:

```bash
STALE_THRESHOLD_SECONDS=300          # 5 minutes - RED if any message older
YELLOW_THRESHOLD_SECONDS=120         # 2 minutes - YELLOW warning
MAX_RESTART_ATTEMPTS=3
RESTART_COOLDOWN_SECONDS=600         # 10 min window for counting attempts
MEMORY_THRESHOLD=90                  # percentage
DISK_THRESHOLD=95                    # percentage
```

## Cron Setup

The health check runs every 2 minutes:

```cron
*/2 * * * * /home/admin/lobster/scripts/health-check-v3.sh
```

## Log Files

- `~/lobster-workspace/logs/health-check.log` - Health check results
- `~/lobster-workspace/logs/heartbeat.log` - Heartbeat status messages
- `~/lobster-workspace/logs/alerts.log` - Alert history
- `~/lobster-workspace/logs/health-restart-state-v3` - Restart rate limiting state

## Manual Commands

```bash
# Run health check manually
~/lobster/scripts/health-check-v3.sh

# Check current status
~/lobster/scripts/lobster-status.sh

# View recent health check logs
tail -50 ~/lobster-workspace/logs/health-check.log

# Touch heartbeat manually (for testing)
~/lobster/scripts/heartbeat.sh "Manual heartbeat"
```

## Failure Scenarios Handled

### API Errors / Claude Stuck
When Claude hits API errors or stops processing for any reason:
1. Messages accumulate in inbox without being drained
2. Health check detects stale messages (>5 minutes)
3. Automatic restart via `systemctl restart lobster-claude`

### Process Crash
If Claude process dies:
1. Health check detects missing Claude process (or process not in lobster tmux)
2. Automatic restart via systemd

### Service Down
If systemd service stops:
1. Health check detects `lobster-claude` or `lobster-router` not active
2. Automatic restart via systemd

### Disk Full
If disk usage exceeds 95%:
1. Warning logged (YELLOW level)
2. Does not auto-restart (restart won't help)
3. Needs manual cleanup

### Memory Exhaustion
If memory usage exceeds 90%:
1. Triggers restart (RED level) to reclaim memory
2. Consider adding swap or increasing instance size if recurring

## Improvements Over v2

| v2 (old) | v3 (current) |
|----------|--------------|
| Relied on heartbeat file (LLM-dependent) | Zero LLM dependency - inbox drain is the primary signal |
| Scraped tmux output to detect stuck state | Verifies Claude process ancestry in tmux (deterministic) |
| Manual tmux session rebuild on restart | Recovery via systemd (never manually rebuilds tmux) |
| Alerts via `scripts/alert.sh` | Direct Telegram alerts via curl (bypasses outbox/MCP) |
| 15-minute stale message threshold | 5-minute stale threshold with 2-minute YELLOW warning |
| 5-minute restart cooldown | 10-minute restart cooldown window |
| 7 checks (some LLM-dependent) | 6 checks, all fully deterministic |
| No disk space monitoring | Disk usage check with threshold |

## Improvements Over v1

| v1 (old) | v2 (replaced) |
|----------|---------------|
| Only checked file age in inbox | Multi-layered health checks |
| No heartbeat | Automatic heartbeat from MCP server |
| No rate limiting | Rate-limited restarts |
| No alerting | Alert system |
| Single check | 7 independent checks |
| No stuck detection | Detects Claude stuck at prompt |
