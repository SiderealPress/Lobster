# Librarian Housekeeping Reference

Tool-specific checklists for common librarian tasks. These complement the operating model in `behavior/system.md` — read that first.

## Issue Tracker Checklist (GitHub)

**Before closing any resolved issue:**
- Confirm the linked PR was merged and fully covers the issue (not just partial progress)
- Leave a brief comment if closing stale: "Closing: no activity in 90+ days and superseded by #N"

**Labeling conventions:** bug, enhancement, docs, stale, duplicate, blocked, good-first-issue, ready-for-implementation

**Implementation readiness tagging:**
- Tag `ready-for-implementation` when an issue is well-scoped with a clear expected outcome
- Leave a comment: what's needed + expected outcome

**PR review hygiene:**
- Comment on PRs open >7 days with no activity, flagging them for attention
- Update PR description if it no longer matches the current code

## Dedup Check (Required Before Any PR)

Always run this before creating a PR to avoid accumulating duplicates:

```bash
gh pr list --repo <owner/repo> --search "closes #<issue-number>" --state open
gh pr list --repo <owner/repo> --search "fixes #<issue-number>" --state open
gh pr list --repo <owner/repo> --head "<expected-branch-name>" --state open
```

If any open PR is returned: skip this fix. Log: "Skipped: PR already open for #N."

Parallel agents must each independently run this check.

## Codebase Audit Checklist

File an issue for each finding — don't fix inline unless it's a one-line obvious correction.

- Dead code: unreachable functions, unused imports, commented-out blocks
- Missing test coverage: modules or functions with no tests
- Doc staleness: documentation referencing removed features or outdated paths
- Bootup file redundancy: overlapping or contradictory instructions across context files

## Workspace and Config Audit Checklist

File issues for things that need human review. Act directly on clear-cut problems.

- Stale git worktrees (`git worktree list`): list any with no open PR and their last-commit date; file an issue if pruning looks warranted — do not prune
- Orphaned scripts with no callers: file an issue listing them — do not delete
- Config drift: settings referencing old paths or env vars that no longer exist
- Stale scheduled jobs (no successful run in >14 days): file an issue and flag to user

## Memory and Context Housekeeping Checklist

- `MEMORY.md`: review each entry for staleness or inaccuracy; update or remove stale entries
- Memory database: no direct deletion; flag stale entries for review
- Session notes: prune very old or redundant sessions; update open threads to reflect current state
- Rolling summary: verify it reflects current priorities and open work
- Behavioral rules (IFTTT): review each rule — is it still accurate? Still needed? Tighten imprecise conditions.
- Handoff and priorities files: update to reflect the current state of work after a housekeeping pass
