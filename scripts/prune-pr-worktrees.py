#!/usr/bin/env python3
"""
Prune merged/closed PR worktree directories from ~/lobster-workspace/projects/.

For each directory in the projects dir:
  1. Detect if it's a git worktree (`.git` is a file, not a dir)
  2. Get the current branch name
  3. Get the remote origin URL → derive owner/repo
  4. Check GitHub for PR status on that branch
  5. If the PR is merged or closed AND the dir is ≥ AGE_DAYS_THRESHOLD old:
     - In dry-run mode: print what would be removed
     - In live mode: remove the worktree via `git worktree remove` then delete dir

Usage:
    uv run scripts/prune-pr-worktrees.py [--dry-run] [--age-days N] [--projects-dir PATH]

Fixes: issue #1626
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path


DEFAULT_PROJECTS_DIR = Path.home() / "lobster-workspace" / "projects"
DEFAULT_AGE_DAYS = 7
LOG_FILE = Path.home() / "lobster-workspace" / "logs" / "prune-worktrees.log"


def log(msg: str) -> None:
    print(msg)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a") as f:
        ts = datetime.now(timezone.utc).isoformat()
        f.write(f"{ts} {msg}\n")


def run(cmd: list[str], cwd: Path | None = None, timeout: int = 30) -> tuple[int, str, str]:
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd
        )
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except subprocess.TimeoutExpired:
        log(f"  [TIMEOUT] command timed out after {timeout}s: {cmd[0]}")
        return 1, "", "timeout"


def is_worktree(path: Path) -> bool:
    """Return True if .git is a file (pointing to main worktree), not a directory."""
    git_path = path / ".git"
    return git_path.is_file()


def get_branch(path: Path) -> str | None:
    rc, branch, _ = run(["git", "branch", "--show-current"], cwd=path)
    return branch if rc == 0 and branch else None


def get_remote_url(path: Path) -> str | None:
    rc, url, _ = run(["git", "remote", "get-url", "origin"], cwd=path)
    return url if rc == 0 and url else None


def remote_url_to_repo(url: str) -> str | None:
    """Extract 'owner/repo' from an HTTPS or SSH GitHub URL."""
    # HTTPS: https://github.com/owner/repo.git
    m = re.search(r"github\.com[:/]([^/]+)/([^/.]+?)(?:\.git)?$", url)
    if m:
        return f"{m.group(1)}/{m.group(2)}"
    return None


def get_pr_state(repo: str, branch: str) -> str | None:
    """Return 'MERGED', 'CLOSED', 'OPEN', or None if no PR found."""
    rc, out, _ = run(
        ["gh", "pr", "list", "--repo", repo, "--head", branch,
         "--state", "all", "--json", "state", "--limit", "1"]
    )
    if rc != 0:
        return None
    try:
        data = json.loads(out)
        if data:
            return data[0].get("state")  # 'MERGED', 'CLOSED', or 'OPEN'
    except (json.JSONDecodeError, IndexError, KeyError):
        pass
    return None


def dir_age_days(path: Path) -> float:
    """Return the age of the directory in days based on mtime."""
    stat = path.stat()
    age = datetime.now(timezone.utc) - datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
    return age.total_seconds() / 86400


def remove_worktree(main_repo: Path, worktree_path: Path, dry_run: bool) -> bool:
    """Remove a git worktree and its directory."""
    if dry_run:
        log(f"  [DRY-RUN] Would remove: {worktree_path}")
        return True
    # Attempt graceful removal via git first
    rc, _, err = run(["git", "worktree", "remove", "--force", str(worktree_path)], cwd=main_repo)
    if rc == 0:
        # Success — prune stale admin entries and done
        run(["git", "worktree", "prune"], cwd=main_repo)
        log(f"  [REMOVED] {worktree_path}")
        return True

    # git worktree remove failed. Only fall back to shutil.rmtree if the
    # directory still exists (meaning git failed before touching it, e.g.
    # because the worktree isn't registered). If git already partially removed
    # it (unlikely with --force), we skip to avoid double-removal.
    log(f"  [WARN] git worktree remove failed ({err or 'unknown'}) — trying direct removal")
    if worktree_path.exists():
        import shutil
        try:
            shutil.rmtree(str(worktree_path))
        except OSError as exc:
            log(f"  [ERROR] shutil.rmtree failed: {exc}")
            return False
    # Prune any stale entries regardless
    run(["git", "worktree", "prune"], cwd=main_repo)
    log(f"  [REMOVED] {worktree_path}")
    return True


def find_main_repo(path: Path) -> Path | None:
    """Given a worktree path, find the main repo from the .git file."""
    git_file = path / ".git"
    if not git_file.is_file():
        return None
    content = git_file.read_text().strip()
    # "gitdir: /path/to/main/.git/worktrees/name"
    m = re.match(r"gitdir:\s*(.+)", content)
    if not m:
        return None
    gitdir = Path(m.group(1))
    # Go up to find the main .git dir
    # /path/to/main/.git/worktrees/name → /path/to/main
    main_git = gitdir.parent.parent  # up from worktrees/name to .git
    if main_git.name == ".git":
        return main_git.parent
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description="Prune stale PR worktrees")
    parser.add_argument("--dry-run", action="store_true", help="Print what would be removed without deleting")
    parser.add_argument("--age-days", type=float, default=DEFAULT_AGE_DAYS, help="Minimum age in days (default: 7)")
    parser.add_argument("--projects-dir", type=Path, default=DEFAULT_PROJECTS_DIR, help="Directory to scan")
    args = parser.parse_args()

    projects_dir = args.projects_dir
    if not projects_dir.is_dir():
        print(f"ERROR: {projects_dir} does not exist", file=sys.stderr)
        sys.exit(1)

    mode = "DRY-RUN" if args.dry_run else "LIVE"
    log(f"=== prune-pr-worktrees [{mode}] age≥{args.age_days}d dir={projects_dir} ===")

    candidates = sorted(projects_dir.iterdir())
    total = 0
    prunable = 0
    pruned = 0
    skipped_young = 0
    skipped_open = 0
    skipped_no_pr = 0
    skipped_no_worktree = 0
    errors = 0

    for entry in candidates:
        if not entry.is_dir():
            continue
        total += 1

        if not is_worktree(entry):
            skipped_no_worktree += 1
            continue

        branch = get_branch(entry)
        if not branch:
            log(f"  [SKIP] {entry.name}: no branch (detached HEAD?)")
            skipped_no_worktree += 1
            continue

        remote_url = get_remote_url(entry)
        if not remote_url:
            log(f"  [SKIP] {entry.name}: no remote origin")
            skipped_no_worktree += 1
            continue

        repo = remote_url_to_repo(remote_url)
        if not repo:
            log(f"  [SKIP] {entry.name}: can't parse repo from {remote_url}")
            skipped_no_worktree += 1
            continue

        age = dir_age_days(entry)
        if age < args.age_days:
            skipped_young += 1
            continue

        state = get_pr_state(repo, branch)
        if state is None:
            skipped_no_pr += 1
            continue

        if state == "OPEN":
            skipped_open += 1
            continue

        # MERGED or CLOSED and old enough
        prunable += 1
        log(f"  [PRUNE] {entry.name} | branch={branch} | pr={state} | age={age:.1f}d")
        main_repo = find_main_repo(entry)
        if not main_repo:
            log(f"  [ERROR] can't find main repo for {entry.name}")
            errors += 1
            continue
        ok = remove_worktree(main_repo, entry, args.dry_run)
        if ok:
            pruned += 1
        else:
            errors += 1

    log(f"=== Summary: {total} dirs | {prunable} prunable | {pruned} pruned | "
        f"{skipped_open} open PRs | {skipped_no_pr} no PR | "
        f"{skipped_young} too young | {skipped_no_worktree} not worktrees | {errors} errors ===")


if __name__ == "__main__":
    main()
