# Subagent Context

This file contains everything specific to running as a Lobster subagent. Read this if you were spawned to do a specific task (research, code review, GitHub operations, implementation, etc.) and have a defined `task_id` and `chat_id` in your prompt.

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
