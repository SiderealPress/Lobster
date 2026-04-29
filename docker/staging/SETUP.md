# Lobster Staging — Setup Guide

This document covers how to start the staging container and configure it correctly.
For Docker testing (unit/integration tests with a mock Telegram server), see
`docs/DOCKER-TESTING.md` instead.

---

## What the staging container is

The staging instance runs a full live Lobster runtime against the real Telegram API
using a dedicated test bot token. It is isolated from production via separate
Docker volumes and a separate config file (`config.staging.env`).

---

## One-time credential setup

```bash
cp ~/lobster/config/config.staging.env.example ~/lobster-config/config.staging.env
```

Edit `~/lobster-config/config.staging.env` and fill in:

- `TELEGRAM_BOT_TOKEN` — use the **test bot token**, never the production one
- `TELEGRAM_ALLOWED_USERS` — comma-separated Telegram user IDs to allow

**Never put the production `TELEGRAM_BOT_TOKEN` in `config.staging.env`.**

---

## Starting the container

```bash
cd ~/lobster

# Export GITHUB_TOKEN in your host shell to enable gh CLI inside the container.
# The container init script automatically wires it into all 4 auth layers.
export GITHUB_TOKEN=ghp_...

sudo docker compose -f docker/staging/docker-compose.staging.yml up -d
```

---

## Attaching to the Claude session

```bash
sudo docker exec -it lobster-staging tmux -L lobster attach -t lobster
```

---

## Viewing logs

```bash
sudo docker compose -f docker/staging/docker-compose.staging.yml logs -f
```

---

## Stopping

```bash
sudo docker compose -f docker/staging/docker-compose.staging.yml down
```

---

## GitHub Auth

### How it works

`lobster-container-init.sh` automatically wires `GITHUB_TOKEN` into all 4 auth
layers at container boot, so `gh` CLI works in every subprocess context —
systemd services, tmux sessions, and Claude Code subagents.

| Layer | Location | What it covers |
|---|---|---|
| 1 | `/etc/environment` | All processes, all shells, tmux sessions |
| 2 | `gh auth login` → `~/.config/gh/hosts.yml` | `gh` CLI itself, regardless of env vars |
| 3 | `~/.bashrc` / `~/.profile` | Interactive and login shell sessions |
| 4 | `config.env` | Lobster-sourced config at startup |

All 4 layers are written on every boot and are idempotent — container restarts
do not accumulate duplicate entries.

### Setup

Set `GITHUB_TOKEN` in **either** of these places (not both required):

**Option A — host shell (recommended):**
```bash
export GITHUB_TOKEN=ghp_...
sudo docker compose -f docker/staging/docker-compose.staging.yml up -d
```

**Option B — config.staging.env:**
```bash
# ~/lobster-config/config.staging.env
GITHUB_TOKEN=ghp_...
```

### Verifying auth

```bash
sudo docker exec -it lobster-staging bash -l -c "gh auth status"
```

### Why docker-compose env passthrough alone is not enough

Docker injects variables into PID 1 (systemd). Subprocesses launched later —
tmux sessions, Claude Code forks, systemd service workers — do not inherit
variables that aren't written to a persistent location. The 4-layer setup in
`lobster-container-init.sh` propagates the token into all durable locations so
every spawned process can authenticate.

---

## Troubleshooting

### `gh` commands fail inside Claude Code / subagents

Check that `GITHUB_TOKEN` was set when the container started:

```bash
sudo docker exec lobster-staging bash -l -c "gh auth status"
```

If not authenticated, restart the container with `GITHUB_TOKEN` exported:

```bash
sudo docker compose -f docker/staging/docker-compose.staging.yml down
export GITHUB_TOKEN=ghp_...
sudo docker compose -f docker/staging/docker-compose.staging.yml up -d
```

### Router fails to start

```bash
sudo docker exec lobster-staging tail -50 /home/lobster/lobster-workspace/logs/router.log
```

### Claude session not starting

```bash
sudo docker exec lobster-staging tail -50 /home/lobster/lobster-workspace/logs/claude.log
```

### Full rebuild

```bash
sudo docker compose -f docker/staging/docker-compose.staging.yml down
sudo docker compose -f docker/staging/docker-compose.staging.yml build --no-cache
sudo docker compose -f docker/staging/docker-compose.staging.yml up -d
```
