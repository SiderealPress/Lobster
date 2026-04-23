## Lobster Dev — Reference

Full development context for working on Lobster itself.

---

### Staging Docker Setup

**Full documentation:** `~/lobster/docs/DOCKER-STAGING.md`

| Item | Value |
|------|-------|
| Container name | `lobster-staging` |
| Test bot | `@Lobstertown_test_bot` |
| Compose file | `~/lobster/docker/staging/docker-compose.staging.yml` |
| Staging env file | `~/lobster-config/config.staging.env` |

**Start staging (first run or after Dockerfile changes):**
```bash
cd ~/lobster
sudo docker compose -f docker/staging/docker-compose.staging.yml up -d --build
```

**Start staging (subsequent runs, image already built):**
```bash
cd ~/lobster
sudo docker compose -f docker/staging/docker-compose.staging.yml up -d
```

**Verify dispatcher is running inside staging:**
```bash
sudo docker exec lobster-staging tmux -L lobster capture-pane -pt lobster
```

**Attach interactively (read: Ctrl-b d to detach):**
```bash
sudo docker exec -it lobster-staging tmux -L lobster attach -t lobster
```

**Stop staging (preserves volumes):**
```bash
cd ~/lobster
sudo docker compose -f docker/staging/docker-compose.staging.yml down
```

**Stop staging and wipe volumes (fresh start):**
```bash
cd ~/lobster
sudo docker compose -f docker/staging/docker-compose.staging.yml down -v
```

---

### LOBSTER_ENV Behavior — Known Quirk (issue #1717)

`LOBSTER_ENV` controls whether `claude-persistent.sh` runs the full dispatcher or exits immediately.

| Value | Behavior |
|-------|----------|
| `production` | Full dispatcher runs — use this for all real usage, including the staging container |
| `staging` (or any other value) | `claude-persistent.sh` exits immediately — smoke-test mode only |

**Rule of thumb:** Always set `LOBSTER_ENV=production` inside the staging Docker container. "Staging" is the environment name for the Docker container — it does not mean `LOBSTER_ENV=staging`. The `config.staging.env` file must contain `LOBSTER_ENV=production`.

Symptom if misconfigured: the container starts and the router connects to Telegram, but no Claude session appears in tmux and messages go unanswered.

---

### LOBSTER_DEBUG Behavior

Set in `~/lobster-config/config.env`:

| Value | Behavior |
|-------|----------|
| `false` (default) | `start-claude.sh` launches `claude-persistent.sh` — headless persistent session |
| `true` | `start-claude.sh` launches `claude-wrapper.exp` — interactive REPL via expect, `lobster attach` is fully interactive |

When `LOBSTER_DEBUG=true`:
- `~/lobster/` should be on `local-dev`, not `main`
- If `~/lobster/` is on `main` in debug mode, that's only expected briefly right after a PR merges (before `local-dev` is rebuilt)
- Be more verbose in `write_result` summaries
- OOM monitor activates (controlled by `LOBSTER_DEBUG` gate in `scripts/oom-monitor.py`)

---

### Branch Strategy and Local-Dev

**Branch model:**
- `main` — stable, production branch; all PRs target here
- `local-dev` — local integration branch: `main` + all currently open feature PRs merged. Never pushed or PR'd. Rebuilt whenever the open PR set changes.

**Creating a feature branch (always use a worktree):**
```bash
git -C ~/lobster worktree add ~/lobster-workspace/projects/<branch-name> -b <branch-name> origin/main
```

**Deploy branch to local-dev for soak testing** (while `~/lobster/` is on `local-dev`):
```bash
git -C ~/lobster merge origin/<branch-name>
```

**Do NOT** run `git checkout <feature-branch>` inside `~/lobster/` — it disrupts the live running system.

**Post-merge cleanup** after a PR merges to main on GitHub:
```bash
git -C ~/lobster fetch origin
git -C ~/lobster merge origin/main
```
Do NOT switch to main — leave `~/lobster/` on whatever branch it was on (usually `local-dev` in debug mode).

