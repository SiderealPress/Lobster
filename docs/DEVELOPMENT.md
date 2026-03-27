# Lobster Development Guide

This guide covers conventions for developing Lobster features using git worktrees, the standard workflow for all feature and fix branches.

## Worktree Workflow

All feature branch work happens in git worktrees placed at `~/lobster-workspace/projects/<branch-name>/`. The main repository at `~/lobster/` always stays on `main`.

### Creating a worktree

```bash
cd ~/lobster
git worktree add -b feature/my-feature ~/lobster-workspace/projects/my-feature main
```

Or, to check out an existing branch:

```bash
git worktree add ~/lobster-workspace/projects/my-feature feature/my-feature
```

Work inside the worktree directory. Commit and push from there. Open your editor pointing at the worktree path.

### Removing a worktree after merge

```bash
cd ~/lobster
git worktree remove ~/lobster-workspace/projects/my-feature
git branch -d feature/my-feature
```

---

## Convention 1: ~/lobster/ must stay on main

**Never run `git checkout <branch>` inside `~/lobster/`.** The dispatcher process reads configuration, hooks, and agent definitions from `~/lobster/` at runtime. Switching branches there changes what the live system sees and can break the running dispatcher in unpredictable ways.

All feature branch work uses worktrees at `~/lobster-workspace/projects/<branch>/`. This is not a preference — it is a system constraint.

If you need to verify which branch `~/lobster/` is on:

```bash
git -C ~/lobster branch --show-current
# Should always print: main
```

---

## Convention 2: The cp-then-test pattern

Hook scripts and agent definitions are referenced by **absolute path** in `~/.claude/settings.json`, which points into `~/lobster/hooks/` and `~/lobster/.claude/agents/`. A file that lives only in a worktree is **not** picked up at runtime — the live dispatcher never sees it.

To test a new or modified hook or agent definition before merging:

```bash
# For hook scripts:
cp ~/lobster-workspace/projects/<branch>/hooks/my-hook.py ~/lobster/hooks/my-hook.py

# For agent definitions:
cp ~/lobster-workspace/projects/<branch>/.claude/agents/my-agent.md ~/lobster/.claude/agents/my-agent.md
```

Test the live behavior. When you are satisfied:

- Do **not** revert the copy manually — the file will be correct in `~/lobster/` once the PR lands on main.
- If you need to abandon the test before merging, delete the copy from `~/lobster/hooks/` or `~/lobster/.claude/agents/`.

This pattern lets you run real traffic against a change without touching the main branch checkout.

---

## Convention 3: Register-after-merge rule

**Never update `~/.claude/settings.json` to register a hook before the hook file exists in `~/lobster/hooks/`** (i.e., before the PR is merged to main).

If `settings.json` references a hook path that does not exist on disk, every Claude tool call fails with a hook error. This can completely paralyze the dispatcher — no tools work, no messages are processed.

This is what caused the post-compact-enforcement incident.

### Correct sequence for adding a new hook

1. Develop the hook script in your worktree (`~/lobster-workspace/projects/<branch>/hooks/`)
2. (Optional) Copy to `~/lobster/hooks/` for live testing using the cp-then-test pattern above
3. Open a PR and get it reviewed
4. Merge the PR — the file now exists in `~/lobster/hooks/` via main
5. Only then: run `install.sh` to register the hook in `~/.claude/settings.json`, or rely on the idempotent registration block in `install.sh` if it already covers the hook

### Why this order matters

`install.sh` is designed to be idempotent and safe to re-run. It checks for existing entries before adding new ones. If your new hook is wired into `install.sh`, running it after merge is all you need. If you are registering manually, wait until after merge.

---

## Post-Update Checklist (VPS)

After pulling updates on the VPS (`git pull` + `uv pip install -e .`):

1. **Fix file permissions**: `chmod +x scripts/claude-persistent.sh`
   - `git pull` can change file modes (755→644), which silently breaks the tmux launch
   - Note: `claude-wrapper.sh` is superseded by `claude-persistent.sh` and no longer used
2. **Verify auth**: Test that Claude can authenticate — see `docs/REMOTE-AUTH.md`
3. **Restart services**: `systemctl restart lobster-claude && lobster restart`
4. **Verify startup**: `tail -f /home/lobster/lobster-workspace/logs/claude-persistent.log`
   - Should see `"Starting fresh session (attempt 1)..."` without immediate exit

---

## YAML Frontmatter in Subagent Prompts

The `auto-register-agent.py` PostToolUse hook reads structured metadata from subagent prompts using YAML frontmatter. New subagents should use this format instead of the legacy text pattern:

```yaml
---
task_id: my-task-123
chat_id: ADMIN_CHAT_ID_REDACTED
source: telegram
reply_to_message_id: 10924
---
```

The hook uses this to register the subagent in `agent_sessions.db` immediately upon spawn. Recognized fields: `task_id`, `chat_id`, `source`, `reply_to_message_id`.

Legacy format (still supported for backward compat): `Your task_id is: my-task-123`

See `hooks/auto-register-agent.py` for full details.

---

## Convention 4: The Dogfooding Gate

**Every PR that touches runtime code must be cleared through the dogfooding gate before merging to main.** This is a hard prerequisite alongside code review and smoke testing.

### What the gate is

The dogfooding gate is the formal signal that a PR has been running long enough on the local integration branch (`local-dev`) to be trusted. It closes the gap between "merged to local-dev for testing" and "ready to ship to main."

### Soak period

A PR must run on `local-dev` for **at least 2 hours** without incident before it can be cleared. An incident is any error, regression, or behavior change caused by the branch that required action (restart, revert, workaround). Unrelated system failures do not count.

**Exceptions:**
- Doc/prompt-only PRs (no code changes): soak not required.
- Infrastructure/install changes tested via Docker: Docker run substitutes for local soak.

### Clearing the gate

After the soak period, the operator clears the PR with `/dogfooded <PR-number>`. This is an explicit human acknowledgment that the branch has been observed in production without incident.

Until the `/dogfooded` command is fully implemented (see [issue #917](https://github.com/SiderealPress/lobster/issues/917)), clearance is handled by the dispatcher as a verbal confirmation from the user, recorded in the PR walkthrough notes.

### Gate status in PR walkthroughs

When presenting a PR for merge consideration, always include dogfooding status:
- "Not yet deployed to local-dev" — gate is not started
- "In soak since [time] — [elapsed]h / 2h minimum" — gate is running
- "Cleared by /dogfooded at [time]" — gate is passed

### Why this gate exists

The informal "used in practice" requirement had no forcing function. There was no moment where anyone consciously declared a PR ready. The dogfooding gate creates that moment.

---

## Related documentation

- `.claude/sys.dispatcher.bootup.md` — runtime behavior and the worktree constraint from the dispatcher's perspective
- `docs/engineering-lessons-learned.md` — recurring patterns to check during PR review
- `docs/REMOTE-AUTH.md` — headless OAuth re-authentication for the VPS
- `CLAUDE.md` — full system architecture and key directories
