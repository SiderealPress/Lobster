# Project: Lobster

*Auto-updated by nightly consolidation.*

## Description

Always-on AI assistant built on Claude Code, processing messages from Telegram and other channels.

## Repository

https://github.com/SiderealPress/Lobster

## Status

Active development

## Key Components

- MCP inbox server (`src/mcp/inbox_server.py`)
- Telegram bot (`lobster_bot.py`)
- Reliability layer (`reliability.py`)
- Calendar integration (`calendar_integration.py`)
- Memory system (`src/mcp/memory/`)
- Scheduled tasks system
- Brain dumps processing

## Recent Work

- Three-layer memory system implementation (Layer 1 complete, Layer 2 in progress)
- Reliability improvements (atomic writes, circuit breakers)
- Google Calendar integration
- Headless browser fetch capability
- Slack integration

## Next Steps

- Complete Layer 2: nightly consolidation
- Layer 3: canonical file maintenance and retrieval
- Additional messaging channel support

## Blockers

*No current blockers.*
