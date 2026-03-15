# 🦞 Lobster

**A hardened, always-on Claude Code agent** with Telegram and Slack integration.

*Hard shell. Soft skills. Never sleeps.*

## One-Line Install

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/SiderealPress/lobster/main/install.sh)
```

## Overview

Lobster transforms a server into an always-on Claude Code hub that:

- 🔒 **Runs 24/7** — Claws never stop clicking
- 🧠 **Maintains persistent context** across restarts
- ♻️ **Auto-restarts on failure** via systemd
- 🛡️ **Hardened by design** — sandboxed, isolated, resilient

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                   🦞 LOBSTER DISPATCHER (tmux)                    │
│         Stateless main loop — runs forever in tmux               │
│         Blocks on wait_for_messages(); routes messages in <7s    │
│                                                                   │
│  7-second rule: anything taking longer goes to a subagent        │
│    → Task(subagent_type=..., run_in_background=True)             │
│    → register_agent(agent_id, chat_id, ...)  [SQLite tracking]   │
│    → Subagent calls write_result() when done                     │
│    → Dispatcher forwards result or drops if already delivered    │
│                                                                   │
│  Skill system: composable context layers loaded per message      │
│    → always / triggered / contextual activation modes           │
│                                                                   │
│  Brain-dump routing: voice notes → brain-dumps subagent          │
│    → transcribe_audio() → detect brain dump → GitHub issue       │
│                                                                   │
│   MCP Server: lobster-inbox                                       │
│   - Message queue (check_inbox, mark_processing, mark_processed) │
│   - Task tracking (create_task, update_task, list_tasks)         │
│   - Scheduled job management                                      │
│   - Agent session store (register_agent, get_active_sessions)    │
│   - Subagent result bus (write_result, write_observation)        │
└──────────────────────────────────────────────────────────────────┘
                              ↑↓
               ~/messages/inbox/ ←→ ~/messages/outbox/
               ~/messages/processing/ (claimed messages)
               ~/messages/config/agent_sessions.db (SQLite)
                              ↑↓
┌──────────────────────────────────────────────────────────────────┐
│              TELEGRAM BOT (lobster-router)                        │
│   Writes incoming messages to inbox                               │
│   Watches outbox and sends replies                                │
└──────────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────┐
│              SLACK BOT (lobster-slack-router)                     │
│   Receives messages via Socket Mode                               │
│   Writes to inbox, sends replies from outbox                      │
└──────────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────┐
│              SCHEDULED TASKS (Cron)                               │
│   Automated jobs run on schedule                                  │
│   Each job spawns a fresh Claude subagent instance                │
│   Outputs go to ~/messages/task-outputs/                          │
└──────────────────────────────────────────────────────────────────┘
```

## Prerequisites

- Debian 12+ or Ubuntu 22.04+
- Claude Code authenticated (Max subscription)
- Telegram bot token (from @BotFather) and/or Slack app tokens
- Your Telegram user ID (from @userinfobot) if using Telegram

## Manual Install

```bash
git clone https://github.com/SiderealPress/lobster.git
cd lobster
bash install.sh
```

## Local Installation (VM + Tailscale)

