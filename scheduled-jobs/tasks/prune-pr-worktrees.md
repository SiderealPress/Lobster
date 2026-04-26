# prune-pr-worktrees

Remove stale git worktrees from merged or closed PRs in ~/lobster-workspace/projects/.

Run:
  uv run ~/lobster/scripts/prune-pr-worktrees.py --age-days 7

Log output to ~/lobster-workspace/logs/prune-worktrees.log.
No user notification unless the script exits non-zero.
