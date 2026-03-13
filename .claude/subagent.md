# Subagent Context

This file contains everything specific to running as a Lobster subagent. Read this if you were spawned to do a specific task (research, code review, GitHub operations, implementation, etc.) and have a defined `task_id` and `chat_id` in your prompt.

**After reading this file**, also check for and read user context files if they exist:
- `~/lobster-workspace/.claude/user.md` — applies to all roles
- `~/lobster-workspace/.claude/subagent.md` — subagent-specific user overrides

These files are private and not in the git repo. They extend and override the defaults here.

## Identity: Are You a Subagent?

**You are a subagent if:**
- You were spawned to do a specific task (research, code review, GitHub operations, etc.)
- You have a defined task_id and chat_id in your prompt

**You are the Lobster main loop (dispatcher) if:**
- You are calling `wait_for_messages` in a loop
- Your first action was to read CLAUDE.md and begin the main loop

## Subagent Rules

Subagents deliver results in two steps:

1. **Send the full reply directly to the user** via `send_reply`
2. **Call `write_result` with a short 1-3 line summary** so the dispatcher can log and mark the task complete

Never silently complete and return — always do both steps.

**Required pattern at end of every subagent task:**
```python
# Step 1: Deliver full content to the user
mcp__lobster-inbox__send_reply(
    chat_id=<user's chat_id>,
    text="<your full result or report>",
    source="telegram"  # or "slack" if appropriate
)

# Step 2: Notify the dispatcher with a short summary (NOT the full text)
mcp__lobster-inbox__write_result(
    task_id="<descriptive-task-id>",
    chat_id=<user's chat_id>,
    text="Done: [1-3 line summary of what was accomplished]",
    source="telegram"  # or "slack" if appropriate
)
```

The `write_result` text is a summary for dispatcher awareness only — the user already received the full reply. Keep it to 1-3 lines. Start it with `Done:` so the dispatcher knows the user was already notified.

If you were not given a `chat_id` in your prompt, do not call `send_reply` or `write_result` — your results will be returned directly to the caller.

## Model Selection

Lobster uses a tiered model strategy to balance cost and quality. Each subagent has an explicit model assigned in its `.md` frontmatter. When delegating work, the dispatcher does not need to specify a model — the agent definition handles it.

**Model tiers:**

| Tier | Model | Use For | Cost |
|------|-------|---------|------|
| **High** | `opus` | Complex coding, architecture, debugging | 1x (baseline) |
| **Standard** | `sonnet` | Planning, research, execution, synthesis | 0.6x |
| **Light** | `haiku` | Verification, plan-checking, integration checks | 0.2x |

**Agent model assignments:**

- **Opus**: `functional-engineer`, `gsd-debugger` -- tasks requiring deep reasoning
- **Sonnet**: `gsd-executor`, `gsd-planner`, `gsd-phase-researcher`, `gsd-codebase-mapper`, `gsd-research-synthesizer`, `gsd-roadmapper`, `gsd-project-researcher` -- structured work
- **Haiku**: `gsd-verifier`, `gsd-plan-checker`, `gsd-integration-checker` -- pass/fail evaluation
- **Inherit (Sonnet)**: `general-purpose` -- inherits from `CLAUDE_CODE_SUBAGENT_MODEL` env var

**When to override:** If a task normally handled by a Sonnet agent requires unusually deep reasoning (e.g., a complex multi-system execution plan), consider using `functional-engineer` (Opus) instead.

**For general background tasks** with no specific agent type, use `subagent_type='lobster-generalist'` rather than omitting `subagent_type` or using an untyped Agent call. The `lobster-generalist` agent is the correct default for open-ended background work that doesn't map to a more specialized agent.

## Tooling conventions

- **GitHub operations:** Use `gh` CLI (via Bash tool) for all GitHub operations — posting PR reviews, merging PRs, creating issues, etc. Do NOT use `mcp__github__*` MCP tools in agent code.
  - Post a PR review: `gh pr review <number> --comment --body "..." --repo SiderealPress/lobster`
  - Merge a PR: `gh pr merge <number> --squash --repo SiderealPress/lobster`
  - Create an issue: `gh issue create --title "..." --body "..." --repo SiderealPress/lobster`

- **Default repo:** `SiderealPress/lobster` (owner=SiderealPress, repo=lobster). If no repo is specified in your task, use this.

- **Linear API:** Access Linear via REST API. The `LINEAR_API_KEY` environment variable is set. GraphQL endpoint: `https://api.linear.app/graphql`. Use `curl -H "Authorization: $LINEAR_API_KEY" -H "Content-Type: application/json"`.

- **Python:** Always use `uv run` not `python` or `python3`.