> **Deploying** Lobster, not developing it. To work on the code, see [Development](#development).

Want to run Lobster on your local machine instead of a cloud server? You can run it inside a VM with Tailscale Funnel for internet access:

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
   cp config/lobster.conf.example config/lobster.conf
   ```

2. Edit `config/lobster.conf` with your settings

3. Run the installer:
   ```bash
   ./install.sh
   ```

### Private Configuration Repository

For advanced users who want to keep customizations in a separate repo:

```bash
# Set your private config directory
export LOBSTER_CONFIG_DIR=~/lobster-config

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
| `LOBSTER_CONFIG_DIR` | Private config overlay directory | (none) |
| `LOBSTER_REPO_URL` | Git repository URL | `https://github.com/SiderealPress/lobster.git` |
| `LOBSTER_BRANCH` | Git branch to install | `main` |
| `LOBSTER_USER` | System user | `$USER` |
| `LOBSTER_HOME` | Home directory | `$HOME` |
| `LOBSTER_INSTALL_DIR` | Installation directory | `$HOME/lobster` |
| `LOBSTER_WORKSPACE` | Claude workspace directory | `$HOME/lobster-workspace` |
| `LOBSTER_PROJECTS` | Projects directory | `$LOBSTER_WORKSPACE/projects` |
| `LOBSTER_MESSAGES` | Message queue directory | `$HOME/messages` |

## Development

For working on Lobster's code, not deploying it. The install sections above set up the full always-on assistant (systemd, bots, MCP servers). This section just runs the test suite in Docker — no deployment needed.

### Prerequisites

- [Docker](https://docs.docker.com/get-docker/) (with Docker Compose v2)

### Quick Start

```bash
make test
```

This builds the dev image (Python 3.13 + all deps from `uv.lock`) and runs the full test suite.

### Make Targets

| Command | Description |
|---------|-------------|
| `make test` | Run full test suite |
| `make test-unit` | Run `tests/unit/` only |
| `make test-integration` | Run `tests/integration/` only |
| `make test-file FILE=tests/unit/test_skill_manager.py` | Run a specific test file |
| `make shell` | Open interactive shell in dev container |
| `make build` | Build the dev image only |
| `make clean` | Remove dev containers and images |

### Workflow

Source and test files are bind-mounted into the container, so you can edit locally and re-run `make test` without rebuilding. A rebuild is only needed when dependencies change.

**Adding a dependency:**

1. Edit `pyproject.toml`
2. Run `uv lock` to update the lockfile
3. Run `make build` to rebuild the image with the new dependency

## CLI Commands

```bash
lobster start      # Start all services
lobster stop       # Stop all services
lobster restart    # Restart services
lobster status     # Show status
lobster attach     # Attach to Claude tmux session
lobster logs       # Show logs (follow mode)
lobster inbox      # Check pending messages
lobster outbox     # Check pending replies
lobster stats      # Show statistics
lobster test       # Create test message
lobster help       # Show help
```

## Directory Structure

```
~/lobster/                     # Repository (the shell)
├── src/
│   ├── bot/lobster_bot.py     # Telegram bot
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

~/lobster-workspace/           # Claude workspace (the brain)
├── CLAUDE.md                  # System context
├── projects/                  # All Lobster-managed projects
│   └── [project-name]/        # Each project in its own directory
└── logs/                      # Log files
```

### Project Directory Convention

All projects cloned or created by Lobster live in `~/lobster-workspace/projects/[project-name]/`. This is a system convention, not optional. The directory is created automatically by the installer. The `$LOBSTER_PROJECTS` environment variable points here.

## MCP Tools

The lobster-inbox MCP server provides:

### Message Queue (Dispatcher)
- `wait_for_messages(timeout?, hibernate_on_timeout?)` - Block until messages arrive (dispatcher only)
- `check_inbox(source?, limit?)` - Non-blocking inbox check
- `send_reply(chat_id, text, source?, message_id?, task_id?)` - Send a reply (pass `message_id` to atomically mark processed)
- `mark_processing(message_id)` - Claim a message before processing
- `mark_processed(message_id)` - Mark message handled
- `mark_failed(message_id, error?)` - Mark message failed (auto-retried with backoff)
- `list_sources()` - List available channels
- `get_stats()` - Inbox statistics

### Subagent Result Bus
- `write_result(task_id, chat_id, text, forward?, status?)` - Return a result from a background subagent. Pass `forward=False` if already called `send_reply` directly.
- `write_observation(chat_id, text, category, task_id?)` - Write a side-channel observation (user_context, system_context, system_error)

### Agent Session Tracking (SQLite)
- `register_agent(agent_id, description, chat_id, source?, output_file?, timeout_minutes?)` - Register a running subagent; survives restarts
- `get_active_sessions()` - List all running/recently completed subagent sessions

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

Lobster integrates with GitHub via the official GitHub MCP server. This allows directing work through GitHub issues and project boards.

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
Lobster: Uses mcp__github tools to list and summarize issues

User: "Work on issue #42"
Lobster: Reads issue details, implements solution, comments on progress
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

Lobster supports voice message transcription using local whisper.cpp:

- Voice messages are automatically downloaded from Telegram
- Use `transcribe_audio(message_id)` to transcribe
- Transcription runs locally using whisper.cpp with the small model (~465MB)
- No cloud API or API key required

**Dependencies:**
- **whisper.cpp** - Local speech recognition (installed in `~/lobster-workspace/whisper.cpp/`)
- **FFmpeg** - Audio format conversion (OGG → WAV)

**Setup:**
```bash
# Install FFmpeg (if not already installed)
sudo apt-get install -y ffmpeg

# Clone and compile whisper.cpp
cd ~/lobster-workspace
git clone https://github.com/ggerganov/whisper.cpp.git
cd whisper.cpp
make -j$(nproc)

# Download the small model (~465MB)
bash models/download-ggml-model.sh small
```

## Services

| Service | Description |
|---------|-------------|
| `lobster-router` | Telegram bot (writes to inbox, sends from outbox) |
| `lobster-slack-router` | Slack bot (optional, uses Socket Mode) |
| `lobster-claude` | Claude Code session (runs in tmux) |
| `cron` | Scheduled task executor |

Manual control:
```bash
sudo systemctl status lobster-router
sudo systemctl status lobster-slack-router  # if Slack enabled
sudo systemctl status lobster-claude
tmux -L lobster list-sessions          # Check tmux session
lobster attach                          # Attach to Claude session
```

## Upgrading

Already running Lobster? Pull the latest changes and rerun the installer:

```bash
cd ~/lobster
git pull origin main
./install.sh
```

The installer is idempotent — it updates scripts and services without touching
your existing config, tokens, or message history.

For a full step-by-step guide including lobster-watcher redeployment, DB
migration verification, and rollback instructions, see
[docs/upgrading.md](docs/upgrading.md).

## Slack Integration

To add Slack as a message source, see [docs/SLACK-SETUP.md](docs/SLACK-SETUP.md) for detailed setup instructions.

## Security

- 🔐 Bot restricted to allowed user IDs only
- 🔒 Credentials stored in config.env (gitignored)
- 🛡️ No hardcoded secrets in code
- 🦞 Hard shell, soft on the inside

## License

MIT

---

*Built to survive. Designed to serve.* 🦞
