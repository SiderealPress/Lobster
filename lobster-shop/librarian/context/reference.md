# Librarian Mode — Reference Context

## Staleness Thresholds (Defaults, Configurable)

| Item | Stale After |
|------|------------|
| GitHub issue (no activity) | 90 days |
| Scheduled job (no successful run) | 14 days |
| Git worktree (no open PR) | 30 days |
| Contact without interaction (if Kissinger active) | 30 days |

## Standard Issue Labels

Use these when labeling issues:

| Label | Meaning |
|-------|---------|
| `bug` | Confirmed defect |
| `enhancement` | Feature request or improvement |
| `docs` | Documentation update needed |
| `stale` | No activity, candidate for closing |
| `duplicate` | Duplicate of another issue |
| `blocked` | Waiting on external dependency |
| `good-first-issue` | Well-scoped, low risk, good for a new contributor |
| `ready-for-implementation` | Well-scoped, approved approach, ready to build |
| `needs-discussion` | Unclear scope or approach, needs a decision |
| `wontfix` | Intentionally not addressing |

## Audit Checklist

### Codebase
- [ ] Unused imports (Python: `ruff check --select F401`)
- [ ] Dead functions (no callers)
- [ ] Commented-out code blocks (>5 lines)
- [ ] TODOs/FIXMEs older than 60 days (check git blame)
- [ ] README references non-existent files or commands
- [ ] Bootup .md files with overlapping or contradictory instructions
- [ ] Test coverage gaps (modules with no corresponding test file)

### Workspace
- [ ] `git worktree list` — identify stale worktrees (no open PR, >30 days old); file an issue listing them, do NOT prune
- [ ] `~/lobster/scripts/` — scripts not referenced in install.sh, upgrade.sh, or cron
- [ ] `~/lobster/scheduled-tasks/` — tasks not registered in jobs.json
- [ ] `~/lobster-workspace/scheduled-jobs/` — jobs disabled or not run in >14 days
- [ ] Env vars in config.env that no longer match what the code expects

### Issues
- [ ] All open issues have at least one label
- [ ] Issues >90 days old with no activity reviewed for staleness
- [ ] Duplicate issues cross-linked and one closed
- [ ] Issues with "done" or "fixed" in comments but still open — verify and close

## PR Conventions for Small Fixes

- Branch name: `librarian/fix-<short-description>`
- PR title: `[Librarian] <short description>`
- PR body: Explain what was wrong and what was changed; link the issue if one exists
- Always request review; never self-merge
- **Always run the dedup check before `gh pr create`** (see behavior/system.md)

## Deduplication Check (Required Before Every PR)

Run both checks before opening any PR:

```bash
# 1. Search by issue number in PR body (covers closes/fixes/resolves variants)
gh pr list --repo <owner/repo> --search "closes #<N>" --state open
gh pr list --repo <owner/repo> --search "fixes #<N>" --state open
gh pr list --repo <owner/repo> --search "resolves #<N>" --state open

# 2. Search by expected branch name
gh pr list --repo <owner/repo> --head "librarian/fix-<description>" --state open
```

If either returns a result: skip. Log the skip with the existing PR number.

This prevents the duplicate PR problem that occurs when librarian runs in multiple waves or with parallel agents. Each wave and each parallel agent must run this check independently.

## Parallel Workstream Template

When launching parallel agents, assign each a clear scope and output format:

```
Agent A — Issue Triage
Scope: All open GitHub issues in [repo]
Output: List of actions taken (closed N, labeled N, linked N duplicates)
File: One GitHub comment per changed issue

Agent B — Codebase Audit
Scope: ~/lobster-workspace/projects/[project]/
Output: List of findings filed as GitHub issues
File: Issues tagged `librarian-audit`

Agent C — Workspace/Config Audit
Scope: ~/lobster/, ~/lobster-config/, ~/lobster-workspace/
Output: List of findings filed as tasks or issues
```

Note: Each parallel agent is independently responsible for the dedup check before any PR creation.
