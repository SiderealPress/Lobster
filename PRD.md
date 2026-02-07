# Lobster - Product Requirements Document

## Overview

Lobster is a secure, self-hosted personal server environment for running an always-on Claude Code session with Telegram integration. Claude maintains persistent context across all messages, running in an infinite loop that blocks until new messages arrive.

## Problem Statement

Running Claude Code as a persistent agent with messaging reach-back requires significant manual configuration: the session must survive SSH disconnects, maintain conversation context indefinitely, and integrate with messaging platforms. Users need a reproducible, secure setup that "just works."

## Solution

A systemd-managed always-on Claude Code session with:

- **Always-on Claude session** running in tmux, never exiting
- **Telegram Bot integration** for receiving/sending messages
- **Blocking message queue** - Claude calls `wait_for_messages` and blocks until messages arrive
- **Persistent context** - conversation history maintained across all interactions
- **Security hardening** via UFW firewall, fail2ban, and SSH lockdown

## Core Features

| Feature | Description |
|---------|-------------|
| Always-on Claude | Persistent Claude Code session in tmux via systemd |
| Telegram Bot | Receive/send messages through Telegram Bot API |
| Blocking inbox | `wait_for_messages` tool blocks until new messages arrive (inotify) |
| Voice messages | Audio transcription via OpenAI Whisper API |
| tmux session | Claude runs in `lobster` tmux session, attachable for debugging |
| Security | Firewall, intrusion prevention, SSH hardening |

> **Future integrations:** See [docs/FUTURE.md](docs/FUTURE.md) for planned Signal and Twilio SMS support.

## Target Users

- Developers wanting persistent AI agents with messaging reach-back
- Users who want Claude to maintain context across all conversations
- Power users comfortable with terminal-based workflows

## Non-Goals

- GUI/web interface
- Multi-user support
- Container orchestration
- Multiple Claude instances

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                      Lobster System                         │
├─────────────────────────────────────────────────────────────┤
│  systemd target: lobster.target                            │
│  ├── lobster-claude.service (always-on Claude in tmux)    │
│  └── lobster-router.service (Telegram bot)                │
├─────────────────────────────────────────────────────────────┤
│  Claude's Main Loop (runs forever)                          │
│  ┌──────────────────────────────────────────────────────┐  │
│  │  while True:                                          │  │
│  │      messages = wait_for_messages()  # blocks here   │  │
│  │      for msg in messages:                             │  │
│  │          response = process(msg)                      │  │
│  │          send_reply(msg.chat_id, response)            │  │
│  │          mark_processed(msg.id)                       │  │
│  └──────────────────────────────────────────────────────┘  │
├─────────────────────────────────────────────────────────────┤
│  MCP Server: lobster-inbox                                 │
│  ├── wait_for_messages() - blocks until inbox has messages │
│  ├── check_inbox() - list pending messages                 │
│  ├── send_reply() - write to outbox                        │
│  └── mark_processed() - move to processed/                 │
├─────────────────────────────────────────────────────────────┤
│  File-based Message Queue                                   │
│  ├── ~/messages/inbox/    (incoming JSON files)            │
│  ├── ~/messages/outbox/   (outgoing JSON files)            │
│  ├── ~/messages/processed/ (handled messages)              │
│  └── ~/messages/audio/    (voice message files)            │
├─────────────────────────────────────────────────────────────┤
│  Security Layer                                             │
│  ├── UFW (SSH only by default)                             │
│  ├── fail2ban (brute-force prevention)                     │
│  └── SSH hardening (no root, key-only)                     │
└─────────────────────────────────────────────────────────────┘
```

## Message Flow

1. User sends Telegram message (text or voice)
2. `lobster-router` (Telegram bot) writes JSON to `~/messages/inbox/`
3. Voice messages: bot downloads audio, transcribes via OpenAI Whisper
4. Claude's `wait_for_messages()` returns with new messages
5. Claude processes message, calls `send_reply(chat_id, text)`
6. Reply JSON written to `~/messages/outbox/`
7. Bot's outbox watcher sends reply to Telegram
8. Claude calls `mark_processed(message_id)`, loops back to `wait_for_messages()`

## Quick Start

```bash
# Run installer
bash setup.sh

# Start services
sudo systemctl start lobster.target

# Attach to Claude session (for debugging)
tmux -L lobster attach -t lobster

# View logs
journalctl -u lobster-claude -u lobster-router -f
```

## Key Files

| File | Purpose |
|------|---------|
| `services/lobster-claude.service` | Always-on Claude session |
| `services/lobster-router.service` | Telegram bot |
| `services/lobster.target` | Systemd target grouping both services |
| `scripts/claude-wrapper.exp` | Expect script to auto-accept permissions dialog |
| `src/mcp/inbox_server.py` | MCP server with message queue tools |
| `src/bot/lobster_bot.py` | Telegram bot with voice transcription |
