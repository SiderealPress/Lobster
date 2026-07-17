# Contributing to Lobster

Thanks for contributing. This document covers what to read before you touch the codebase, and the conventions expected of any change — whether you're a human or an AI agent.

## For AI agents

Before making any change to this repository, read the following documents **in this order**:

1. [`docs/engineering-lessons-learned.md`](docs/engineering-lessons-learned.md) — recurring bug patterns and subtle system behaviors that have bitten past reviews (PID reuse races, tmux `-a` flag, dispatcher-exclusion bugs, etc.). Check new code against these patterns before proposing it.
2. [`docs/INVARIANTS.md`](docs/INVARIANTS.md) — named system invariants (e.g. "the dispatcher session is never a subagent") that must never be violated, regardless of which file or code path you're touching.
3. [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md) — the worktree workflow and the hard constraints around it: `~/lobster/` must always stay on `main`, the cp-then-test pattern for hooks/agent definitions, and the register-after-merge rule for new hooks.

Other documents worth reading depending on what you're changing:

- [`README.md`](README.md) — system architecture overview and the 7-second dispatcher rule
- `.claude/agents/review.md` — if you are (or are spawning) a review agent, this defines what "done" review looks like, including the scope check and the lessons-learned cross-reference
- `.claude/agents/functional-engineer.md` — if you are implementing a GitHub issue end-to-end, this defines the full issue → branch → TDD → PR workflow

### Enforcement note

`.claude/agents/functional-engineer.md` is wired to **programmatically enforce** step 1 above (reading `docs/engineering-lessons-learned.md`) before it starts implementation work — this is not optional for that agent.

No equivalent enforcement exists for any other agent definition, or for humans. Every other agent (`review.md`, `general-purpose`, custom subagents, etc.) and every human contributor is expected to follow the same numbered order **manually**. If you're writing a new agent definition or onboarding a new contributor, point them at this section rather than re-deriving the reading list.

## General contributing guidelines

### Development environment

All feature and fix work happens in a git worktree, never directly on `main` in `~/lobster/`. See `docs/DEVELOPMENT.md` for the full worktree workflow, including how to test hooks and agent definitions locally before they're merged (the cp-then-test pattern).

### Running tests

The dev environment runs in Docker via `docker-compose.dev.yml`:

```bash
make test            # full test suite
make test-unit        # tests/unit/ only
make test-integration  # tests/integration/ only
make test-file FILE=tests/unit/test_skill_manager.py   # a single file
make shell            # interactive shell in the dev container
```

### Commit and PR conventions

- Write atomic, well-documented commits with clear messages.
- Reference the issue or ticket being addressed in the PR description (`Closes #N` for GitHub issues; for Linear-tracked work, state `Implements LINEAR-KEY (url): <summary>` since Linear issues have no auto-close syntax).
- Tests must be written before the implementation they cover, and must be shown to fail without that implementation (see `docs/engineering-lessons-learned.md` and the functional-engineer workflow for the falsifiability check).
- Prefer functional style where the language supports it: pure functions, immutability, composition over inheritance, higher-order functions over imperative loops.

### Migrations

Changes that affect existing installs (new cron entries, new directories, config renames, new service files) need a numbered migration in `scripts/upgrade.sh`, not just `install.sh`. See `.claude/agents/lobster-ops.md` for the migration format.
