## Lobster Dev — Usage Guidelines

This skill is active during Lobster development sessions. When active, you have access to staging Docker setup, PR workflow conventions, and known dev quirks.

### Activating

Use `/lobster-dev` to activate, or this skill auto-activates when the conversation involves staging Docker, Lobster PRs, local-dev branch operations, dispatcher debugging, or mentions of "dev mode" / "lobster dev mode".

### What this skill provides

See `context/dev-reference.md` for the full reference. Key quick-access items:

**Starting the staging container:**
```bash
cd ~/lobster/docker/staging && sudo docker-compose -f docker-compose.staging.yml up -d
```

**Verifying staging dispatcher is running:**
```bash
sudo docker exec lobster-staging tmux -L lobster capture-pane -pt lobster
```

**LOBSTER_ENV — critical gotcha:**
- `LOBSTER_ENV=production` → full dispatcher runs (use this even for staging!)
- `LOBSTER_ENV=staging` (or anything else) → process exits immediately (smoke-test mode)

**Deploy a branch to local-dev:**
```bash
git -C ~/lobster merge origin/<branch-name>
```

### PR workflow reminder

1. Code review via `subagent_type="review"` subagent
2. Dogfood on local-dev (deploy and soak)
3. Smoke test on staging Docker
4. PRs target `main` — never `local-dev`
