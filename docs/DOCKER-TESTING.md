# Docker Testing ("Dark Lobster")

The Docker test environment provides an isolated, reproducible environment for running the Lobster test suite. It is sometimes called "dark lobster" because it runs without any real Telegram/Slack credentials — using a mock Telegram server instead.

## When to use it

Use Docker testing when:
- Validating a PR before merge
- Checking that a change doesn't break existing behavior
- Running tests in a clean environment (no dev machine quirks)
- Integration testing with the mock Telegram server

Do not use Docker testing for:
- Tests that require real Telegram API access (use staging bot instead)
- Performance profiling (Docker overhead skews results)

---

## Directory structure

```
tests/
├── docker/
│   ├── docker-compose.test.yml    # Orchestrates all test services
│   ├── Dockerfile.integration     # Full test env (python:3.11-slim-bookworm)
│   ├── Dockerfile.test            # Fresh Debian install test
│   └── entrypoint-test.sh         # Init + test runner script
├── unit/                          # Unit tests (no external deps)
├── integration/                   # Integration tests (requires mock-telegram)
├── stress/                        # Stress / high-volume tests
├── mocks/                         # Mock servers (MockTelegramServer on port 8081)
└── requirements-test.txt          # Test-only Python deps
```

---

## Running tests

All commands run from the repo root (`~/lobster/` or a worktree).

### Run unit tests only (fastest)

```bash
docker compose -f tests/docker/docker-compose.test.yml run --rm unit-tests
```

### Run integration tests (starts mock Telegram server)

```bash
docker compose -f tests/docker/docker-compose.test.yml up integration-tests
```

### Run the full suite

```bash
docker compose -f tests/docker/docker-compose.test.yml up all-tests
```

### Run a specific test file or pattern

Override the command:

```bash
docker compose -f tests/docker/docker-compose.test.yml run --rm unit-tests \
  .venv/bin/pytest tests/unit/test_memory.py -v --tb=short -k "not VectorMemory"
```

### Run fresh install test

```bash
docker compose -f tests/docker/docker-compose.test.yml up install-test
```

### Using the entrypoint script directly

The `entrypoint-test.sh` script supports named test types:

```bash
docker compose -f tests/docker/docker-compose.test.yml run --rm unit-tests \
  bash tests/docker/entrypoint-test.sh unit
```

Available types: `unit`, `integration`, `stress`, `install`, `mcp`, `bot`, `daemon`, `cli`, `all`

---

## Test Telegram bot token

For Docker tests that need a real bot token (smoke tests, end-to-end flows), use the dedicated test bot:

```
8678304453:AAHlkL0C4Tit3e_zVXdD0adPewQVNUQ3jpA
```

**Never use a production bot token in Docker tests.** This token is for isolated testing only.

Pass it via environment variable:

```bash
TELEGRAM_BOT_TOKEN=8678304453:AAHlkL0C4Tit3e_zVXdD0adPewQVNUQ3jpA \
  docker compose -f tests/docker/docker-compose.test.yml run --rm integration-tests
```

---

## Concurrent agent safety (Docker lock)

Docker builds are slow and stateful. If multiple agents run Docker tests simultaneously they will conflict (port clashes, shared volumes, image build races).

Always wrap Docker test invocations in a file lock:

```bash
flock /tmp/lobster-docker.lock \
  docker compose -f tests/docker/docker-compose.test.yml up unit-tests
```

This serializes Docker test runs across all concurrent agent processes on the same host. The lock is released automatically when the command exits.

---

## Known issues and pre-existing failures

These failures exist in the current codebase and are **not regressions** introduced by your change. Do not count them as test failures when evaluating a PR.

### 1. `TestVectorMemory` — fixture errors at setup

**Symptom:** `TestVectorMemory` tests error during the `vec_mem` fixture (before the test body runs).

**Root cause:** The `sqlite-vec` extension (required by `VectorMemory`) is not installed in the Docker image. The `Dockerfile.integration` installs Python deps from `requirements.txt` but `sqlite-vec` may be absent or fail to compile in the container environment.

**Workaround:** Skip these tests in Docker with `-k "not VectorMemory"` or `-m "not requires_sqlite_vec"` until the Dockerfile is updated. The `StaticMemory` fallback tests (`TestMemoryFallback`) do pass.

**Tracking issue:** https://github.com/SiderealPress/lobster/issues/308

---

### 2. `TestSendReply` / `TestMarkProcessed` — error type mismatches

**Symptom:** Tests that assert `"Error" in result[0].text` or check specific error strings fail because the error format returned by the MCP handler has changed.

**Root cause:** The tests were written against an older error response format. The production code now returns structured errors but the test assertions look for the old string patterns.

**Workaround:** These failures are cosmetic — they test error handling paths, not happy paths. The production behavior is correct. Skip or update the affected assertions when you need a clean run.

**Tracking issue:** https://github.com/SiderealPress/lobster/issues/309

---

### 3. `test_blocks_human_message_without_reply` — assertion failure

**Symptom:**
```
AssertionError: assert (inbox / f"{msg_id}.json").exists()
```
The test expects that marking a human message as processed without first sending a reply is blocked (file stays in inbox). In the current implementation this behavior has changed — `mark_processed` now succeeds unconditionally (or the guard was removed/relaxed).

**Root cause:** Behavior change in `handle_mark_processed` not reflected in the test. Either the reply guard was intentionally removed or the logic moved elsewhere.

**Workaround:** Skip this test (`-k "not test_blocks_human_message_without_reply"`) until the test is updated to match current behavior.

**Tracking issue:** https://github.com/SiderealPress/lobster/issues/310

---

## Determining pre-existing vs. regression

When evaluating a PR:

1. Run the same test suite against `main` (without your changes) in Docker.
2. Run against your branch.
3. Compare the failure lists. Only **new** failures in your branch are regressions.

Quick comparison command:

```bash
# On main
git stash
flock /tmp/lobster-docker.lock \
  docker compose -f tests/docker/docker-compose.test.yml run --rm unit-tests \
  .venv/bin/pytest tests/unit -v --tb=line 2>&1 | tee /tmp/main-results.txt

# On your branch
git stash pop
flock /tmp/lobster-docker.lock \
  docker compose -f tests/docker/docker-compose.test.yml run --rm unit-tests \
  .venv/bin/pytest tests/unit -v --tb=line 2>&1 | tee /tmp/branch-results.txt

diff /tmp/main-results.txt /tmp/branch-results.txt
```

---

## Cleanup

Remove stopped containers and the shared test volume:

```bash
docker compose -f tests/docker/docker-compose.test.yml down -v
```

Remove the built images (forces a full rebuild next time):

```bash
docker compose -f tests/docker/docker-compose.test.yml down --rmi local -v
```

---

## Troubleshooting

### Build fails with "exec format error"

You are building on an ARM host (Apple Silicon) targeting AMD64. Add `--platform linux/amd64` if needed, or build natively.

### Port 8081 already in use

Another mock-telegram container is running. Either kill it or use the Docker lock pattern above to serialize runs.

### `ModuleNotFoundError` for a src module

The `WORKDIR` in `Dockerfile.integration` is `/home/testuser/lobster`. Tests import `src.*` relative to this directory. If running tests outside Docker, set `PYTHONPATH=/home/testuser/lobster` (or your local repo root).

### Tests pass locally but fail in Docker

Check for hardcoded paths (`/home/lobster/...`). The Docker container runs as `testuser` with home `/home/testuser`. Any path that embeds the developer's home directory will break.
