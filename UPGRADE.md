# Upgrading Lobster

If your Lobster installation is more than a few days old, the upgrade script will
bring it fully up to date -- pulling latest code, installing new dependencies,
creating new directories, and restarting services.

## Quick Start

```bash
~/lobster/scripts/upgrade.sh
```

That's it. The script is idempotent and safe to run multiple times.

## What the Upgrade Does

| Step | What | Details |
|------|------|---------|
| 1 | **Backup** | Saves config.env, lobster.conf, tasks.json, jobs.json, CLAUDE.md, and service files to `~/lobster-backups/upgrade-<timestamp>/` |
| 2 | **Git pull** | Fetches and fast-forward merges latest `main`. Stashes local changes if any. |
| 3 | **Python deps** | Updates pip packages in the venv from requirements.txt (or core deps if no requirements.txt). |
| 4 | **New directories** | Creates directories that newer code expects: `~/messages/sent/`, `~/messages/files/`, etc. |
| 5 | **Syncthing** | *Optional, prompted.* Installs Syncthing for LobsterDrop file sharing. |
| 6 | **Playwright** | Installs Playwright + Chromium for the `fetch_page` headless browser tool. |
| 7 | **Systemd** | Reloads systemd if service templates have changed. |
| 8 | **Restart** | Restarts `lobster-router` and `lobster-claude` services. |
| 9 | **Migrations** | Detects and migrates old config formats (`.lobster.env`, Hyperion service names, flat message dirs). |
| 10 | **Health check** | Verifies config, Python packages, directories, services, MCP registration, and Telegram API. |

## Options

```
~/lobster/scripts/upgrade.sh [OPTIONS]

Options:
  --help              Show help and exit
  --dry-run           Preview changes without applying them
  --skip-syncthing    Skip Syncthing/LobsterDrop setup prompt
  --skip-playwright   Skip Playwright/Chromium installation
  --force             Continue past non-critical errors
```

## Common Scenarios

### Preview first, then upgrade

```bash
~/lobster/scripts/upgrade.sh --dry-run
~/lobster/scripts/upgrade.sh
```

### Headless server (no interactive prompts)

```bash
~/lobster/scripts/upgrade.sh --skip-syncthing
```

### Minimal upgrade (code and services only)

```bash
~/lobster/scripts/upgrade.sh --skip-syncthing --skip-playwright
```

## Backups

Every upgrade creates a timestamped backup at:

```
~/lobster-backups/upgrade-YYYYMMDD-HHMMSS/
```

Contents:
- `config.env` - your bot token and API keys
- `lobster.conf` - installation config
- `jobs.json` - scheduled task definitions
- `tasks.json` - task list
- `CLAUDE.md` - your workspace instructions
- `*.service` - installed systemd service files
- `git-commit.txt` - the commit hash before upgrade
- `git-log.txt` - recent commit history

## Rolling Back

If something goes wrong, restore from backup:

```bash
# See available backups
ls ~/lobster-backups/

# Restore a specific backup's config
cp ~/lobster-backups/upgrade-*/lobster/config/config.env ~/lobster/config/config.env

# Or revert git to the pre-upgrade commit
cd ~/lobster
cat ~/lobster-backups/upgrade-*/git-commit.txt  # see the old commit
git checkout <commit-hash>

# Restart services
sudo systemctl restart lobster-router lobster-claude
```

You can also use the existing update script's rollback feature:

```bash
~/lobster/scripts/update-lobster.sh --rollback
```

## New Features After Upgrade

### Conversation History (`get_conversation_history`)
Browse past sent and received messages. Both incoming messages (from `processed/`)
and outgoing replies (from `sent/`) are available with filtering and pagination.

### Headless Browser (`fetch_page`)
Fetches web pages using Playwright/Chromium with full JavaScript rendering.
Works with Twitter/X, SPAs, and other JS-heavy sites. Requires the Playwright
step during upgrade.

### Markdown Rendering
Bot replies now render Markdown formatting in Telegram, with automatic fallback
to plain text if parsing fails.

### File Size Pre-check
Documents over 20MB are rejected before download with a clear error message,
instead of failing silently.

## Troubleshooting

### Playwright install fails
```bash
# Install system dependencies manually
sudo apt-get install -y libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
  libcups2 libdrm2 libdbus-1-3 libxkbcommon0 libatspi2.0-0 libxcomposite1 \
  libxdamage1 libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 libcairo2 \
  libasound2 libwayland-client0

# Then install Chromium
source ~/lobster/.venv/bin/activate
python -m playwright install chromium
```

### Services won't start
```bash
# Check logs
journalctl -u lobster-router --since "5 minutes ago"
journalctl -u lobster-claude --since "5 minutes ago"

# Verify config
cat ~/lobster/config/config.env
```

### Git merge fails
```bash
cd ~/lobster
git status           # see what's conflicting
git stash            # stash your changes
git pull origin main # pull fresh
git stash pop        # re-apply your changes
```
