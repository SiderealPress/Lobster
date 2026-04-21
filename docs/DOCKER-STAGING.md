# Docker Staging Runbook

The staging Docker environment spins up a full live Lobster instance connected to a real Telegram bot for manual end-to-end testing. Unlike the [Docker testing environment](DOCKER-TESTING.md) (which uses a mock Telegram server), staging talks to the real Telegram API via a dedicated test bot: **@Lobstertown_test_bot**.

## When to use it

Use staging when:
- Manually testing a feature end-to-end before merging
- Verifying Telegram bot behavior (formatting, button interactions, threading)
- Checking dispatcher startup, MCP server launch, and routing in a realistic environment
- Reproducing a bug that only appears under live conditions
- Testing cron-dependent behavior (health checks, nightly consolidation, scheduled jobs)

Do not use staging for:
- Automated regression testing (use Docker Testing / `docker-compose.test.yml` instead)
- Production deployment (staging uses a test bot token, not the production token)

---

## Architecture

```
Host machine
  ~/.claude/              <- bind-mounted read-write into container
  ~/.local/bin/claude     <- bind-mounted read-only (reuses host binary)
  ~/lobster-config/config.staging.env  <- env vars injected at compose up

Container: lobster-staging (systemd as PID 1)
  /home/lobster/lobster/              <- repo (baked into image at build time)
  /home/lobster/messages/             <- Docker volume (lobster-staging-messages)
  /home/lobster/lobster-workspace/    <- Docker volume (lobster-staging-workspace)
  /home/lobster/lobster-config/       <- written by lobster-container-init.service

Systemd-managed services (start order):
  lobster-container-init.service      <- oneshot: writes config.env, runs install.sh --container-setup
  lobster-mcp-local.service           <- MCP server (After=lobster-container-init)
  lobster-router.service              <- Telegram router (After=lobster-container-init)
  lobster-claude.service              <- dispatcher / Claude Code (After=lobster-container-init)
  cron.service                        <- cron daemon (reads /etc/cron.d/lobster-staging)
```

Credentials are **bind-mounted from the host** — they are never baked into the image.

---

## Cron in the staging container

The staging container runs a full cron daemon managed by systemd (`cron.service`). Cron entries are baked into the image at `/etc/cron.d/lobster-staging` — the same entries that `install.sh` sets up on a production host. There is no entrypoint cron setup.

| Job | Schedule | Marker |
|-----|----------|--------|
| Health check (v3) | every 4 minutes | `LOBSTER-HEALTH` |
| Daily dependency health check | 06:00 daily | `LOBSTER-DAILY-HEALTH` |
| Nightly consolidation | 03:00 daily | `LOBSTER-NIGHTLY-CONSOLIDATION` |
| Log export | 03:00 daily | `LOBSTER-LOG-EXPORT` |
| Ghost detector | every 5 minutes | `LOBSTER-GHOST-DETECTOR` |
| OOM monitor | every 10 minutes | `LOBSTER-OOM-CHECK` |
| Worktree + audio cleanup | 04:00 daily | `LOBSTER-CLEANUP` |

**Verifying cron is running inside the container:**

```bash
# Check cron daemon status via systemd
sudo docker exec lobster-staging systemctl status cron

# Verify the lobster cron entries are installed
sudo docker exec lobster-staging ls /etc/cron.d/
sudo docker exec lobster-staging cat /etc/cron.d/lobster-staging

# Tail the cron journal for live output
sudo docker exec lobster-staging journalctl -u cron -f
```

**Tailing cron-driven log output:**

```bash
# Health check log (written by health-check-v3.sh)
sudo docker exec lobster-staging tail -f /home/lobster/lobster-workspace/logs/health-check.log

# Ghost detector log
sudo docker exec lobster-staging tail -f /home/lobster/lobster-workspace/logs/agent-monitor.log
```

---

## Prerequisites

1. **Docker and docker compose** installed on the host.

