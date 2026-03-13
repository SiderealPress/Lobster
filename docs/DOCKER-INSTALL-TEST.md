# Docker Install-Test Environment

A local Docker environment for testing `install.sh` end-to-end on a clean
Debian system. Use this before merging changes that touch the installer,
service templates, or system dependencies.

This is a **manual testing tool**, not a CI step. It gives you a disposable
VPS-shaped container you can poke around in, nuke, and restart cleanly.

---

## What this is NOT

- It is not the automated integration test suite (`tests/docker/docker-compose.test.yml`).
- It does not mock anything. It runs the real install.sh against the real network.
- It does not run unattended. You watch it, you poke it, you verify things look right.

---

## Files

| File | Purpose |
|---|---|
| `tests/docker/Dockerfile.install-test` | Base image: clean Debian + sudo + curl. Nothing else. |
| `tests/docker/docker-compose.install-test.yml` | Compose file with volume mounts and env var forwarding. |

---

## Prerequisites

- Docker and Docker Compose installed on your dev machine.
- The environment variables listed below set in your shell.

---

## Step 1: Set your environment variables

The container never hardcodes secrets. Set them in your shell before running
any compose commands:

```bash
# Required for a real end-to-end test
export TELEGRAM_BOT_TOKEN="123456:ABC-your-bot-token"
export TELEGRAM_ALLOWED_USERS="your_telegram_user_id"
export ANTHROPIC_API_KEY="sk-ant-..."
export GITHUB_TOKEN="ghp_..."

# Optional: path to your local ~/.claude directory for Claude Code auth
# If set, the directory is mounted read-only into the container so you
# do not need to re-authenticate Claude Code inside the container.
export CLAUDE_CONFIG_VOLUME="$HOME/.claude"
```

If you only want to test the installer up to the point where it would launch
Claude Code (and skip the Claude auth step), you can leave `CLAUDE_CONFIG_VOLUME`
unset and authenticate manually inside the container after install.sh runs.

---

## Step 2: Build the base image

Only needed once, or when `Dockerfile.install-test` changes:

```bash
docker compose -f tests/docker/docker-compose.install-test.yml build
```

This takes under a minute. It installs nothing beyond `sudo`, `curl`, and
`ca-certificates` — everything else is left for install.sh.

---

## Step 3: Start a fresh container

### Option A: Run-and-remove (simplest)

Drops you into a shell. Container is deleted when you exit.

```bash
docker compose -f tests/docker/docker-compose.install-test.yml run --rm install-test
```

### Option B: Keep alive for repeated exec

Start the container in the background:

```bash
docker compose -f tests/docker/docker-compose.install-test.yml up -d
```

Then exec into it from your terminal (or from multiple terminals):

```bash
docker exec -it lobster-install-test bash
```

---

## Step 4: Run install.sh inside the container

The repo is already mounted at `~/lobster`. From inside the container:

```bash
# Interactive install (recommended for first-time testing):
cd ~/lobster && bash install.sh

# Non-interactive install (reads everything from env vars):
cd ~/lobster && bash install.sh --non-interactive
```

`--non-interactive` skips all prompts and uses the environment variables
forwarded by compose (`TELEGRAM_BOT_TOKEN`, `ANTHROPIC_API_KEY`, etc.).

Watch the output carefully. A successful run ends with a "Lobster installed
successfully" message and starts the systemd-compatible services (or tmux
sessions, depending on whether the container has systemd).

Note: Docker containers do not run systemd by default. install.sh detects
this and falls back to direct process management. If you specifically want to
test the systemd service files, you need a systemd-enabled container image
(not covered here — see `docs/LOCAL-INSTALL.md` for VM-based testing instead).

---

## Step 5: Verify the installation

After install.sh completes, check key things manually:

```bash
# Python environment
~/lobster/.venv/bin/python --version
~/lobster/.venv/bin/python -c "import mcp; print('MCP OK')"

# uv
~/.local/bin/uv --version

# Claude Code (requires CLAUDE_CONFIG_VOLUME or manual auth)
claude --version

# gh CLI
gh --version
gh auth status   # needs GITHUB_TOKEN in env

# Directory structure
ls ~/lobster-workspace/
ls ~/messages/

# Config
cat ~/lobster-workspace/config/config.env  2>/dev/null || \
cat ~/lobster-config/config.env            2>/dev/null || \
echo "No config.env found — check install output"

# MCP servers registered with Claude Code
cat ~/.claude/claude_desktop_config.json 2>/dev/null | python -m json.tool
```

