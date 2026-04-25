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

# Ensure GITHUB_TOKEN is set in your shell (see GitHub Auth section below)
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

## GitHub Auth — IMPORTANT

### Why `docker-compose` env passthrough is not enough

`docker-compose` injects `GITHUB_TOKEN` into the environment of **PID 1** (the
entrypoint script). That variable is available in the entrypoint shell but is
**not propagated to Claude Code subprocesses** — subagents, scheduled tasks, and
anything Claude Code forks internally will not see it.

This causes `gh` CLI commands to fail with authentication errors inside Claude
Code sessions even though the token appears to be set at the container level.

### The fix: all 4 layers

Run these commands **inside the running container** after the first start:

```bash
sudo docker exec -it lobster-staging bash
```

Then, inside the container (replace `ghp_...` with your actual token):

```bash
GITHUB_TOKEN=ghp_...

# Layer 1: /etc/environment — available system-wide across all shells and tmux sessions
echo "GITHUB_TOKEN=${GITHUB_TOKEN}" | sudo tee -a /etc/environment
echo "GH_TOKEN=${GITHUB_TOKEN}" | sudo tee -a /etc/environment

# Layer 2: gh auth login — writes persistent credentials to ~/.config/gh/hosts.yml
#           This is the most reliable fix; survives shell relaunches.
echo "${GITHUB_TOKEN}" | gh auth login --with-token

# Layer 3: shell profiles — picked up by interactive and login shells
echo "export GITHUB_TOKEN=${GITHUB_TOKEN}" >> ~/.bashrc
echo "export GH_TOKEN=${GITHUB_TOKEN}" >> ~/.bashrc
echo "export GITHUB_TOKEN=${GITHUB_TOKEN}" >> ~/.profile
echo "export GH_TOKEN=${GITHUB_TOKEN}" >> ~/.profile

# Layer 4: Lobster config — sourced when Lobster reads its config
echo "GITHUB_TOKEN=${GITHUB_TOKEN}" >> ~/lobster-config/config.env
echo "GH_TOKEN=${GITHUB_TOKEN}" >> ~/lobster-config/config.env
```

Verify auth is working:

```bash
gh auth status
```

### Why all 4 layers?

| Layer | What it covers |
|---|---|
| `/etc/environment` | All processes, all shells, tmux sessions |
| `gh auth login` | `gh` CLI itself, regardless of env vars |
| `.bashrc` / `.profile` | Interactive and login shell sessions |
| `config.env` | Lobster-sourced config at startup |

Docker env passthrough alone only covers the entrypoint process. The `gh auth login`
step (layer 2) is the most durable fix because it writes a persistent auth file
(`~/.config/gh/hosts.yml`) that `gh` reads regardless of environment variables.

### Tracking issue

See GitHub issue #1798 for full context and planned automation of this setup step.

---

## Troubleshooting

### `gh` commands fail inside Claude Code / subagents

Run `sudo docker exec -it lobster-staging bash` and follow the GitHub Auth
setup above. Check `gh auth status` to confirm.

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
