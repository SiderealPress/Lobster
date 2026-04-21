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

**Start staging:**
```bash
cd ~/lobster/docker/staging && sudo docker-compose -f docker-compose.staging.yml up -d
```

**Verify dispatcher is running inside staging:**
```bash
sudo docker exec lobster-staging tmux -L lobster capture-pane -pt lobster
```

**Stop staging:**
```bash
cd ~/lobster/docker/staging && sudo docker-compose -f docker-compose.staging.yml down
```

---

### LOBSTER_ENV Behavior — Known Quirk (issue #1717)

`LOBSTER_ENV` controls whether `claude-persistent.sh` runs the full dispatcher or exits immediately.

| Value | Behavior |
|-------|----------|
| `production` | Full dispatcher runs — use this for all real usage, including the staging container |
| `staging` (or any other value) | `claude-persistent.sh` exits immediately — smoke-test mode only |

**Rule of thumb:** Always set `LOBSTER_ENV=production` inside the staging Docker container. "Staging" is the environment name for the Docker container — it does not mean `LOBSTER_ENV=staging`.

---

### Key Dev Patterns

**Branch strategy:**
- `main` — stable, production branch; all PRs target here
- `local-dev` — integration branch for soak testing before promotion to main; never target in PRs

**Deploy to local-dev (soak test):**
```bash
git -C ~/lobster merge origin/<branch-name>
```
Do NOT `git checkout` to another branch in `~/lobster/` — that affects the live system. Use `git -C ~/lobster merge` directly.

**Worktree discipline:**
- `~/lobster/` must always stay on `main`
- All feature work happens in a worktree under `~/lobster-workspace/projects/<branch-name>/`
- Create worktrees with: `git worktree add -b <branch> ~/lobster-workspace/projects/<branch> origin/main`

**PR prerequisites (in order):**
1. Code review — spawn a subagent with `subagent_type="review"` to review the PR
2. Dogfood — deploy to local-dev and soak
3. Smoke test — run against staging Docker container
4. PR targets `main`

---

### Active Dev Tooling

| Tool | Location |
|------|----------|
| Main repo | `SiderealPress/lobster` on GitHub |
| Research tracking | `sayhar/lobster-research` on GitHub |
| PR review subagent | `subagent_type="review"` |
| Staging Docker | `~/lobster/docker/staging/` |

---

### Quick Reference Docs

| Doc | Path | What it covers |
|-----|------|----------------|
| Staging Docker | `~/lobster/docs/DOCKER-STAGING.md` | Full staging container setup |
| Docker Testing | `~/lobster/docs/DOCKER-TESTING.md` | Integration test approach |
| Dispatcher spec | `~/lobster/.claude/sys.dispatcher.bootup.md` | Main loop pseudocode, 7-second rule, message flow |
| Install script | `~/lobster/scripts/install.sh` | Check when adding new hooks, cron entries, or service files |
| MCP restart | `~/lobster/scripts/restart-mcp.sh` | Safe MCP restart (never use systemctl directly) |

---

### Common Dev Commands

```bash
# Check live dispatcher logs
sudo journalctl -u lobster-dispatcher -f

# Restart MCP safely (never systemctl directly)
~/lobster/scripts/restart-mcp.sh

# Run tests
cd ~/lobster && uv run pytest tests/

# Check worktrees
git -C ~/lobster worktree list

# Remove a worktree after PR merge
git -C ~/lobster worktree remove ~/lobster-workspace/projects/<branch-name>
git -C ~/lobster branch -d <branch-name>
```