---

## Step 6: Inject Claude Code auth (if not using volume mount)

If you did not set `CLAUDE_CONFIG_VOLUME`, authenticate Claude Code inside the
container:

```bash
claude auth login
# Follow the browser OAuth flow shown in the terminal output
```

Or copy your credentials directly from outside the running container:

```bash
# From your host machine (container must be running, not the --rm variant):
docker cp ~/.claude lobster-install-test:/home/lobster/.claude
```

---

## Wipe and restart cleanly

Kill the named container and all its volumes, then start fresh:

```bash
docker compose -f tests/docker/docker-compose.install-test.yml down -v
docker compose -f tests/docker/docker-compose.install-test.yml up -d
docker exec -it lobster-install-test bash
```

This gives you a completely fresh Debian system. The image is reused (no
rebuild needed) because nothing is baked into it that changes between runs.

To also force a full image rebuild:

```bash
docker compose -f tests/docker/docker-compose.install-test.yml down -v
docker compose -f tests/docker/docker-compose.install-test.yml build --no-cache
docker compose -f tests/docker/docker-compose.install-test.yml up -d
```

---

## What to test

### Before merging install.sh changes

- Run the full install on a clean container.
- Verify every step in the output shows `[OK]` or expected warnings.
- Check that the directory structure matches `install.sh`'s expectations.
- Verify the Python venv is created with `uv`, not bare `pip`.
- Verify MCP servers are registered in `~/.claude/claude_desktop_config.json`.

### Before merging service file changes

- Run install, then inspect the generated service files:
  ```bash
  cat ~/lobster-workspace/services/lobster-router.service
  cat ~/lobster-workspace/services/lobster-worker.service
  ```
- Verify the `{{USER}}`, `{{HOME}}`, `{{INSTALL_DIR}}` placeholders were all
  substituted correctly.

### Before merging changes to system dependency lists

- Run install on a clean container and verify the new packages install cleanly.
- Watch for apt errors, GPG key issues, or missing packages.

### Testing the `--non-interactive` path

```bash
NON_INTERACTIVE=true \
docker compose -f tests/docker/docker-compose.install-test.yml run --rm install-test \
    bash /home/lobster/lobster/install.sh --non-interactive
```

This is closest to what a CI environment would do if we ever automate this.

### Testing the update path

After a successful install, you can test `install.sh` run again as an update:

```bash
# Inside the container, after initial install:
bash ~/lobster/install.sh --non-interactive
# Should detect existing install and run the update path, not re-install everything
```

---

## Troubleshooting

**`/dev/null` bind mount error on CLAUDE_CONFIG_VOLUME**

If `CLAUDE_CONFIG_VOLUME` is unset, compose falls back to `/dev/null` as the
bind source. On some Docker versions this causes an error. Just set the variable:

```bash
export CLAUDE_CONFIG_VOLUME="$HOME/.claude"
```

Or if you genuinely do not want to mount credentials, comment out the
`~/.claude` volume block in `docker-compose.install-test.yml` entirely.

**install.sh exits with "not interactive" error**

install.sh requires either a TTY or `--non-interactive`. Make sure you are
using `run` (which allocates a TTY) rather than `exec`, or pass the flag:

```bash
docker compose ... run --rm install-test bash install.sh --non-interactive
```

**Claude Code not found after install**

Claude Code is installed by install.sh via npm. Check:

```bash
which claude || ls ~/.local/bin/claude || ls /usr/local/bin/claude
node --version   # must be Node 18+
npm list -g @anthropic-ai/claude-code 2>/dev/null
```

If Node is missing, install.sh may have failed silently during the Node.js
install step. Rerun with `bash -x install.sh` to see every command.

**systemd services not starting**

Expected in Docker. The container does not have systemd. Services either run
via tmux or need to be started manually:

```bash
bash ~/lobster/scripts/start-lobster.sh
```

For proper systemd testing, use a VM (see `docs/LOCAL-INSTALL.md`).
