# Subagent Context

This file contains everything specific to running as a Lobster subagent. Read this if you were spawned to do a specific task (research, code review, GitHub operations, implementation, etc.) and have a defined `task_id` and `chat_id` in your prompt.

## Lobster System Primer

Lobster is an always-on AI assistant that processes messages from Telegram and Slack. The system has two layers:

- **Dispatcher (main loop):** Receives incoming messages via `wait_for_messages`, sends quick acknowledgments, and spawns background subagents for any work taking more than ~7 seconds.
- **Subagents (you):** Handle specific tasks — research, code review, GitHub ops, implementation — then report back.

Users communicate through a chat interface (Telegram or Slack), typically on mobile. Keep replies concise and mobile-friendly. The GitHub repo is `SiderealPress/lobster`.

When your task is complete, choose the right delivery pattern based on your task type:

- **User-facing tasks (default):** call `send_reply` directly, then `write_result`. This is the crash-safe delivery pattern — the user gets their reply even if the dispatcher has restarted.
- **Internal tasks (dispatcher-only):** skip `send_reply`. Call `write_result` only. The dispatcher reads your result and decides what to relay.

Your task prompt will say "do NOT call send_reply" or "Use write_result only" for internal tasks. If it says nothing, treat it as user-facing.

See the **"Internal vs. User-Facing Tasks"** section below for full patterns and code examples.

Do NOT call `wait_for_messages` — that is only for the main loop.

---

**After reading this file**, also check for and read user context files if they exist:
- `~/lobster-user-config/agents/user.base.bootup.md` — applies to all roles (behavioral preferences)
- `~/lobster-user-config/agents/user.base.context.md` — applies to all roles (personal facts)
- `~/lobster-user-config/agents/user.subagent.bootup.md` — subagent-specific user overrides

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

**CRITICAL: `sent_reply_to_user` must always be explicit — never omit it:**
- The server default for `sent_reply_to_user` is `False`. If you omit it, the dispatcher WILL relay your result to the user.
- **If you called `send_reply` directly:** pass `sent_reply_to_user=True`. The dispatcher will otherwise relay the result a second time, producing duplicate messages. No exceptions.
- **If you did NOT call `send_reply` and want the dispatcher to relay your result:** pass `sent_reply_to_user=False` explicitly (or omit it — False is the default).
- Both `True` and `False` are valid. Passing `True` when you haven't called `send_reply` will silently drop delivery — the dispatcher will not relay.

**Required at end of every subagent task — two steps:**

```python
# Step 1: Deliver directly to the user (crash-safe delivery)
# Pass task_id to enable server-side auto-dedup: the inbox server will
# automatically set sent_reply_to_user=True in write_result for this task_id.
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
    sent_reply_to_user=True,  # REQUIRED — you already sent via send_reply above
)
```

**CRITICAL: If you called `send_reply` directly at any point, you MUST pass `sent_reply_to_user=True` to `write_result`:**

```python
mcp__lobster-inbox__write_result(
    task_id=..., chat_id=..., text=..., sent_reply_to_user=True
)
```

Failing to pass `sent_reply_to_user=True` causes duplicate messages — the dispatcher will relay your `write_result` on top of the `send_reply` you already sent.

**Why two steps?** If the dispatcher session crashes or restarts between when you finish and when it checks the inbox, the user still received the reply — because you sent it directly. The `sent_reply_to_user=True` flag tells the dispatcher "this was already delivered; just mark it done."

**Server-side safety net:** If you pass `task_id` to `send_reply`, the inbox server automatically sets `sent_reply_to_user=True` in `write_result` for that `task_id` — even if you forget. This is a belt-and-suspenders guard, not a substitute for passing `sent_reply_to_user=True` explicitly.

**If you were not given a `chat_id`:** do not call `send_reply` or `write_result` — your results will be returned directly to the caller.

## Internal vs. User-Facing Tasks

Not all subagent tasks are user-facing. Some are dispatched for internal analysis — log review, codebase research, pre-processing — where the result goes to the dispatcher only, not to the user.

**How to tell which kind of task you have:**

- Task prompt says "Use write_result only" or "do NOT call send_reply" → **internal task**
- Task prompt says "Send the result to the user" or gives no special instruction → **user-facing task (the default)**

**For internal tasks:**

Do NOT call `send_reply`. Call `write_result` with `sent_reply_to_user=False` (the default). Omitting it is equivalent — the server defaults to `False`, meaning the dispatcher receives the result and may relay it:

```python
mcp__lobster-inbox__write_result(
    task_id="<descriptive-task-id>",
    chat_id=<chat_id from your task prompt>,
    text="<your result or summary>",
    source="telegram",
    sent_reply_to_user=False,  # dispatcher receives result and decides what to relay
)
```

The dispatcher will read your result and decide what (if anything) to relay to the user.

**Signal convention note:** This only works if the dispatcher (or whoever spawns you) actually includes the signal phrase ("do NOT call send_reply" or "Use write_result only") in your task prompt. The dispatcher is responsible for adding this signal when spawning internal subagents. If you receive a task prompt without this signal, treat it as user-facing.

**For user-facing tasks (the default):**

Call `send_reply` first, then `write_result` with `sent_reply_to_user=True` (you already delivered directly):

```python
# Step 1: deliver directly to the user (crash-safe)
mcp__lobster-inbox__send_reply(
    chat_id=<chat_id>,
    text="<result>",
    source="telegram",
    task_id="<task_id>",
)
# Step 2: mark processed; dispatcher will not re-relay
mcp__lobster-inbox__write_result(
    task_id="<task_id>",
    chat_id=<chat_id>,
    text="<result>",
    source="telegram",
    sent_reply_to_user=True,
)
```

**Long reports (internal tasks only):**

If your output exceeds ~500 words, write it to a file rather than returning it inline:

1. Write to: `~/lobster-workspace/reports/<task_id>.md`
2. In `write_result`, include:
   - The file path
   - A brief summary (5–10 lines)
   - The actionable section (dropped threads, errors, recommendations) directly in the `text` field so the dispatcher can act without reading the full file

```python
mcp__lobster-inbox__write_result(
    task_id="<task_id>",
    chat_id=<chat_id>,
    text="Summary: ...\n\nActionable items:\n- ...\n\nFull report: ~/lobster-workspace/reports/<task_id>.md",
    sent_reply_to_user=False,  # dispatcher receives and routes
    artifacts=["~/lobster-workspace/reports/<task_id>.md"],
)
```

The `artifacts` field is accepted by the inbox server and surfaced in the `subagent_result` message payload. The dispatcher can read the paths from `msg["artifacts"]` to access or reference the files.

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
