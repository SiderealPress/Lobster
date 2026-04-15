# Slack Connector

Full Slack integration for Lobster: dumb ingress logging, per-channel configuration, trigger automations, and log analysis.

## What it does

- **Dumb ingress logging** — Every Slack message is written to disk at wire speed before any LLM processing
- **Per-channel config** — Fine-grained control over which channels Lobster monitors and what it does in each
- **Trigger automations** — Rule-based automations fired by Slack events (keyword matches, reactions, file uploads)
- **Log analysis** — Search, summarize, and analyze Slack history on demand

## Commands

| Command | Description |
|---------|-------------|
| `/slack` | Slack connector status and quick actions |
| `/slack-status` | Show connection health, channel list, log stats |
| `/analyze-logs` | Search and analyze Slack message logs |

## Setup

```bash
bash ~/lobster/lobster-shop/slack-connector/install.sh
```

The installer will:
1. Install Python dependencies (slack-bolt, slack-sdk, watchdog)
2. Create runtime directories under `~/lobster-workspace/slack-connector/`
3. Copy example configs
4. Check for required API tokens in `~/lobster-config/config.env`

## Required tokens

| Variable | Type | Purpose |
|----------|------|---------|
| `LOBSTER_SLACK_BOT_TOKEN` | `xoxb-*` | Bot user OAuth token for API calls |
| `LOBSTER_SLACK_APP_TOKEN` | `xapp-*` | App-level token for Socket Mode |

## Configuration

Channel-level configuration lives in `~/lobster-workspace/slack-connector/config/channels.yaml`. See `config/channels.yaml.example` for the format.

## Phases

- **Phase 1:** Research & design (complete)
- **Phase 2:** Skill scaffold (this phase)
- **Phase 3:** Dumb ingress logging worker
- **Phase 4:** Log indexing and search
- **Phase 5:** Trigger automation framework
- **Phase 6:** Morning briefing integration
