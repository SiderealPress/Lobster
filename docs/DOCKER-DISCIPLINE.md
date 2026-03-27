# Docker Discipline Policy

## Purpose

Docker is used for testing Lobster installs in clean, isolated environments that mirror real deployments. This policy defines the rules for how containers and images are created, named, used, and cleaned up on machines that run or test Lobster.

---

## Rules

### 1. Real installs only

Every test Docker container must run a real install via `install.sh` on an OS that supports systemd (e.g., Ubuntu 22.04 with systemd). No shortcuts, no partial setups, no hand-wired config files. If the install script cannot succeed inside the container, that is a bug to fix — not a reason to bypass it.

### 2. One TEST token at a time

Only one Docker container may hold the TEST Telegram bot token at any given time. This container is the "active test" container. Before spinning up a new active test container, the old one must be stopped and removed. Reason: two containers running the same bot token produce split message delivery and unpredictable behavior.

### 3. No production tokens in Docker

No container may ever contain the production Telegram bot token or any production credential. Production tokens are strictly for the live Lobster process on the production host — never in a container, never in a Dockerfile, never in a compose file committed to the repo.

### 4. Secrets via runtime env files

Credentials are passed at container startup using `--env-file`, not baked into images:

```bash
docker run --env-file /path/to/test.env ...
```

The env file itself lives outside the repo (never committed). It is never referenced with an `ENV` directive inside a Dockerfile. This ensures images are credential-free and can be safely shared or cached.

### 5. Max N=2 containers running simultaneously

At most two containers may run on a development machine at the same time:

- 1 active test container (holds the TEST bot token)
- 0 or 1 additional containers for parallel testing (no token — used for install verification, unit tests, etc.)

SharedLobster / staging containers on the shared server are excluded from this count. If you need more than 2 local containers for a specific investigation, stop and remove them when done.

### 6. Cleanup after testing

When a testing session is complete, stop and remove the test container:

```bash
docker stop lobster-test && docker rm lobster-test
```

Exception: the active test container may persist across sessions if ongoing tests require it. However, it must be explicitly named (see Rule 8) and its purpose must be documented.

### 7. CI auto-cleanup

Every CI run must prune stopped containers and dangling images after completion. Add this to the end of any CI workflow that uses Docker:

```bash
docker container prune -f && docker image prune -f
```

For full teardown (e.g., after integration test suites), use:

```bash
docker system prune -f
```

### 8. Named, dated images

Test images must follow this naming convention:

```
lobster-test-YYYYMMDD-<purpose>
```

Examples:
- `lobster-test-20260327-http-mcp`
- `lobster-test-20260327-install`
- `lobster-test-20260401-telegram-hooks`

This makes it easy to identify stale images during audits. Anonymous images (`<none>`) must be pruned immediately after the build that created them.

### 9. Regular audits

Monthly — or after any PR merge wave — run the following audit:

```bash
docker system df
docker ps -a
docker images --format "table {{.Repository}}\t{{.Tag}}\t{{.CreatedAt}}\t{{.Size}}"
```

Cleanup rules:
- Any stopped container older than 7 days: remove it
- Any image not used in 14 days: remove it
- Any volume not attached to a running container: review and remove if safe

The dispatcher runs a weekly automated Docker audit and reports results to the operator. See Rule 10 and `docs/DOCKER-TESTING.md` for details.

### 10. GitHub token handling

The GitHub token is NOT injected into test containers unless the specific test requires it. If a test genuinely needs GitHub access, use a scoped read-only token with the minimum required permissions. Document which test requires it and why.

---

## How to set up a test container

See `~/lobster/docs/DOCKER-TESTING.md` for the step-by-step guide.

---

## Enforcement

The dispatcher runs a weekly Docker audit scheduled job that checks:

1. No production tokens are present in any running container
2. No more than N=2 containers are running simultaneously
3. No images are older than 14 days
4. No stopped containers are older than 7 days

Violations are flagged immediately to the operator via Telegram. A GitHub issue tracks the implementation of this automated enforcement: see the issue tagged `docker-discipline` in the SiderealPress/lobster repo.
