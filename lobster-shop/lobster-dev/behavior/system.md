## Lobster Dev — Behavioral Guidelines

This skill is active during Lobster development sessions. When active, follow these rules to avoid common dev mistakes.

---

### Before touching ~/lobster/, check which branch it's on

```bash
git -C ~/lobster branch --show-current
```

- Debug mode (LOBSTER_DEBUG=true): expect `local-dev`
- Production mode: expect `main`

**Never run `git checkout <feature-branch>` inside `~/lobster/`** — this disrupts the live running system. Feature work must happen in a worktree at `~/lobster-workspace/projects/<branch-name>/`.

---

### Creating a new feature branch — use a worktree

```bash
git -C ~/lobster worktree add ~/lobster-workspace/projects/<branch-name> -b <branch-name> origin/main
```

Work entirely inside the worktree. `~/lobster/` stays on its current branch throughout.

---

### Deploying a branch to local-dev for soak testing

While `~/lobster/` is on `local-dev`:

```bash
git -C ~/lobster merge origin/<branch-name>
```

Do NOT switch to main first. The soak must start right after code review passes — do not wait for user sign-off to deploy. The soak ENABLES the sign-off.

---

### PR prerequisites — all three required before merging

1. **Code review** — post `gh pr review <N> --repo SiderealPress/lobster --comment --body "..."`. Verdict must be PASS. Never use `--approve` or `--request-changes` (same token = self-review error).
2. **Dogfooded** — branch merged into `local-dev`, soaking for at least 2 hours, then explicitly cleared with `/dogfooded <PR-number>`. Start the soak immediately after PASS review, not after user sign-off.
3. **Smoke test** — for code PRs (any new feature, bug fix, refactor). Pure doc/prompt-only changes are exempt.

---

### After merging a PR to main — post-merge cleanup

```bash
git -C ~/lobster fetch origin
git -C ~/lobster merge origin/main
```

Do NOT run `git checkout main`. In debug mode `~/lobster/` is on `local-dev` — switching away would break the soak. The merge step pulls the newly merged commit into whatever branch is running.

---

### Staging Docker — correct commands

Start (from repo root, not from docker/staging/):

```bash
cd ~/lobster && sudo docker compose -f docker/staging/docker-compose.staging.yml up -d
```

Verify dispatcher is running inside staging:

```bash
sudo docker exec lobster-staging tmux -L lobster capture-pane -pt lobster
```

Stop:

```bash
cd ~/lobster && sudo docker compose -f docker/staging/docker-compose.staging.yml down
```

**LOBSTER_ENV gotcha:** Always set `LOBSTER_ENV=production` inside the staging container. `LOBSTER_ENV=staging` causes `claude-persistent.sh` to exit immediately (smoke-test-only mode). The container is called "staging" but the env var must be `production`.

---

### Log locations — host system

| What | Command |
|------|---------|
| Dispatcher (Claude session) | `tail -f ~/lobster-workspace/logs/claude-persistent.log` |
| MCP server | `tail -f ~/lobster-workspace/logs/mcp-server.log` |
| Telegram router | `tail -f ~/lobster-workspace/logs/telegram-bot.log` |
| Health check | `tail -f ~/lobster-workspace/logs/health-check.log` |
| Systemd (Claude service) | `journalctl -u lobster-claude -f` |
| Systemd (MCP service) | `journalctl -u lobster-mcp-local -f` |

---

### Running tests

```bash
# Unit tests
cd ~/lobster && uv run pytest tests/unit/ -v

# Smoke tests (fast critical-path checks)
cd ~/lobster && uv run pytest tests/smoke/ -v

# All tests
cd ~/lobster && uv run pytest tests/ -v
```

Smoke tests cover hooks and the health check. Unit tests cover MCP tools, skill manager, event bus, etc.

---

### PRs that change how Lobster runs — check install.sh

Before writing the PR description, scan your diff for:
- New hook files → hook registration in `install.sh`
- New cron jobs → cron entry in `install.sh`
- New service files → `systemctl enable/start` in `install.sh`
- New env vars or config keys → default in `install.sh`

Also add a numbered migration to `scripts/upgrade.sh` for anything that affects existing installs.

---

### MCP restart — safe method only

```bash
~/lobster/scripts/restart-mcp.sh
```

Never run `sudo systemctl restart lobster-mcp-local` directly — it invalidates the active MCP session without warning.

---

### Quick link: staging Docker log tailing (inside container)

| What | Command |
|------|---------|
| Claude session | `sudo docker exec lobster-staging tail -f /home/lobster/lobster-workspace/logs/claude.log` |
| MCP server | `sudo docker exec lobster-staging tail -f /home/lobster/lobster-workspace/logs/mcp-server.log` |
| Router | `sudo docker exec lobster-staging tail -f /home/lobster/lobster-workspace/logs/router.log` |
