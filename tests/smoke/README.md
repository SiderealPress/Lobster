# Smoke Tests

Smoke tests are fast, lightweight checks that verify the most critical paths in
the Lobster system work end-to-end. They are **not** unit tests (they run real
code, not mocks) and they are **not** integration tests (they do not require a
running Lobster instance, a Telegram connection, or external services).

Think of them as "does it start without exploding?" tests. A failing smoke test
means something fundamental is broken and the system cannot operate correctly.

## What belongs here

- Tests that run a script/hook and assert it exits 0
- Tests that verify a critical file was written with the right shape
- Regression tests for bugs that caused silent data loss or duplicate actions

## What does NOT belong here

- Tests requiring network access or external APIs
- Tests that need a running Lobster dispatcher
- Comprehensive behavior tests (those go in `tests/unit/` or `tests/integration/`)

## Running the smoke tests

From the project root:

```bash
cd ~/lobster
uv run pytest tests/smoke/ -v
```

To run a specific file:

```bash
uv run pytest tests/smoke/test_on_compact.py -v
```

## Test groups

| File | Group | What it covers |
|------|-------|----------------|
| `test_on_compact.py` | A | `hooks/on-compact.py` — context-compaction hook |
| `test_post_compact_gate.py` | B | `hooks/post-compact-gate.py` — tool-call gate during compaction |
| `test_require_write_result.py` | C | `hooks/require-write-result.py` — subagent write_result enforcement |
| `test_health_check.py` | D | `scripts/health-check-v3.sh` — health check critical paths |

## Adding new smoke tests

1. Create `tests/smoke/test_<area>.py`
2. Mark the file with `pytestmark = pytest.mark.smoke` (optional but helpful)
3. Keep each test fast (< 5 seconds), isolated (temp dirs), and side-effect-free
4. Document the failure mode in the test docstring — what real bug does this catch?
