"""
Lobster Self-Update System

Provides update detection, changelog generation, compatibility analysis,
and safe upgrade execution.
"""
import subprocess
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

LOBSTER_ROOT = Path(os.environ.get("LOBSTER_ROOT", Path.home() / "lobster"))


class UpdateManager:
    def __init__(self, repo_path: Path = LOBSTER_ROOT):
        self.repo_path = repo_path

    def check_for_updates(self) -> dict:
        """Check if updates are available on remote."""
        # git fetch
        self._git("fetch", "origin", "main")

        local_sha = self._git("rev-parse", "HEAD").strip()
        remote_sha = self._git("rev-parse", "origin/main").strip()

        if local_sha == remote_sha:
            return {"updates_available": False, "local_sha": local_sha}

        # Count commits behind
        behind_count = self._git("rev-list", "--count", f"{local_sha}..{remote_sha}").strip()

        # Get commit log
        log = self._git("log", "--oneline", f"{local_sha}..{remote_sha}")

        return {
            "updates_available": True,
            "local_sha": local_sha,
            "remote_sha": remote_sha,
            "commits_behind": int(behind_count),
            "commit_log": log.strip().split("\n") if log.strip() else [],
        }

    def generate_changelog(self, from_sha: str = None, to_sha: str = "origin/main") -> str:
        """Generate a human-readable changelog."""
        if not from_sha:
            from_sha = self._git("rev-parse", "HEAD").strip()

        # Get detailed log
        log = self._git("log", "--format=%h %s (%an, %ar)", f"{from_sha}..{to_sha}")

        if not log.strip():
            return "No changes."

        # Categorize commits
        features = []
        fixes = []
        other = []

        for line in log.strip().split("\n"):
            lower = line.lower()
            if "feat" in lower:
                features.append(line)
            elif "fix" in lower or "bug" in lower:
                fixes.append(line)
            else:
                other.append(line)

        changelog = "## Changelog\n\n"
        if features:
            changelog += "### New Features\n" + "\n".join(f"- {f}" for f in features) + "\n\n"
        if fixes:
            changelog += "### Bug Fixes\n" + "\n".join(f"- {f}" for f in fixes) + "\n\n"
        if other:
            changelog += "### Other Changes\n" + "\n".join(f"- {o}" for o in other) + "\n\n"

        return changelog

    def analyze_compatibility(self, from_sha: str = None, to_sha: str = "origin/main") -> dict:
        """Analyze breaking changes and compatibility issues."""
        if not from_sha:
            from_sha = self._git("rev-parse", "HEAD").strip()

        diff = self._git("diff", "--name-only", f"{from_sha}..{to_sha}")
        changed_files = [f for f in diff.strip().split("\n") if f]

        issues = []
        warnings = []
        safe = True

        # Check for breaking changes
        for f in changed_files:
            # New dependencies
            if f == "requirements.txt" or f.endswith("requirements.txt"):
                issues.append(f"Dependencies changed: {f} - may need `pip install`")

            # MCP interface changes
            if f == "src/mcp/inbox_server.py":
                # Check if tool signatures changed
                warnings.append("MCP server modified - tool interfaces may have changed")

            # Config changes
            if f.endswith(".env") or f.endswith(".env.example"):
                warnings.append(f"Environment config changed: {f}")

            # Database schema
            if "migration" in f.lower() or "schema" in f.lower():
                issues.append(f"Database schema change detected: {f}")
                safe = False

            # Cron/scheduled tasks
            if "cron" in f.lower() or f.startswith("scripts/"):
                warnings.append(f"Script/cron change: {f}")

        # Check for local uncommitted changes
        status = self._git("status", "--porcelain")
        local_changes = [line for line in status.strip().split("\n") if line.strip()]

        if local_changes:
            conflicting = [
                line for line in local_changes
                if any(line.strip().endswith(f) for f in changed_files)
            ]
            if conflicting:
                issues.append(f"Local changes conflict with update: {conflicting}")
                safe = False
            else:
                warnings.append(f"{len(local_changes)} local uncommitted changes (non-conflicting)")

        return {
            "safe_to_update": safe and len(issues) == 0,
            "changed_files": changed_files,
            "issues": issues,
            "warnings": warnings,
            "local_changes": len(local_changes),
            "recommendation": "auto-update" if (safe and not issues) else "manual review needed",
        }

    def create_upgrade_plan(self) -> dict:
        """Create a complete upgrade plan."""
        update_info = self.check_for_updates()
        if not update_info["updates_available"]:
            return {"action": "none", "message": "Already up to date."}

        changelog = self.generate_changelog(update_info["local_sha"])
        compat = self.analyze_compatibility(update_info["local_sha"])

        plan = {
            "action": "auto" if compat["safe_to_update"] else "manual",
            "commits_behind": update_info["commits_behind"],
            "changelog": changelog,
            "compatibility": compat,
            "steps": [],
        }

        if compat["safe_to_update"]:
            plan["steps"] = [
                "1. Pull latest from origin/main",
                "2. Install any new dependencies",
                "3. Restart services",
                "4. Run health check",
            ]
        else:
            plan["steps"] = [
                "1. Review breaking changes: " + "; ".join(compat["issues"]),
                "2. Backup current state",
                "3. Pull latest from origin/main",
                "4. Resolve conflicts manually",
                "5. Install dependencies",
                "6. Run migrations if needed",
                "7. Restart services",
                "8. Run health check",
                "9. If health check fails, rollback",
            ]

        return plan

    def execute_safe_update(self) -> dict:
        """Execute a safe auto-update (only if compatibility check passes)."""
        compat = self.analyze_compatibility()

        if not compat["safe_to_update"]:
            return {
                "success": False,
                "message": "Cannot auto-update. Issues: " + "; ".join(compat["issues"]),
            }

        try:
            # Snapshot current state
            current_sha = self._git("rev-parse", "HEAD").strip()

            # Pull
            self._git("pull", "origin", "main", "--ff-only")
            new_sha = self._git("rev-parse", "HEAD").strip()

            # Check for new pip requirements
            if os.path.exists(self.repo_path / "requirements.txt"):
                subprocess.run(
                    ["pip", "install", "-r", "requirements.txt", "--quiet"],
                    cwd=self.repo_path,
                    capture_output=True,
                )

            return {
                "success": True,
                "previous_sha": current_sha,
                "current_sha": new_sha,
                "message": f"Updated from {current_sha[:7]} to {new_sha[:7]}",
                "rollback_command": f"git reset --hard {current_sha}",
            }
        except Exception as e:
            return {"success": False, "message": str(e)}

    def _git(self, *args) -> str:
        result = subprocess.run(
            ["git"] + list(args),
            cwd=self.repo_path,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0 and "fetch" not in args:
            raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr}")
        return result.stdout
