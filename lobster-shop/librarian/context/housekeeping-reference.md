# Librarian Housekeeping Reference

Workstream checklists and reference material. Read `behavior/system.md` first for the operating model.

## Before Creating Any PR — Dedup Check

The most common failure in parallel librarian sessions is duplicate PRs. Always run before opening one:

```bash
gh pr list --repo <owner/repo> --search "closes #<N>" --state open
gh pr list --repo <owner/repo> --search "fixes #<N>" --state open
gh pr list --repo <owner/repo> --head "<expected-branch>" --state open
```

If any open PR matches: skip this fix. Log "Skipped: PR #N already open." Each parallel subagent runs this independently.

## Issue Tracker

- Close resolved issues only when the fix **fully covers** the issue. Partial progress → update the description, do not close.
- Close stale issues (>90 days, no clear owner, superseded or no longer relevant). Leave a closing comment: "Closing: no activity 90+ days, superseded by #N."
- Update stale descriptions: add missing context, correct wrong info, sharpen vague titles.
- Label: bug, enhancement, docs, stale, duplicate, blocked, good-first-issue, ready-for-implementation.
- Link and close duplicates. Decompose large issues into sub-issues. Update project tracking after a pass.
- Comment on PRs open >7 days with no activity.
- Tag `ready-for-implementation` with a comment: what's needed + expected outcome.

*Requires user sign-off:* merging PRs; closing issues where resolution is genuinely unclear.

## Codebase Audit

File an issue for each finding (fix inline only for obvious one-line corrections):
- Dead code: unreachable functions, unused imports, commented-out blocks
- Missing test coverage: modules or functions with no tests
- Doc staleness: documentation referencing removed features or outdated paths
- Context file redundancy: overlapping or contradictory instructions

## Workspace and Config

File issues for anything needing human review. Act on clear-cut problems.
- Stale git worktrees: list with last-commit dates; file an issue if pruning looks warranted — do not prune
- Orphaned scripts with no callers: file an issue — do not delete
- Config drift: settings referencing old paths or removed env vars
- Stale scheduled jobs (>14 days no successful run): file an issue and flag

## Managed Projects

Check each directory under `$LOBSTER_PROJECTS`:
- Uncommitted changes or untracked files: note and flag if unexpected
- Repos significantly behind upstream (>30 commits): flag for attention
- Projects with no activity in >60 days: flag if still-active status is unclear
- Worktrees from past agent runs with no open PR: list and flag — do not prune unilaterally

## Memory and Context

- Memory index and files: review each entry for staleness; update or remove stale entries
- Session notes: prune redundant old sessions; update open threads to reflect current state
- Rolling summary: verify it reflects current priorities and open work
- Behavioral rules: review each — still accurate? still needed? Tighten imprecise conditions.
- Handoff and priorities files: update to reflect current state after a housekeeping pass

## Super Librarian: Priority Order

Explicit pre-session instructions override this list.

1. In-flight work (soak, PRs awaiting merge)
2. In-flight projects (per session notes)
3. Issue and task hygiene
4. Memory and context housekeeping
5. Managed project maintenance
6. Research and deep reading
7. Test coverage and doc improvements
8. Session notes and context catchup

## Super Librarian: Failure Modes

- **Stalling** — waiting for prompts instead of working
- **Session collapse** — brief activity then stopping instead of sustained work
- **Passive observation** — monitoring instead of executing
- **Scan instead of work** — "found 12 issues" when the job is to close, update, and triage them
