# Librarian Housekeeping Reference

Tool-specific checklists and reference material for common librarian workstreams. Read `behavior/system.md` first for the operating model.

## Issue Tracker Checklist

**Before closing any resolved issue:** confirm the linked PR was merged and fully covers the issue (not partial progress). For stale closures, leave a comment: "Closing: no activity in 90+ days and superseded by #N."

**Labeling conventions:** bug, enhancement, docs, stale, duplicate, blocked, good-first-issue, ready-for-implementation

**Implementation readiness tagging:** tag `ready-for-implementation` when an issue is well-scoped with a clear expected outcome; leave a comment with what's needed and the expected outcome.

**PR review hygiene:** comment on PRs open >7 days with no activity; update PR descriptions that no longer match the current code.

## Dedup Check (Required Before Any PR)

```bash
gh pr list --repo <owner/repo> --search "closes #<issue-number>" --state open
gh pr list --repo <owner/repo> --search "fixes #<issue-number>" --state open
gh pr list --repo <owner/repo> --head "<expected-branch-name>" --state open
```

If any open PR is returned: skip this fix. Log: "Skipped: PR already open for #N." Parallel agents must each independently run this check.

## Codebase Audit Checklist

File an issue for each finding — fix inline only for one-line obvious corrections.

- Dead code: unreachable functions, unused imports, commented-out blocks
- Missing test coverage: modules or functions with no tests
- Doc staleness: documentation referencing removed features or outdated paths
- Context file redundancy: overlapping or contradictory instructions across behavior/context files

## Workspace and Config Audit Checklist

File issues for things needing human review. Act directly on clear-cut problems.

- Stale git worktrees (`git worktree list`): list any with no open PR and their last-commit date; file an issue if pruning looks warranted — do not prune
- Orphaned scripts with no callers: file an issue listing them — do not delete
- Config drift: settings referencing old paths or env vars that no longer exist
- Stale scheduled jobs (no successful run in >14 days): file an issue and flag to user

## Project Subdirectory Checklist (`$LOBSTER_PROJECTS`)

Check each managed project directory:

- Uncommitted changes or untracked files: note them, file an issue if unexpected
- Stale branches with no open PR and no recent commits: file an issue listing them — do not delete
- Repos significantly behind their upstream remote (`git fetch && git status`): note the lag, flag if >30 commits
- Projects with no activity in >60 days: note whether still active; flag to user if unclear
- Worktrees created by past agent runs that are no longer needed: list and flag — do not prune unilaterally

## Memory and Context Housekeeping Checklist

- `MEMORY.md` and memory files: review each entry for staleness or inaccuracy; update or remove stale entries
- Session notes: prune very old or redundant sessions; update open threads to reflect current state
- Rolling summary: verify it reflects current priorities and open work
- Behavioral rules (IFTTT): review each rule — still accurate? Still needed? Tighten imprecise conditions
- Handoff and priorities files: update to reflect current state after housekeeping pass

## Super Librarian: Default Priority Order

Check session notes first — explicit pre-session instructions override this list.

1. PR and soak test anything in flight on the local integration branch
2. Move forward on current in-flight projects (per session notes)
3. Issue and task hygiene
4. Memory and context housekeeping
5. Project subdirectory maintenance
6. Research and deep reading
7. Test coverage and doc improvements
8. Session notes and context catchup

## Super Librarian: Failure Modes to Avoid

- **Stalling** — waiting for the user to prompt every action
- **Session collapse** — brief activity then stopping instead of sustaining momentum
- **Passive observation** — monitoring rather than executing
- **Scan instead of work** — "found 12 stale issues" when the job is to close, update, and triage them
