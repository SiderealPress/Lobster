# Lobster Dev Skill

Context and behavioral guidelines for active Lobster development work.

## What it does

Activates on demand (not always-on) to inject dev context — staging Docker setup, LOBSTER_ENV quirks, PR workflow conventions, debug mode behavior, test infrastructure, log locations, and key doc pointers — without contaminating normal day-to-day Lobster usage.

## Activate

```
/lobster-dev
```

Or `/dev`. Also auto-activates when the conversation involves staging Docker, Lobster PRs, local-dev branch operations, dispatcher debugging, test runs, LOBSTER_DEBUG, health check failures, or migration/upgrade.sh.

## What's injected when active

**`behavior/system.md`** — what to DO and what NOT to do in dev mode:
- Branch discipline: check which branch `~/lobster/` is on, never checkout feature branches in the live repo
- Worktree creation syntax
- Deploy to local-dev (soak) procedure with the correct command
- PR prerequisites: code review → dogfood (2h soak + `/dogfooded`) → smoke test → PR to main
- Post-merge cleanup (fetch + merge, not checkout)
- Correct staging Docker commands (`docker compose`, not `docker-compose`)
- Host log locations and service names
- How to run tests
- install.sh / upgrade.sh requirements for PRs that change runtime behavior

**`context/dev-reference.md`** — full reference:
- Staging Docker commands (start, verify, stop, wipe)
- LOBSTER_ENV gotcha (must be `production` even in staging container)
- LOBSTER_DEBUG behavior and effects
- Branch strategy and local-dev model
- Complete PR prerequisites with exact commands
- Log locations (host + staging container)
- Test infrastructure (unit/smoke/integration, what each suite covers)
- Health check system and how to diagnose failures
- Rollback procedure
- Active dev tooling and key doc links

## No install needed

This skill is context-only — no dependencies, no install script required. Activate via:

```bash
activate_skill lobster-dev
```
