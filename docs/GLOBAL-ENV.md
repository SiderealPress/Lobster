# Global Environment Store

Lobster provides a standardized location for API tokens and credentials that need
to be shared across multiple services, scripts, and CLI tools on the same machine.

## Location

```
~/lobster-config/global.env
```

This file lives in your personal Lobster config directory (`$LOBSTER_CONFIG_DIR`,
which defaults to `~/lobster-config/`). It is **never committed to any repository**
and should have restricted file permissions (`600`).

## Format

Standard shell `KEY=VALUE` pairs, one per line. No `export` keyword is needed —
the file is sourced by Lobster's shell integration automatically.

```bash
# ~/lobster-config/global.env

HETZNER_API_TOKEN=your-token-here
GITHUB_TOKEN=ghp_yourtoken
ANTHROPIC_API_KEY=sk-ant-...
```

Comments (lines starting with `#`) and blank lines are ignored.

## Purpose

`config.env` holds Lobster-specific configuration (Telegram tokens, feature flags,
etc.). `global.env` holds machine-wide API credentials that multiple services or
scripts might need — Hetzner, GitHub, Anthropic, Twilio, etc.

The separation keeps concerns distinct:
- `config.env` — Lobster service configuration
- `global.env` — API keys and credentials for any tool on this machine

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
$EDITOR ~/lobster-config/global.env
```

## Shell Integration

The installer adds a snippet to `~/.bashrc` (and `~/.zshrc` if present) that
sources `global.env` on every login. This makes all stored tokens available as
environment variables to any script or CLI tool running in your shell session.

Lobster's systemd services also load `global.env` via `EnvironmentFile=` so tokens
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

## Migration

If you have tokens currently in `config.env` that are not Lobster-specific (e.g.,
`HCLOUD_TOKEN`, `GITHUB_TOKEN`), you can move them to `global.env`:

```bash
# Move a token from config.env to global.env
lobster env set HETZNER_API_TOKEN "$(grep HCLOUD_TOKEN ~/lobster-config/config.env | cut -d= -f2)"
# Then remove it from config.env manually
```