2. **lobster-config checked out** with the staging env file present:

   ```
   ~/lobster-config/config.staging.env
   ```

   The file must contain at minimum:

   ```env
   TELEGRAM_BOT_TOKEN=<test bot token>
   TELEGRAM_ALLOWED_USERS=<your Telegram user ID>
   LOBSTER_ADMIN_CHAT_ID=<your Telegram user ID>
   LOBSTER_ENV=production
   LOBSTER_DEBUG=false
   ```

   > **Critical:** `LOBSTER_ENV` must be `production`. See [LOBSTER_ENV gotcha](#lobster_env-gotcha) below.

3. **Host Claude credentials** must be present at `~/.claude/` — the container bind-mounts this directory and reads credentials from it.

4. **Host claude binary** must be installed at `~/.local/bin/claude` — this binary is also bind-mounted into the container.

---

## Building and starting

All commands run from the repo root (`~/lobster/`).

### First run (or after Dockerfile changes)

```bash
cd ~/lobster
sudo docker compose -f docker/staging/docker-compose.staging.yml up -d --build
```

### Subsequent runs (no Dockerfile changes)

```bash
sudo docker compose -f docker/staging/docker-compose.staging.yml up -d
```

---

## LOBSTER_ENV gotcha

**`LOBSTER_ENV` must be set to `production`, not `staging`.**

This is counter-intuitive. The env var does not mean "which environment am I running in" — it controls which dispatcher mode is active. Setting `LOBSTER_ENV=staging` causes the dispatcher to exit immediately without starting the message loop.

---

## Credential pattern

Credentials are **never copied into the Docker image**. The host's `~/.claude/` directory is bind-mounted into the container at the same path:

```yaml
volumes:
  - /home/lobster/.claude:/home/lobster/.claude
```

---

## Cold start time

The first time the container starts (or after a workspace volume wipe), the dispatcher takes approximately **10 minutes** to become responsive. During this time it is reading bootup context files, initializing the MCP server, and completing the dispatcher loop startup sequence.

---

## Verifying it is working

### 1. Check the container is running

```bash
sudo docker ps
```

Look for `lobster-staging` with status `Up`.

### 2. Attach to the dispatcher tmux session

```bash
sudo docker exec -it lobster-staging tmux -L lobster attach -t lobster
```

### 3. Check cron is running

```bash
sudo docker exec lobster-staging systemctl status cron
sudo docker exec lobster-staging ls /etc/cron.d/
sudo docker exec lobster-staging cat /etc/cron.d/lobster-staging
```

### 4. Tail the MCP server log

```bash
sudo docker exec lobster-staging tail -f /home/lobster/lobster-workspace/logs/mcp-server.log
```

---

## Tailing logs

| What | Command |
|------|---------|
| Router (Telegram bot) | `sudo docker exec lobster-staging tail -f /home/lobster/lobster-workspace/logs/router.log` |
| Claude session | `sudo docker exec lobster-staging tail -f /home/lobster/lobster-workspace/logs/claude.log` |
| MCP server | `sudo docker exec lobster-staging tail -f /home/lobster/lobster-workspace/logs/mcp-server.log` |
| Health check | `sudo docker exec lobster-staging tail -f /home/lobster/lobster-workspace/logs/health-check.log` |
| Ghost detector | `sudo docker exec lobster-staging tail -f /home/lobster/lobster-workspace/logs/agent-monitor.log` |

---

## Stopping and cleanup

### Stop the container (preserves volumes)

```bash
sudo docker compose -f docker/staging/docker-compose.staging.yml down
```

### Stop and wipe volumes (fresh start)

```bash
sudo docker compose -f docker/staging/docker-compose.staging.yml down -v
```

### Remove the built image (forces full rebuild)

```bash
sudo docker compose -f docker/staging/docker-compose.staging.yml down --rmi local
```

---

## Common issues

### Dispatcher never starts / no tmux session

**Symptom:** Container is running but `tmux -L lobster ls` inside shows no sessions.

**Check:** `sudo docker logs lobster-staging` — look for `ERROR` lines.

### Messages to the test bot go unanswered

**Check 1 — LOBSTER_ENV:** Verify `config.staging.env` has `LOBSTER_ENV=production`.

**Check 2 — Cold start:** Wait up to 10 minutes for the dispatcher to finish its bootup sequence.

**Check 3 — Router log:** `sudo docker exec lobster-staging tail -30 /home/lobster/lobster-workspace/logs/router.log`

### Cron jobs not running

**Check 1 — Daemon:** `sudo docker exec lobster-staging systemctl status cron`

**Check 2 — Cron entries:** `sudo docker exec lobster-staging ls /etc/cron.d/` — verify `lobster-staging` is listed.
Then: `sudo docker exec lobster-staging cat /etc/cron.d/lobster-staging` — verify LOBSTER entries are present.

**Check 3 — Journal:** `sudo docker exec lobster-staging journalctl -u cron -f` — look for cron daemon output.

**Note:** If the container was built before this fix was added, rebuild the image:

```bash
sudo docker compose -f docker/staging/docker-compose.staging.yml down --rmi local
sudo docker compose -f docker/staging/docker-compose.staging.yml up -d --build
```

### `claude: not found` inside the container

The host claude binary is bind-mounted at `/home/lobster/.local/bin/claude`. Ensure `claude` is installed on the host at `~/.local/bin/claude`.
