# Credential Store (config.env)

> **Note:** `global.env` was deprecated in issue #1785 (config consolidation, Option A).
> All credentials and API tokens now belong in `~/lobster-config/config.env`.
> Existing `global.env` files are merged into `config.env` automatically by upgrade.sh migration 79
> and archived as `global.env.bak`. The `lobster env set` command writes to `config.env`.

Lobster provides a standardized location for API tokens and credentials that need
to be shared across multiple services, scripts, and CLI tools on the same machine.

## Location

```
~/lobster-config/config.env
```

This file lives in your private Lobster config directory (`$LOBSTER_CONFIG_DIR`,
which defaults to `~/lobster-config/`). It is **never committed to any repository**
and should have restricted file permissions (`600`).

> **Directory layout note:** Lobster uses two separate directories with distinct purposes:
>
> - `~/lobster-config/` (`$LOBSTER_CONFIG_DIR`) — Private credentials and config overlay.
>   Contains `config.env` (Lobster service config and all API tokens) and private
>   overrides applied during install. Also contains `owner.toml` (identity, preferences,
>   consolidation schedule) and `sync-repos.json` (repos to monitor).
> - `~/lobster-user-config/` (`$LOBSTER_USER_CONFIG`) — User-visible behavioral config.
>   Contains agent bootup files, memory, and context files that shape how Lobster behaves.

## Format

Standard shell `KEY=VALUE` pairs, one per line. No `export` keyword is needed —
the file is sourced by Lobster's shell integration automatically.

```bash
# ~/lobster-config/config.env

# Lobster service configuration
TELEGRAM_BOT_TOKEN=...
TELEGRAM_ALLOWED_USERS=...

# API tokens and credentials
HETZNER_API_TOKEN=your-token-here
GITHUB_TOKEN=ghp_yourtoken
ANTHROPIC_API_KEY=sk-ant-...
```

Comments (lines starting with `#`) and blank lines are ignored.

## Purpose

`config.env` is the **single canonical file** for both Lobster service configuration
and machine-wide API credentials. This consolidation (issue #1785) replaced the previous
two-file arrangement (`config.env` + `global.env`) to reduce confusion about which file
is authoritative.

## Usage

### Setting a token

```bash
lobster env set HETZNER_API_TOKEN your-token-here
```

### Getting a token value

```bash
lobster env get HETZNER_API_TOKEN
```

### Listing all stored keys (values are hidden for security)

```bash
lobster env list
```

### Editing directly

```bash
$EDITOR ~/lobster-config/config.env
```

## Shell Integration

The installer adds a snippet to `~/.bashrc` (and `~/.zshrc` if present) that
sources `config.env` on every login. This makes all stored tokens available as
environment variables to any script or CLI tool running in your shell session.

Lobster's systemd services also load `config.env` via `EnvironmentFile=` so tokens
are available to background services without any extra steps.

## Security

- File permissions are set to `600` (owner read/write only) during install
- `lobster env list` never prints values, only key names
- The file is excluded from git via `.gitignore` patterns in the private config repo
- Store only credentials for services you personally control on this machine

## Common Keys

| Key | Service | Where to get it |
|-----|---------|-----------------|
| `HETZNER_API_TOKEN` | Hetzner Cloud | https://console.hetzner.cloud → Security → API Tokens |
| `GITHUB_TOKEN` | GitHub | https://github.com/settings/tokens |
| `ANTHROPIC_API_KEY` | Anthropic | https://console.anthropic.com/settings/keys |
| `TWILIO_ACCOUNT_SID` | Twilio | https://console.twilio.com |
| `TWILIO_AUTH_TOKEN` | Twilio | https://console.twilio.com |
| `OPENAI_API_KEY` | OpenAI | https://platform.openai.com/api-keys |
| `CLOUDFLARE_API_TOKEN` | Cloudflare | https://dash.cloudflare.com/profile/api-tokens |
| `VERCEL_TOKEN` | Vercel | https://vercel.com/account/tokens |
| `DO_TOKEN` | DigitalOcean | https://cloud.digitalocean.com/account/api/tokens |

## Lobster Behavior Keys

| Key | Values | Default | Description |
|-----|--------|---------|-------------|
| `LOBSTER_TOOL_PREFERENCE` | `cli_first` | `cli_first` | Controls how Lobster and its subagents interact with external services. When set to `cli_first`, Lobster will always prefer an installed CLI (`gh`, `vercel`, `docker`, etc.) over raw API calls (curl/fetch). Only falls back to raw API calls if the CLI cannot accomplish the task. |

### `LOBSTER_TOOL_PREFERENCE=cli_first`

This is the default and recommended setting. With this setting active:

- `gh` is used for all GitHub operations instead of raw API calls
- `vercel` is used for Vercel deployments and configuration instead of the Vercel REST API
- `docker` is used for container management instead of the Docker daemon API
- `git` is used for version control instead of raw file manipulation
- Any other installed CLI is preferred over its corresponding API

**Why this matters:** CLIs handle authentication automatically using pre-configured credentials,
produce human-readable error messages, and provide stable interfaces designed for scripting.
Raw API calls require manual credential management, request construction, and pagination handling.
