# Lobster Dev Skill

Context and quick-reference for active Lobster development work.

## What it does

Activates on demand (not always-on) to inject dev context — staging Docker setup, LOBSTER_ENV quirks, PR workflow conventions, and key doc pointers — without contaminating normal day-to-day Lobster usage.

## Activate

```
/lobster-dev
```

Or `/dev`. Also auto-activates when the conversation involves staging Docker, Lobster PRs, local-dev branch operations, or dispatcher debugging.

## What's injected when active

- **Staging Docker** — how to start `lobster-staging`, how to verify the dispatcher, key gotchas (see issue #1717 on LOBSTER_ENV)
- **LOBSTER_ENV behavior** — `production` runs the dispatcher; anything else exits immediately
- **PR workflow** — code review → dogfood on local-dev → smoke test → PR to `main`
- **Worktree discipline** — `~/lobster/` stays on `main`; feature work goes in worktrees
- **Quick links** — DOCKER-STAGING.md, dispatcher spec, install script, MCP restart script

## No install needed

This skill is context-only — no dependencies, no install script required. Activate via:

```bash
activate_skill lobster-dev
```
