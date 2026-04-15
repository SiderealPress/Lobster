## Summary

-
-

## Alternatives considered

<!-- Required if this PR replaces a previous approach or changes an existing mechanism. -->
<!-- For each rejected alternative: what was tried, why it was rejected, and any confirming test/error. -->
<!-- For purely additive PRs (no existing approach replaced): "N/A — purely additive." -->

## Tests run

Fill this in before opening the PR. Each checked item must show the exact command and a brief outcome. Each unchecked item must explain why it was skipped or blocked. No abstract category labels — just commands and results.

- [x] `uv run pytest tests/unit/` — 42 passed, 0 failed
- [x] `uv run ruff check . && uv run mypy .` — clean, no errors
- [ ] `docker compose -f tests/docker/docker-compose.test.yml up install-test` — skipped: Docker not available in this environment
- [ ] Live Telegram test — blocked: requires production restart (safe to merge, no behavior change)

**Blocked items needing attention before merge:** none

> If you couldn't run something, write the exact command and: "Couldn't run: [reason] — needs [X] before merge"
> and flag it in `write_result` so the dispatcher can relay to the user.

## Functional patterns used

-

## Breaking changes / migration notes

None.

Closes #