---

### PR Prerequisites (in order, all required)

1. **Code review posted as a GitHub comment** — verdict must be PASS
   ```bash
   gh pr review <N> --repo SiderealPress/lobster --comment --body "🤖🦞 Lobster (reviewer): PASS/NEEDS-WORK/FAIL: ..."
   ```
   Never `--approve` or `--request-changes` — same token = self-review error.

2. **Dogfooded** — branch merged into `local-dev` and soaking for at least 2 hours, then explicitly cleared with `/dogfooded <PR-number>`.
   - Start the soak immediately after PASS review — do NOT wait for user sign-off. Soak enables sign-off.
   - Soak not required for pure doc/prompt-only changes; `/dogfooded` sign-off still required.
   - Docker testing can substitute for local soak for infrastructure/install changes.

3. **Smoke test** — new features, bug fixes, refactors affecting runtime. Pure doc/prompt changes exempt.

---

### PR Self-Check Before Opening

Before writing the PR description, scan the diff for:
- **New hook files** → add hook registration to `install.sh`
- **New cron jobs** → add cron entry to `install.sh`
- **New service files** → add `systemctl enable/start` to `install.sh`
- **New env vars or config keys** → add default/placeholder to `install.sh`
- **New scripts called from services or cron** → verify `install.sh` creates or copies them
- **Changes affecting existing installs** → add a numbered migration to `scripts/upgrade.sh`

Also ask: does the PR description explain *why* the change is needed, not just *what* it does?

---

### Log Locations — Host System

| Log | Path |
|-----|------|
| Dispatcher (Claude session) | `~/lobster-workspace/logs/claude-persistent.log` |
| MCP server | `~/lobster-workspace/logs/mcp-server.log` |
| Telegram router | `~/lobster-workspace/logs/telegram-bot.log` |
| Health check | `~/lobster-workspace/logs/health-check.log` |
| Dispatcher heartbeat | `~/lobster-workspace/logs/dispatcher-heartbeat` |
| Observations | `~/lobster-workspace/logs/observations.log` |
| Nightly consolidation | `~/lobster-workspace/logs/nightly-consolidation.log` |

**Systemd services:**
| Service | Description |
|---------|-------------|
| `lobster-claude` | Claude Code persistent session |
| `lobster-mcp-local` | MCP server (inbox, tasks, skills, memory) |
| `lobster-router` | Telegram bot router |
| `lobster-transcription` | Voice transcription worker |

**Tail live logs:**
```bash
# Dispatcher session
tail -f ~/lobster-workspace/logs/claude-persistent.log

# Claude dispatcher service log
journalctl -u lobster-claude -f

# MCP server
tail -f ~/lobster-workspace/logs/mcp-server.log
```

---

### Log Locations — Staging Docker Container

| Log | Command |
|-----|---------|
| Claude session | `sudo docker exec lobster-staging tail -f /home/lobster/lobster-workspace/logs/claude.log` |
| MCP server | `sudo docker exec lobster-staging tail -f /home/lobster/lobster-workspace/logs/mcp-server.log` |
| Router | `sudo docker exec lobster-staging tail -f /home/lobster/lobster-workspace/logs/router.log` |
| All (compose) | `cd ~/lobster && sudo docker compose -f docker/staging/docker-compose.staging.yml logs -f` |

---

### Test Infrastructure

**Run tests:**
```bash
# Unit tests — MCP tools, skill manager, event bus, message handling, etc.
cd ~/lobster && uv run pytest tests/unit/ -v

# Smoke tests — critical-path hooks and health check, no external deps needed
cd ~/lobster && uv run pytest tests/smoke/ -v

# Integration tests — require more infrastructure, used for end-to-end flows
cd ~/lobster && uv run pytest tests/integration/ -v

# All tests
cd ~/lobster && uv run pytest tests/ -v
```

