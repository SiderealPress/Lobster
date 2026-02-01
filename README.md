# Hyperion

Always-on Claude Code message processor with Telegram integration.

## One-Line Install

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/SiderealPress/hyperion/main/install.sh)
```

## Overview

Hyperion transforms a server into an always-on Claude Code hub that:

- **Processes messages 24/7** via Telegram
- **Maintains persistent context** across restarts
- **Auto-restarts on failure** via systemd
- **Provides unified CLI** for management

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                ALWAYS-ON CLAUDE (tmux)                      │
│         Long-running Claude Code session in tmux            │
│         Blocks on wait_for_messages() - infinite loop       │
│                                                             │
│   MCP Server: hyperion-inbox                                │
│   - Message queue management                                │
│   - Task tracking                                           │
│   - Scheduled job management                                │
└─────────────────────────────────────────────────────────────┘
                              ↑↓
               ~/messages/inbox/ ←→ ~/messages/outbox/
                              ↑↓
┌─────────────────────────────────────────────────────────────┐
│              TELEGRAM BOT (hyperion-router)                 │
│   Writes incoming messages to inbox                         │
│   Watches outbox and sends replies                          │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│              SCHEDULED TASKS (Cron)                         │
│   Automated jobs run on schedule                            │
│   Each job spawns a fresh Claude instance                   │
│   Outputs go to ~/messages/task-outputs/                    │
└─────────────────────────────────────────────────────────────┘
```

## Prerequisites

- Debian 12+ or Ubuntu 22.04+
- Claude Code authenticated (Max subscription)
- Telegram bot token (from @BotFather)
- Your Telegram user ID (from @userinfobot)

## Manual Install

```bash
git clone https://github.com/SiderealPress/hyperion.git
cd hyperion
bash install.sh
```

## Local Installation (VM + Tailscale)

Want to run Hyperion on your local machine instead of a cloud server? You can run it inside a VM with Tailscale Funnel for internet access:

1. Create a Debian 12 VM (UTM, VirtualBox, or VMware)
2. Install Tailscale and authenticate
3. Run the standard `install.sh`
4. Enable Tailscale Funnel

See [docs/LOCAL-INSTALL.md](docs/LOCAL-INSTALL.md) for the full step-by-step guide.

## Configuration

### Quick Start (Default Settings)

For most users, no configuration is needed:

```bash
./install.sh
```

The installer prompts for required credentials (Telegram bot token, user ID) and uses sensible defaults for everything else.

### Custom Installation

For custom paths or settings:

1. Copy the example configuration:
   ```bash
   cp config/hyperion.conf.example config/hyperion.conf
   ```

2. Edit `config/hyperion.conf` with your settings

3. Run the installer:
   ```bash
   ./install.sh
   ```

### Private Configuration Repository

For advanced users who want to keep customizations in a separate repo:

```bash
# Set your private config directory
export HYPERION_CONFIG_DIR=~/hyperion-config

# Run installer
./install.sh
```

See [docs/CUSTOMIZATION.md](docs/CUSTOMIZATION.md) for detailed documentation on:
- Setting up a private config repository
- Creating custom agents
- Defining scheduled tasks
- Writing installation hooks

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `HYPERION_CONFIG_DIR` | Private config overlay directory | (none) |
| `HYPERION_REPO_URL` | Git repository URL | `https://github.com/SiderealPress/hyperion.git` |
| `HYPERION_BRANCH` | Git branch to install | `main` |
| `HYPERION_USER` | System user | `$USER` |
| `HYPERION_HOME` | Home directory | `$HOME` |
| `HYPERION_INSTALL_DIR` | Installation directory | `$HOME/hyperion` |
| `HYPERION_WORKSPACE` | Claude workspace directory | `$HOME/hyperion-workspace` |
| `HYPERION_MESSAGES` | Message queue directory | `$HOME/messages` |

## CLI Commands

```bash
hyperion start      # Start all services
hyperion stop       # Stop all services
hyperion restart    # Restart services
hyperion status     # Show status
hyperion attach     # Attach to Claude tmux session
hyperion logs       # Show logs (follow mode)
hyperion inbox      # Check pending messages
hyperion outbox     # Check pending replies
hyperion stats      # Show statistics
hyperion test       # Create test message
hyperion help       # Show help
```

## Directory Structure

```
~/hyperion/                    # Repository
├── src/
│   ├── bot/hyperion_bot.py    # Telegram bot
│   ├── mcp/inbox_server.py    # MCP server
│   └── cli                    # CLI tool
├── scripts/
│   └── claude-wrapper.exp     # Expect script for Claude startup
├── scheduled-tasks/           # Scheduled jobs system
│   ├── jobs.json              # Job registry
│   ├── tasks/                 # Task markdown files
│   ├── logs/                  # Execution logs
│   ├── run-job.sh             # Task executor
│   └── sync-crontab.sh        # Crontab synchronizer
├── services/                  # systemd units
├── config/                    # Configuration
└── install.sh                 # Bootstrap installer

~/messages/                    # Runtime data
├── inbox/                     # Incoming messages
├── outbox/                    # Outgoing replies
├── processed/                 # Archive
├── audio/                     # Voice message files
└── task-outputs/              # Scheduled job outputs

~/hyperion-workspace/          # Claude workspace
├── CLAUDE.md                  # System context
└── logs/                      # Log files
```

