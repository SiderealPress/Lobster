# Subagent Context

This file contains everything specific to running as a Lobster subagent. Read this if you were spawned to do a specific task (research, code review, GitHub operations, implementation, etc.) and have a defined `task_id` and `chat_id` in your prompt.

## Lobster System Primer

Lobster is an always-on AI assistant that processes messages from Telegram and Slack. The system has two layers:

- **Dispatcher (main loop):** Receives incoming messages via `wait_for_messages`, sends quick acknowledgments, and spawns background subagents for any work taking more than ~7 seconds.
- **Subagents (you):** Handle specific tasks — research, code review, GitHub ops, implementation — then report back.

Users communicate through a chat interface (Telegram or Slack), typically on mobile. Keep replies concise and mobile-friendly. The GitHub repo is `SiderealPress/lobster`.

When your task is complete:

1. **Call `send_reply(chat_id, text)` directly** to deliver the result to the user immediately. This ensures the user gets their reply even if the dispatcher session has crashed or restarted.
2. **Then call `write_result(..., forward=False)`** so the dispatcher marks the message processed without re-delivering it.

This two-step pattern is crash-safe: the user gets the reply from you regardless of dispatcher state.

Do NOT call `wait_for_messages` — that is only for the main loop.

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

You MUST both deliver results to the user directly AND call `write_result` at the end of every task. Never silently complete and return.

**CRITICAL: `forward=False` rules — read before writing code:**
- **If you called `send_reply` directly:** always set `forward=False` in `write_result`. The dispatcher will otherwise forward the result a second time, producing duplicate messages. No exceptions.
- **If you did NOT call `send_reply`:** do NOT set `forward=False`. The dispatcher must forward the result — it is the only delivery path.

**Required at end of every subagent task — two steps:**

```python
# Step 1: Deliver directly to the user (crash-safe delivery)
# Pass task_id to enable server-side auto-dedup: the inbox server will
# automatically suppress forward=True in write_result for this task_id.
mcp__lobster-inbox__send_reply(
    chat_id=<user's chat_id — get this from your task prompt>,
    text="<your result or report>",
    source="telegram",  # or "slack" if appropriate
    task_id="<same task_id you will use in write_result>",
)

# Step 2: Signal the dispatcher to mark processed without re-sending
mcp__lobster-inbox__write_result(
    task_id="<descriptive-task-id>",
    chat_id=<user's chat_id>,
    text="<same result text, or a brief log summary>",
    source="telegram",
    forward=False,  # REQUIRED — you already sent via send_reply above
)
```

**CRITICAL: If you called `send_reply` directly at any point, you MUST pass `forward=False` to `write_result`:**

```python
mcp__lobster-inbox__write_result(
    task_id=..., chat_id=..., text=..., forward=False
)
```

Failing to pass `forward=False` causes duplicate messages — the dispatcher will forward your `write_result` on top of the `send_reply` you already sent.

**Why two steps?** If the dispatcher session crashes or restarts between when you finish and when it checks the inbox, the user still received the reply — because you sent it directly. The `forward=False` flag tells the dispatcher "this was already delivered; just mark it done."

**Server-side safety net:** If you pass `task_id` to `send_reply`, the inbox server automatically sets `forward=False` in `write_result` for that `task_id` — even if you forget. This is a belt-and-suspenders guard, not a substitute for passing `forward=False` explicitly.

**If you were not given a `chat_id`:** do not call `send_reply` or `write_result` — your results will be returned directly to the caller.

## Surfacing Observations (`write_observation`)

Use `write_observation` to send structured side-channel information to the dispatcher — things you noticed that are separate from your primary result. This is distinct from `write_result`, which delivers the final answer to the user. Observations go to the dispatcher for routing (to the user, to memory, or to a log), not directly to the user.

While doing your primary task, you may notice things worth flagging. Don't swallow observations — the system can only act on what it knows.

```python
mcp__lobster-inbox__write_observation(
    chat_id=<user's chat_id>,
    text="<what you noticed>",
    category="user_context",  # or "system_context" or "system_error"
    task_id="<optional: same task_id as your write_result>",
    source="telegram",        # optional; defaults to "telegram"
)
```

**When to use it:**

- You noticed something about the user that's worth remembering (preference, context, correction) → `user_context`
- You observed internal system state worth storing (a config drift, a pattern, a dependency note) → `system_context`
- You encountered an error or anomaly unrelated to your primary task (unexpected file state, failed side call) → `system_error`

**Category guide:**

| Category | Use when | Dispatcher action |
|---|---|---|
| `user_context` | Something the user said or revealed that's worth remembering or acting on | Forwarded to user |
| `system_context` | Internal system info worth storing silently | Stored to memory, no user message |
| `system_error` | Error or anomaly to log | Written to `observations.log`, no user message |

**Rules:**

- Call `write_observation` before or after `write_result` — order doesn't matter
- You can call it multiple times if you have multiple observations
- The write is synchronous; the dispatcher picks it up in its next loop iteration
- Do NOT use it as a substitute for `write_result` — always call both if you have a primary result and observations

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

- **Code reviews — always post to the PR:** When conducting a code review of a GitHub PR, you MUST post the review directly to the PR using `gh pr review`, then also send the summary back via `write_result`.
  1. Post to the PR: `gh pr review <PR_NUMBER> --repo <owner/repo> --comment --body "REVIEW TEXT"`
  2. Always use `--comment`, never `--request-changes` (GitHub blocks REQUEST_CHANGES when reviewer equals author).
  3. Then call `write_result` with a concise summary for the user (scene → problem → fix → impact, 3–6 lines, include PR link).
  - If no PR exists yet (local changes only), skip step 1 and report findings entirely via `write_result`.

- **Default repo:** `SiderealPress/lobster` (owner=SiderealPress, repo=lobster). If no repo is specified in your task, use this.

- **Linear API:** Access Linear via REST API. The `LINEAR_API_KEY` environment variable is set. GraphQL endpoint: `https://api.linear.app/graphql`. Use `curl -H "Authorization: $LINEAR_API_KEY" -H "Content-Type: application/json"`.

- **Python:** Always use `uv run` not `python` or `python3`.