**Smoke test coverage:**
| File | Covers |
|------|--------|
| `tests/smoke/test_on_compact.py` | `hooks/on-compact.py` context-compaction hook |
| `tests/smoke/test_post_compact_gate.py` | `hooks/post-compact-gate.py` tool-call gate during compaction |
| `tests/smoke/test_require_write_result.py` | `hooks/require-write-result.py` subagent enforcement |
| `tests/smoke/test_health_check.py` | `scripts/health-check-v3.sh` critical paths |

---

### Health Check System

Health check runs every 4 minutes via cron: `~/lobster/scripts/health-check-v3.sh`

**Escalation ladder:**
- GREEN — all checks pass
- YELLOW — inbox messages exist but not stale, or transient state
- RED — stale inbox > threshold OR missing process/tmux/service → triggers restart
- BLACK — 3 restart failures in cooldown window → alert, stop retrying

**Key signals:**
- Dispatcher heartbeat: `~/lobster-workspace/logs/dispatcher-heartbeat` (written by `hooks/thinking-heartbeat.py` on every PostToolUse). Checked against 1200s threshold.
- Compaction suppression: after `on-compact.py` fires, stale-inbox check is suppressed for a window. Heartbeat check is NOT suppressed.

**Diagnose a failed health check:**
```bash
# Last health check result
tail -50 ~/lobster-workspace/logs/health-check.log

# Any recent incidents collected before restarts
ls ~/lobster/incidents/
```

**Rollback via update-lobster.sh:**
```bash
~/lobster/scripts/update-lobster.sh --rollback
```
Restores from the most recent backup created during the last upgrade.

---

### Active Dev Tooling

| Tool | Location |
|------|----------|
| Main repo | `SiderealPress/lobster` on GitHub |
| PR review subagent | spawn with `subagent_type="review"` |
| Staging Docker | `~/lobster/docker/staging/` |
| Incident investigation | `~/lobster/scripts/investigate-incident.sh "<reason>"` |
| MCP safe restart | `~/lobster/scripts/restart-mcp.sh` |

---

### Quick Reference Docs

| Doc | Path | What it covers |
|-----|------|----------------|
| Staging Docker | `~/lobster/docs/DOCKER-STAGING.md` | Full staging container setup, credentials, tailing logs |
| Docker Testing | `~/lobster/docs/DOCKER-TESTING.md` | Automated integration test approach (uses mock Telegram) |
| Dispatcher spec | `~/lobster-workspace/.claude/sys.dispatcher.bootup.md` | Main loop pseudocode, 7-second rule, message flow |
| Debug supplement | `~/lobster-workspace/.claude/sys.debug.bootup.md` | install.sh completeness rule, PR self-check prompt, verbose logging |
| Install script | `~/lobster/install.sh` | Check when adding new hooks, cron entries, or service files |
| MCP restart | `~/lobster/scripts/restart-mcp.sh` | Safe MCP restart (never use systemctl directly) |
| Migration system | `~/lobster/scripts/upgrade.sh` | Run migrations, add new migrations at the bottom |

---

### Common Dev Commands

```bash
# Check which branch ~/lobster/ is on
git -C ~/lobster branch --show-current

# Create a feature worktree
git -C ~/lobster worktree add ~/lobster-workspace/projects/<branch-name> -b <branch-name> origin/main

# Check all worktrees
git -C ~/lobster worktree list

# Merge feature branch into local-dev (while on local-dev)
git -C ~/lobster merge origin/<branch-name>

# Post-merge cleanup after PR merges to main
git -C ~/lobster fetch origin && git -C ~/lobster merge origin/main

# Remove a worktree after PR merge
git -C ~/lobster worktree remove ~/lobster-workspace/projects/<branch-name>
git -C ~/lobster branch -d <branch-name>

# Restart MCP safely
~/lobster/scripts/restart-mcp.sh

# Run smoke tests
cd ~/lobster && uv run pytest tests/smoke/ -v

# Run all tests
cd ~/lobster && uv run pytest tests/ -v
```