## MCP Tools

The hyperion-inbox MCP server provides:

### Message Queue
- `check_inbox(source?, limit?)` - Get new messages
- `send_reply(chat_id, text, source?)` - Send a reply
- `mark_processed(message_id)` - Mark message handled
- `list_sources()` - List available channels
- `get_stats()` - Inbox statistics

### Voice Transcription
- `transcribe_audio(message_id)` - Transcribe voice messages using local whisper.cpp (small model). Fully local, no cloud API needed.

### Task Management
- `list_tasks(status?)` - List all tasks
- `create_task(subject, description?)` - Create task
- `update_task(task_id, status?, ...)` - Update task
- `get_task(task_id)` - Get task details
- `delete_task(task_id)` - Delete task

### Scheduled Jobs
Create recurring automated tasks that run on a cron schedule:
- `create_scheduled_job(name, schedule, context)` - Create a new scheduled job
- `list_scheduled_jobs()` - List all jobs with status
- `get_scheduled_job(name)` - Get job details and task file
- `update_scheduled_job(name, schedule?, context?, enabled?)` - Update a job
- `delete_scheduled_job(name)` - Delete a job
- `check_task_outputs(since?, limit?, job_name?)` - Check job outputs
- `write_task_output(job_name, output, status?)` - Write job output (used by job instances)

### GitHub Integration
Access GitHub repositories, issues, PRs, and projects via the GitHub MCP server:
- Browse and search code across repositories
- Create, update, and manage issues
- Review pull requests and add comments
- Access project boards and manage items
- Monitor GitHub Actions workflow runs

## GitHub Integration

Hyperion integrates with GitHub via the official GitHub MCP server. This allows directing work through GitHub issues and project boards.

### Setup

During installation, you'll be prompted for a GitHub Personal Access Token. Or configure manually:

```bash
# Create a PAT at https://github.com/settings/tokens with scopes: repo, read:org, read:project

# Add the GitHub MCP server
claude mcp add-json github '{"type":"http","url":"https://api.githubcopilot.com/mcp","headers":{"Authorization":"Bearer YOUR_PAT"}}'

# Verify
claude mcp list
```

### Usage Examples

```
User: "Check my GitHub issues"
Hyperion: Uses mcp__github tools to list and summarize issues

User: "Work on issue #42"
Hyperion: Reads issue details, implements solution, comments on progress
```

## Scheduled Jobs

Create automated tasks that run on a schedule:

```
User: "Every morning at 9am, check the weather and summarize it"

Main Claude:
  → create_scheduled_job(
      name="morning-weather",
      schedule="0 9 * * *",
      context="Check weather for SF and summarize"
    )

Every day at 9am:
  → Cron runs the job
  → Fresh Claude instance executes task
  → Output written to ~/messages/task-outputs/

Main Claude:
  → check_task_outputs() shows results
```

### Schedule Format (Cron)

| Expression | Meaning |
|------------|---------|
| `0 9 * * *` | Daily at 9:00 AM |
| `*/30 * * * *` | Every 30 minutes |
| `0 */6 * * *` | Every 6 hours |
| `0 9 * * 1` | Every Monday at 9:00 AM |

## Voice Messages

Hyperion supports voice message transcription using local whisper.cpp:

- Voice messages are automatically downloaded from Telegram
- Use `transcribe_audio(message_id)` to transcribe
- Transcription runs locally using whisper.cpp with the small model (~465MB)
- No cloud API or API key required

**Dependencies:**
- **whisper.cpp** - Local speech recognition (installed in `~/hyperion-workspace/whisper.cpp/`)
- **FFmpeg** - Audio format conversion (OGG → WAV)

**Setup:**
```bash
# Install FFmpeg (if not already installed)
sudo apt-get install -y ffmpeg

# Clone and compile whisper.cpp
cd ~/hyperion-workspace
git clone https://github.com/ggerganov/whisper.cpp.git
cd whisper.cpp
make -j$(nproc)

# Download the small model (~465MB)
bash models/download-ggml-model.sh small
```

## Services

| Service | Description |
|---------|-------------|
| `hyperion-router` | Telegram bot (writes to inbox, sends from outbox) |
| `hyperion-claude` | Claude Code session (runs in tmux) |
| `cron` | Scheduled task executor |

Manual control:
```bash
sudo systemctl status hyperion-router
sudo systemctl status hyperion-claude
tmux -L hyperion list-sessions          # Check tmux session
hyperion attach                          # Attach to Claude session
```

## Security

- Bot restricted to allowed user IDs only
- Credentials stored in config.env (gitignored)
- No hardcoded secrets in code

## License

MIT
