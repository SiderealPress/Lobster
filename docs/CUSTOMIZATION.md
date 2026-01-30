# Customizing Hyperion

This guide explains how to customize Hyperion using a private configuration repository. The private repo overlay pattern keeps your personal settings separate from the public codebase, enabling clean upgrades and portable configuration.

## Table of Contents

1. [Why Use a Private Config Repo](#1-why-use-a-private-config-repo)
2. [Quick Start](#2-quick-start)
3. [Private Repo Structure](#3-private-repo-structure)
4. [Configuration Files](#4-configuration-files)
5. [Personal Context Directory](#5-personal-context-directory)
6. [Hooks](#6-hooks)
7. [Keeping Your Private Repo Secure](#7-keeping-your-private-repo-secure)
8. [Upgrading Hyperion](#8-upgrading-hyperion)
9. [Troubleshooting](#9-troubleshooting)

---

## 1. Why Use a Private Config Repo

A private configuration repository provides several benefits:

| Benefit | Description |
|---------|-------------|
| **Clean Upgrades** | Pull updates from upstream without merge conflicts |
| **Portable Config** | Move your setup between machines easily |
| **Version Control** | Track changes to your configuration over time |
| **Security** | Keep secrets out of the public repo |
| **Separation of Concerns** | Distinguish between core code and personal customizations |

Without a private repo, you would need to either:
- Modify files in the public repo (causing merge conflicts on upgrade)
- Manually re-apply your settings after each update

The overlay pattern solves both problems.

---

## 2. Quick Start

Set up a private config repo in minutes:

### Step 1: Create the Private Repository

```bash
# Create and initialize the config directory
mkdir ~/hyperion-config
cd ~/hyperion-config
git init

# Create the basic structure
mkdir -p agents scheduled-tasks/tasks hooks
```

### Step 2: Copy Your Secrets

```bash
# Copy the existing config.env (contains your credentials)
cp ~/hyperion/config/config.env ~/hyperion-config/config.env
```

### Step 3: Set the Overlay Path

Add this to your shell profile (`~/.bashrc` or `~/.zshrc`):

```bash
export HYPERION_CONFIG_DIR=~/hyperion-config
```

Reload your shell:

```bash
source ~/.bashrc  # or source ~/.zshrc
```

### Step 4: Apply the Overlay

```bash
cd ~/hyperion
./install.sh
```

The installer will detect your private config directory and apply the overlay.

### Step 5: Push to a Private Remote (Recommended)

```bash
cd ~/hyperion-config
git add .
git commit -m "Initial configuration"

# Create a private repo on GitHub, then:
git remote add origin git@github.com:YOUR_USERNAME/hyperion-config.git
git push -u origin main
```

---

## 3. Private Repo Structure

Your private config repo can contain any of the following:

```
hyperion-config/
├── config.env              # Credentials and settings (REQUIRED)
├── CLAUDE.md               # Custom Claude context (optional)
├── agents/                 # Custom agent definitions (optional)
│   ├── my-custom-agent.md
│   └── work-assistant.md
├── context/                # Personal context for brain dumps (optional)
│   ├── goals.md            # Long/short-term objectives
│   ├── projects.md         # Active projects
│   ├── values.md           # Core principles
│   ├── habits.md           # Routines and preferences
│   ├── people.md           # Key relationships
│   ├── desires.md          # Wants and aspirations
│   └── serendipity.md      # Random discoveries
├── scheduled-tasks/        # Custom scheduled jobs (optional)
│   ├── jobs.json           # Job registry
│   └── tasks/
│       ├── daily-report.md
│       └── weekly-summary.md
└── hooks/                  # Custom scripts (optional)
    ├── post-install.sh
    └── post-update.sh
```

### What Gets Overlaid

| Private Repo File | Behavior |
|-------------------|----------|
| `config.env` | **Replaces** default config |
| `CLAUDE.md` | **Replaces** workspace context |
| `agents/*.md` | **Merged** with default agents |
| `context/*.md` | **Read** by brain-dumps agent for context matching |
| `scheduled-tasks/` | **Merged** with default tasks |
| `hooks/*.sh` | **Executed** at appropriate times |

---

## 4. Configuration Files

### config.env (Required)

The main configuration file containing your credentials and settings.

```bash
# Hyperion Configuration
# WARNING: This file contains secrets. Never share or commit to public repos.
# Ensure file permissions are restrictive: chmod 600 config.env
#
# Required for basic operation

# Telegram Bot (from @BotFather)
TELEGRAM_BOT_TOKEN=123456789:ABCdefGHIjklMNOpqrsTUVwxyz
TELEGRAM_ALLOWED_USERS=123456789

# Multiple users (comma-separated)
# TELEGRAM_ALLOWED_USERS=123456789,987654321

# Voice Transcription (optional - uses local whisper.cpp by default)
# Only needed if you want to use OpenAI's Whisper API instead
# OPENAI_API_KEY=sk-...

# GitHub Integration (optional)
# GITHUB_PAT_CONFIGURED=true

# Future integrations
# TWILIO_ACCOUNT_SID=
# TWILIO_AUTH_TOKEN=
# TWILIO_PHONE_NUMBER=
# SIGNAL_PHONE_NUMBER=
```

**Getting Your Credentials:**

| Credential | How to Get It |
|------------|---------------|
| `TELEGRAM_BOT_TOKEN` | Message [@BotFather](https://t.me/BotFather) and create a new bot |
| `TELEGRAM_ALLOWED_USERS` | Message [@userinfobot](https://t.me/userinfobot) to get your numeric ID |
| `GITHUB_PAT` | Create at [github.com/settings/tokens](https://github.com/settings/tokens) |

### CLAUDE.md (Optional)

Custom instructions for Claude. When present in your private repo, this **completely replaces** the default workspace context.

```markdown
# My Hyperion Context

You are my personal assistant with these specializations:

## My Preferences
- I prefer concise responses
- Use metric units
- My timezone is America/Los_Angeles

## My Projects
- **Project Alpha**: React frontend at ~/projects/alpha
- **Project Beta**: Python backend at ~/projects/beta

## Custom Commands
When I say "status report", check all my GitHub repos for open PRs.
When I say "morning brief", summarize my calendar and tasks.

## Include Default Behavior
[Include the standard Hyperion behavior from the main CLAUDE.md here
if you want to extend rather than replace it]
```

**Tip:** To extend rather than replace the default behavior, copy the contents of `~/hyperion/CLAUDE.md` into your custom version and add your modifications.

### agents/*.md (Optional)

Define custom Claude Code agents for specialized tasks. These are **merged** with the default agents in `~/hyperion/.claude/agents/`.

**Built-in agents you can override:**

| Agent | Description |
|-------|-------------|
| `functional-engineer.md` | Implements GitHub issues with functional programming patterns |
| `hyperion-ops.md` | System operations and maintenance |
| `brain-dumps.md` | Processes voice note brain dumps into GitHub issues |

To customize a built-in agent, copy it to your private config and modify:

```bash
# Example: Customize the brain-dumps agent
cp ~/hyperion/.claude/agents/brain-dumps.md ~/hyperion-config/agents/brain-dumps.md
# Edit to add custom labels, change behavior, etc.
```

**Creating new agents:**

Example: `agents/code-reviewer.md`

```markdown
# Code Reviewer Agent

You are a specialized code review agent. When reviewing code:

## Review Checklist
- [ ] Check for security vulnerabilities
- [ ] Verify error handling
- [ ] Assess test coverage
- [ ] Review naming conventions
- [ ] Check for performance issues

## Output Format
Provide feedback in this structure:
1. **Summary**: One-line assessment
2. **Critical Issues**: Must fix before merge
3. **Suggestions**: Nice to have improvements
4. **Positive Notes**: What was done well
```

### scheduled-tasks/ (Optional)

Define automated jobs that run on a cron schedule.

**jobs.json** - Registry of all scheduled jobs:

```json
{
  "jobs": {
    "daily-standup": {
      "schedule": "0 9 * * 1-5",
      "enabled": true,
      "description": "Generate daily standup summary",
      "last_run": null,
      "last_status": null
    },
    "weekly-review": {
      "schedule": "0 17 * * 5",
      "enabled": true,
      "description": "Weekly project review",
      "last_run": null,
      "last_status": null
    }
  }
}
```

**tasks/daily-standup.md** - Task instructions:

```markdown
# Daily Standup Summary

Generate a standup summary for today.

## Tasks
1. Check GitHub for PRs merged yesterday
2. List open PRs awaiting review
3. Summarize any failed CI builds
4. Check for issues assigned to me

## Output Format
Write a brief summary suitable for posting in Slack.
```

**Cron Schedule Reference:**

| Expression | Meaning |
|------------|---------|
| `0 9 * * *` | Daily at 9:00 AM |
| `0 9 * * 1-5` | Weekdays at 9:00 AM |
| `*/30 * * * *` | Every 30 minutes |
| `0 */6 * * *` | Every 6 hours |
| `0 9 * * 1` | Every Monday at 9:00 AM |
| `0 0 1 * *` | First day of each month |

---

## 5. Personal Context Directory

The context directory enables the brain-dumps agent to understand your world - your goals, projects, people, and values. This persistent context allows for intelligent matching and enrichment of brain dumps.

### Overview

When you send a brain dump, the agent can:
- Match mentioned projects to your actual project list
- Recognize people you mention
- Link thoughts to your stated goals
- Check alignment with your values
- Suggest additions to your context

### Setting Up Context

1. **Copy templates to your private config:**

```bash
# Create context directory
mkdir -p ~/hyperion-config/context

# Copy templates
cp ~/hyperion/context-templates/*.md ~/hyperion-config/context/
```

2. **Configure the context path** in your `config.env`:

```bash
HYPERION_CONTEXT_DIR="${HYPERION_CONFIG_DIR}/context"
```

3. **Fill in your context files** - Edit each file to add your information:

```bash
nano ~/hyperion-config/context/goals.md
nano ~/hyperion-config/context/projects.md
# etc.
```

### Context Files

| File | Purpose | Update Frequency |
|------|---------|------------------|
| `goals.md` | Long-term vision, annual objectives, current sprints | Monthly |
| `projects.md` | Active, on-hold, and completed projects | Weekly |
| `values.md` | Core principles, priorities, decision frameworks | Quarterly |
| `habits.md` | Daily/weekly routines, preferences, time blocks | Monthly |
| `people.md` | Key relationships, contacts, important dates | As needed |
| `desires.md` | Wants, bucket list, aspirations not yet goals | Quarterly |
| `serendipity.md` | Random discoveries, inspirations, interesting finds | Daily |

### Example: projects.md

```markdown
# Projects

## Active Projects

### MyApp
- **Status**: In Development
- **Repository**: https://github.com/me/myapp
- **Tech Stack**: TypeScript, React, PostgreSQL
- **Current Focus**: Authentication system
- **Next Milestone**: Beta launch (Feb 15)

### Side Project
- **Status**: Planning
- **Tech Stack**: Python, FastAPI
- **Current Focus**: Writing PRD
```

### Example: people.md

```markdown
# People

## Professional Network

### Alex Chen
- **Relationship**: Manager
- **Company**: Acme Corp
- **Context**: Reports to VP of Engineering
- **Notes**: Weekly 1:1 on Wednesdays

## Friends

### Mike
- **Context**: College friend, lives in Austin
- **Interests**: Hiking, startups
- **Last Contact**: Coffee last month
```

### How Context is Used

When processing a brain dump that mentions "the auth system for MyApp" and "call Mike":

1. **Project matching**: Finds "MyApp" in projects.md, notes it's in development with auth focus
2. **People matching**: Finds "Mike" in people.md, notes he's a hiking friend
3. **Enrichment**: Adds labels `project:myapp`, links to repo, notes Mike context
4. **Context update**: If new project or person detected, suggests adding them

### Privacy Notes

- Context files contain personal information
- Always keep in a **private** repository
- Set restrictive permissions: `chmod 600 ~/hyperion-config/context/*`
- Review files before sharing any backups

### Tips

- **Start small**: Fill in essentials first, expand over time
- **Be honest**: Context works best when it reflects reality
- **Review regularly**: Outdated context can be misleading
- **Use templates**: The commented examples show expected format

See [context-templates/README.md](../context-templates/README.md) and [BRAIN-DUMPS.md](BRAIN-DUMPS.md) for more details.

---

## 6. Hooks

Hooks are scripts that run at specific points during installation or updates.

### post-install.sh

Runs after `install.sh` completes. Use for:

- Installing additional dependencies
- Setting up custom symlinks
- Configuring external services
- Running database migrations

```bash
#!/bin/bash
# ~/hyperion-config/hooks/post-install.sh

set -e

echo "Running post-install customizations..."

# Install additional Python packages
source ~/hyperion/.venv/bin/activate
pip install pandas matplotlib
deactivate

# Set up custom symlinks
ln -sf ~/hyperion-config/my-scripts ~/scripts

# Configure external services
if command -v ngrok &> /dev/null; then
    echo "Configuring ngrok..."
    ngrok config add-authtoken "$NGROK_TOKEN"
fi

# Clone additional repositories
if [ ! -d ~/projects/my-project ]; then
    git clone git@github.com:me/my-project.git ~/projects/my-project
fi

echo "Post-install complete!"
```

### post-update.sh

Runs after `git pull` updates the main repository. Use for:

- Clearing caches
- Restarting services
- Running migrations
- Rebuilding assets

```bash
#!/bin/bash
# ~/hyperion-config/hooks/post-update.sh

set -e

echo "Running post-update customizations..."

# Clear any caches
rm -rf ~/hyperion-workspace/.cache/*

# Rebuild whisper.cpp if needed
if [ -d ~/hyperion-workspace/whisper.cpp ]; then
    cd ~/hyperion-workspace/whisper.cpp
    git pull
    make -j$(nproc)
fi

# Restart services to pick up changes
sudo systemctl restart hyperion-router
sudo systemctl restart hyperion-claude

echo "Post-update complete!"
```

**Important:** Make your hooks executable:

```bash
chmod +x ~/hyperion-config/hooks/*.sh
```

---

## 7. Keeping Your Private Repo Secure

### Recommended .gitignore

Create a `.gitignore` in your private repo:

```gitignore
# Logs and temporary files
*.log
*.tmp
*.bak

# Editor files
.idea/
.vscode/
*.swp
*.swo
*~

# OS files
.DS_Store
Thumbs.db

# Local overrides (if you have machine-specific settings)
.env.local
config.local.env

# Sensitive backups
*.backup
```

### Security Best Practices

1. **Use a Private Repository**
   - Never make your config repo public
   - Use GitHub/GitLab private repos or self-hosted Git

2. **Protect Your Tokens**
   - Use environment-specific tokens where possible
   - Rotate tokens periodically
   - Use minimal permission scopes

3. **Audit Access**
   - Review who has access to your private repo
   - Use deploy keys instead of personal tokens for CI/CD

4. **Encrypt Sensitive Data (Optional)**
   ```bash
   # Encrypt config.env before committing
   gpg --symmetric --cipher-algo AES256 config.env
   
   # Add to .gitignore
   echo "config.env" >> .gitignore
   
   # Commit encrypted version
   git add config.env.gpg
   ```

---

## 8. Upgrading Hyperion

The overlay pattern makes upgrades straightforward:

### Standard Upgrade

```bash
# 1. Pull latest from upstream
cd ~/hyperion
git pull origin main

# 2. Re-run installer to apply your overlay
HYPERION_CONFIG_DIR=~/hyperion-config ./install.sh

# 3. Restart services
hyperion restart
```

### Checking for Breaking Changes

Before upgrading, review the changelog:

```bash
cd ~/hyperion

# See what's new
git fetch origin
git log HEAD..origin/main --oneline

# Check for changes to config format
git diff HEAD..origin/main -- config/config.env.example
```

### Handling Breaking Changes

If the config format changes:

1. Compare your config with the new example:
   ```bash
   diff ~/hyperion-config/config.env ~/hyperion/config/config.env.example
   ```

2. Add any new required variables to your config

3. Re-run the installer

### Rollback (If Needed)

```bash
cd ~/hyperion

# See available versions
git tag -l

# Rollback to specific version
git checkout v1.2.3

# Re-apply overlay
HYPERION_CONFIG_DIR=~/hyperion-config ./install.sh
```

---

## 9. Troubleshooting

### Common Issues

#### Config Not Being Applied

**Symptom:** Changes to your private repo aren't taking effect.

**Solutions:**
1. Verify the environment variable is set:
   ```bash
   echo $HYPERION_CONFIG_DIR
   ```

2. Re-run the installer:
   ```bash
   cd ~/hyperion && ./install.sh
   ```

3. Check file permissions:
   ```bash
   ls -la ~/hyperion-config/
   ```

#### Services Won't Start

**Symptom:** `hyperion status` shows services as failed.

**Solutions:**
1. Check the logs:
   ```bash
   hyperion logs
   journalctl -u hyperion-router -n 50
   journalctl -u hyperion-claude -n 50
   ```

2. Verify config.env syntax:
   ```bash
   source ~/hyperion-config/config.env && echo "Config OK"
   ```

3. Check for missing dependencies:
   ```bash
   ~/hyperion/.venv/bin/python -c "import telegram; print('OK')"
   ```

#### Hooks Not Running

**Symptom:** Your hook scripts don't execute.

**Solutions:**
1. Check execution permission:
   ```bash
   ls -la ~/hyperion-config/hooks/
   chmod +x ~/hyperion-config/hooks/*.sh
   ```

2. Test manually:
   ```bash
   bash -x ~/hyperion-config/hooks/post-install.sh
   ```

3. Check for syntax errors:
   ```bash
   bash -n ~/hyperion-config/hooks/post-install.sh
   ```

#### Scheduled Jobs Not Running

**Symptom:** Cron jobs don't execute on schedule.

**Solutions:**
1. Verify cron is running:
   ```bash
   sudo systemctl status cron
   ```

2. Check crontab entries:
   ```bash
   crontab -l | grep HYPERION
   ```

3. Sync the crontab:
   ```bash
   ~/hyperion/scheduled-tasks/sync-crontab.sh
   ```

4. Check job logs:
   ```bash
   ls -la ~/hyperion/scheduled-tasks/logs/
   ```

#### Custom CLAUDE.md Not Working

**Symptom:** Claude doesn't follow your custom instructions.

**Solutions:**
1. Verify the file exists in the workspace:
   ```bash
   cat ~/hyperion-workspace/CLAUDE.md
   ```

2. Re-run installer to copy it:
   ```bash
   cd ~/hyperion && ./install.sh
   ```

3. Restart the Claude service:
   ```bash
   hyperion restart
   ```

### Getting Help

If you're still stuck:

1. Check the [GitHub Issues](https://github.com/SiderealPress/hyperion/issues)
2. Review the main [README](../README.md)
3. Examine the [install.sh](../install.sh) script for overlay logic

---

## Summary

The private repo overlay pattern provides a clean separation between Hyperion's core code and your personal customizations. By maintaining your configuration in a separate repository, you can:

- Upgrade Hyperion without merge conflicts
- Version control your personal settings
- Easily migrate between machines
- Keep your secrets secure

Start with just `config.env`, then gradually add custom agents, scheduled tasks, and hooks as needed.
