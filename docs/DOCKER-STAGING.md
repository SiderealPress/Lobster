# Docker Staging Runbook

The staging Docker environment spins up a full live Lobster instance connected to a real Telegram bot for manual end-to-end testing. Unlike the [Docker testing environment](DOCKER-TESTING.md) (which uses a mock Telegram server), staging talks to the real Telegram API via a dedicated test bot: **@Lobstertown_test_bot**.

## When to use it

Use staging when:
- Manually testing a feature end-to-end before merging
- Verifying Telegram bot behavior (formatting, button interactions, threading)
- Checking dispatcher startup, MCP server launch, and routing in a realistic environment
- Reproducing a bug that only appears under live conditions

Do not use staging for:
- Automated regression testing (use Docker Testing / `docker-compose.test.yml` instead)
- Production deployment (staging uses a test bot token, not the production token)

---

## Architecture

```
Host machine
  ~/.claude/              ← bind-mounted read-write into container
  ~/.local/bin/claude     ← bind-mounted read-only (reuses host binary)
  ~/lobster-config/config.staging.env  ← env vars injected at compose up

Container: lobster-staging
  /home/lobster/lobster/              ← repo (baked into image at build time)
  /home/lobster/messages/             ← Docker volume (lobster-staging-messages)
  /home/lobster/lobster-workspace/    ← Docker volume (lobster-staging-workspace)
  /home/lobster/lobster-config/       ← written by entrypoint from env vars

Inside container:
  tmux session "lobster" (socket: /tmp/tmux-*/lobster)
    └── dispatcher (Claude Code + MCP server)
  lobster_bot.py            ← Telegram router process (background)
```

Credentials are **bind-mounted from the host** — they are never baked into the image. This is intentional; see [Credential pattern](#credential-pattern) below.

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

Build the image and start the container in the background:

```bash
cd ~/lobster
sudo docker compose -f docker/staging/docker-compose.staging.yml up -d --build
```

### Subsequent runs (no Dockerfile changes)

Skip the build step:

```bash
sudo docker compose -f docker/staging/docker-compose.staging.yml up -d
```

---

## LOBSTER_ENV gotcha

**`LOBSTER_ENV` must be set to `production`, not `staging`.**

This is counter-intuitive. The env var does not mean "which environment am I running in" — it controls which dispatcher mode is active. Setting `LOBSTER_ENV=staging` causes the dispatcher to exit immediately without starting the message loop. The dispatcher only runs in `production` mode.

The staging Docker container is a *staging instance of the production dispatcher* — it uses the test bot token but must run the production dispatcher loop.

Symptom if misconfigured: the container starts, the router connects to Telegram, but no Claude session appears in tmux, and messages to the test bot go unanswered.

Fix: ensure `config.staging.env` contains `LOBSTER_ENV=production`.

---

## Credential pattern

Credentials are **never copied into the Docker image**. Instead, the host's `~/.claude/` directory is bind-mounted into the container at the same path:

```yaml
volumes:
  - /home/lobster/.claude:/home/lobster/.claude
```

This means:
- The container reads Claude auth tokens, `settings.json`, and MCP config from the host.
- The entrypoint script updates `settings.json` inside the container to point MCP commands at the correct in-container paths — this is why `~/.claude/` cannot be read-only.
- Rotating credentials on the host immediately affects the container on next restart.
- Never add `COPY ~/.claude` or similar to `Dockerfile.staging`.

---

## Cold start time

The first time the container starts (or after a workspace volume wipe), the dispatcher takes approximately **10 minutes** to become responsive. During this time it is:

1. Reading bootup context files
2. Initializing the MCP server
3. Completing the dispatcher loop startup sequence

The container and router are running during this window — you can tail logs immediately. Just wait before sending test messages.

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

You should see the Claude Code dispatcher session. Press `Ctrl-b d` to detach without stopping it.

### 3. Check MCP server startup in docker logs

```bash
sudo docker logs lobster-staging
```

Look for lines like `[staging] Router started` and `[staging] Claude session started in tmux`.

### 4. Tail the MCP server log

```bash
sudo docker exec lobster-staging tail -f /home/lobster/lobster-workspace/logs/mcp-server.log
```

### 5. Capture the current tmux pane (non-interactive)

```bash
sudo docker exec lobster-staging tmux -L lobster capture-pane -pt lobster
```

---

## Tailing logs

| What | Command |
|------|---------|
| Router (Telegram bot) | `sudo docker exec lobster-staging tail -f /home/lobster/lobster-workspace/logs/router.log` |
| Claude session | `sudo docker exec lobster-staging tail -f /home/lobster/lobster-workspace/logs/claude.log` |
| MCP server | `sudo docker exec lobster-staging tail -f /home/lobster/lobster-workspace/logs/mcp-server.log` |
| Both router + claude (compose) | `sudo docker compose -f docker/staging/docker-compose.staging.yml logs -f` |

---

## Stopping and cleanup

### Stop the container (preserves volumes)

```bash
sudo docker compose -f docker/staging/docker-compose.staging.yml down
```

The `lobster-staging-messages` and `lobster-staging-workspace` volumes persist. This is intentional — it preserves dispatcher state across restarts.

### Stop and wipe volumes (fresh start)

```bash
sudo docker compose -f docker/staging/docker-compose.staging.yml down -v
```

Use this when you want a completely clean slate (e.g., testing a fresh install flow or recovering from a corrupted workspace).

### Remove the built image (forces full rebuild)

```bash
sudo docker compose -f docker/staging/docker-compose.staging.yml down --rmi local
```

---

## Common issues

### Dispatcher never starts / no tmux session

**Symptom:** `sudo docker ps` shows the container running, but `tmux -L lobster ls` inside the container shows no sessions.

**Most likely cause:** The `entrypoint-staging.sh` crashed early, or the router process failed health check and the entrypoint exited.

**Check:** `sudo docker logs lobster-staging` — look for `ERROR` lines near the top.

### Messages to the test bot go unanswered

**Check 1 — LOBSTER_ENV:** Verify `config.staging.env` has `LOBSTER_ENV=production`. If it was set to `staging`, the dispatcher exited without starting the loop.

**Check 2 — Cold start:** If the container just started, wait up to 10 minutes for the dispatcher to finish its bootup sequence.

**Check 3 — Router log:** `sudo docker exec lobster-staging tail -30 /home/lobster/lobster-workspace/logs/router.log` — confirm the bot connected to Telegram without error.

### Stale workspace volume causing startup errors

The `lobster-staging-workspace` volume persists between container runs. Occasionally stale state (e.g., an in-progress message left in `messages/processing/`) can cause the dispatcher to behave unexpectedly on restart.

**Fix:** Wipe and restart with fresh volumes:

```bash
sudo docker compose -f docker/staging/docker-compose.staging.yml down -v
sudo docker compose -f docker/staging/docker-compose.staging.yml up -d --build
```

### `claude: not found` inside the container

The host claude binary is bind-mounted at `/home/lobster/.local/bin/claude`. If the host binary is not present or the mount fails, the tmux session will fail immediately.

**Fix:** Ensure `claude` is installed on the host at `~/.local/bin/claude` before starting the container.

### settings.json permission error

The entrypoint script updates `~/.claude/settings.json` to rewrite MCP server paths for the container environment. If the host's `~/.claude/` is owned by root or has restrictive permissions, this write will fail.

**Fix:** Ensure `~/.claude/` and `~/.claude/settings.json` are owned by the `lobster` user (uid 1000) on the host.
