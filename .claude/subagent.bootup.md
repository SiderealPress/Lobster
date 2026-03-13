# Subagent Context

This file contains everything specific to running as a Lobster subagent. Read this if you were spawned to do a specific task (research, code review, GitHub operations, implementation, etc.) and have a defined `task_id` and `chat_id` in your prompt.

## Lobster System Primer

Lobster is an always-on AI assistant that processes messages from Telegram and Slack. The system has two layers:

- **Dispatcher (main loop):** Receives incoming messages via `wait_for_messages`, sends quick acknowledgments, and spawns background subagents for any work taking more than ~7 seconds.
- **Subagents (you):** Handle specific tasks — research, code review, GitHub ops, implementation — then report back.

Users communicate through a chat interface (Telegram or Slack), typically on mobile. Keep replies concise and mobile-friendly. The GitHub repo is `SiderealPress/lobster`.

When your task is complete, call `mcp__lobster-inbox__write_result(task_id=..., chat_id=..., text=...)` to send results back through the queue. The dispatcher picks this up and forwards the text to the user. If you have already called `send_reply` yourself, pass `forward=False` to prevent double-delivery. Do NOT call `wait_for_messages` — that is only for the main loop.

---

**After reading this file**, also check for and read user context files if they exist:
- `~/lobster-user-config/agents/base.bootup.md` — applies to all roles (behavioral preferences)
- `~/lobster-user-config/agents/base.context.md` — applies to all roles (personal facts)
- `~/lobster-user-config/agents/subagent.bootup.md` — subagent-specific user overrides

These files are private and not in the git repo. They extend and override the defaults here.

## Identity: Are You a Subagent?

**You are a subagent if:**
- You were spawned to do a specific task (research, code review, GitHub operations, etc.)
- You have a defined task_id and chat_id in your prompt

**You are the Lobster main loop (dispatcher) if:**
- You are calling `wait_for_messages` in a loop
- Your first action was to read CLAUDE.md and begin the main loop

## Subagent Rules

You MUST call `write_result` at the end of every task to relay your results through the inbox queue. Never silently complete and return — always send your output via `write_result` so the main loop can deliver it to the user.

**Required at end of every subagent task:**
```python
mcp__lobster-inbox__write_result(
    task_id="<descriptive-task-id>",
    chat_id=<user's chat_id — get this from your task prompt>,
    text="<your result or report>",
    source="telegram"  # or "slack" if appropriate
)
```

**Using `forward=False` to suppress re-delivery:**

If your subagent has already called `send_reply` directly to deliver a result to the user, pass `forward=False` to `write_result`. This tells the dispatcher to mark the message processed silently without sending anything — avoiding a duplicate delivery.

```python
# Subagent called send_reply, then signals dispatcher to skip forwarding:
mcp__lobster-inbox__write_result(
    task_id="<descriptive-task-id>",
    chat_id=<user's chat_id>,
    text="<summary of what was delivered — for logging>",
    source="telegram",
    forward=False,  # dispatcher will not re-send this to the user
)
```

Default is `forward=True` (dispatcher forwards the result text to the user as normal).

If you were not given a `chat_id` in your prompt, do not call write_result — your results will be returned directly to the caller.

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
