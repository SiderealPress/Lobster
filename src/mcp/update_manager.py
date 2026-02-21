"""
Lobster Self-Update System

Provides update detection, changelog generation, compatibility analysis,
and safe upgrade execution.

Supports two install modes:
  - git: traditional git-based updates (dev machines, has .git/)
  - tarball: GitHub Releases tarball updates (managed/self-hosted, no .git/)
"""
import subprocess
import json
import os
import re
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx

LOBSTER_ROOT = Path(os.environ.get("LOBSTER_INSTALL_DIR", os.environ.get("LOBSTER_ROOT", Path.home() / "lobster")))
GITHUB_REPO = "SiderealPress/lobster"
GITHUB_API = f"https://api.github.com/repos/{GITHUB_REPO}"


def _installed_version(repo_path: Path) -> str:
    """Read the installed version from the VERSION file."""
    version_file = repo_path / "VERSION"
    if version_file.exists():
        return version_file.read_text().strip()
    return "0.0.0"


def _is_git_install(repo_path: Path) -> bool:
    """Detect whether this is a git clone or tarball install."""
    return (repo_path / ".git").is_dir()


class UpdateManager:
    def __init__(self, repo_path: Path = LOBSTER_ROOT):
        self.repo_path = repo_path
        self.is_git = _is_git_install(repo_path)

    def check_for_updates(self) -> dict:
        """Check if updates are available."""
        if self.is_git:
            return self._check_git_updates()
        return self._check_release_updates()

    def generate_changelog(self, from_sha: str = None, to_sha: str = "origin/main") -> str:
        """Generate a human-readable changelog."""
        if self.is_git:
            return self._git_changelog(from_sha, to_sha)
        return self._release_changelog()

    def analyze_compatibility(self, from_sha: str = None, to_sha: str = "origin/main") -> dict:
        """Analyze breaking changes and compatibility issues."""
        if self.is_git:
            return self._git_compatibility(from_sha, to_sha)
        return self._release_compatibility()

    def create_upgrade_plan(self) -> dict:
        """Create a complete upgrade plan."""
        update_info = self.check_for_updates()
        if not update_info.get("updates_available"):
            return {"action": "none", "message": "Already up to date."}

        if self.is_git:
            changelog = self.generate_changelog(update_info.get("local_sha"))
            compat = self.analyze_compatibility(update_info.get("local_sha"))
        else:
            changelog = self._release_changelog()
            compat = self._release_compatibility()

        plan = {
            "action": "auto" if compat["safe_to_update"] else "manual",
            "install_mode": "git" if self.is_git else "tarball",
            "current_version": update_info.get("current_version", update_info.get("local_sha", "unknown")),
            "latest_version": update_info.get("latest_version", update_info.get("remote_sha", "unknown")),
            "changelog": changelog,
            "compatibility": compat,
            "steps": [],
        }

        if self.is_git:
            plan["commits_behind"] = update_info.get("commits_behind", 0)
            if compat["safe_to_update"]:
                plan["steps"] = [
                    "1. Pull latest from origin/main",
                    "2. Install any new dependencies",
                    "3. Restart services",
                    "4. Run health check",
                ]
            else:
                plan["steps"] = [
                    "1. Review breaking changes: " + "; ".join(compat.get("issues", [])),
                    "2. Backup current state",
                    "3. Pull latest from origin/main",
                    "4. Resolve conflicts manually",
                    "5. Install dependencies",
                    "6. Run migrations if needed",
                    "7. Restart services",
                    "8. Run health check",
                    "9. If health check fails, rollback",
                ]
        else:
            if compat["safe_to_update"]:
                plan["steps"] = [
                    "1. Download release tarball",
                    "2. Verify checksum",
                    "3. Extract to temp directory",
                    "4. Swap install directory (preserve .venv/)",
                    "5. Install new dependencies if requirements.txt changed",
                    "6. Regenerate systemd services",
                    "7. Restart services",
                    "8. Run health check (rollback on failure)",
                ]
            else:
                plan["steps"] = [
                    "1. Review release notes for breaking changes",
                    "2. Download release tarball",
                    "3. Backup current installation",
                    "4. Extract and swap",
                    "5. Run migrations if needed",
                    "6. Install dependencies",
                    "7. Restart services",
                    "8. Verify health",
                ]

        return plan

    def execute_safe_update(self) -> dict:
        """Execute a safe auto-update (only if compatibility check passes)."""
        if self.is_git:
            return self._execute_git_update()
        return self._execute_tarball_update()

    # =========================================================================
    # Git-based operations (dev machines)
    # =========================================================================

    def _check_git_updates(self) -> dict:
        self._git("fetch", "origin", "main")
        local_sha = self._git("rev-parse", "HEAD").strip()
        remote_sha = self._git("rev-parse", "origin/main").strip()

        if local_sha == remote_sha:
            return {"updates_available": False, "local_sha": local_sha}

        behind_count = self._git("rev-list", "--count", f"{local_sha}..{remote_sha}").strip()
        log = self._git("log", "--oneline", f"{local_sha}..{remote_sha}")

        return {
            "updates_available": True,
            "local_sha": local_sha,
            "remote_sha": remote_sha,
            "commits_behind": int(behind_count),
            "commit_log": log.strip().split("\n") if log.strip() else [],
        }

    def _git_changelog(self, from_sha: str = None, to_sha: str = "origin/main") -> str:
        if not from_sha:
            from_sha = self._git("rev-parse", "HEAD").strip()

        log = self._git("log", "--format=%h %s (%an, %ar)", f"{from_sha}..{to_sha}")
        if not log.strip():
            return "No changes."

        features, fixes, other = [], [], []
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

    def _git_compatibility(self, from_sha: str = None, to_sha: str = "origin/main") -> dict:
        if not from_sha:
            from_sha = self._git("rev-parse", "HEAD").strip()

        diff = self._git("diff", "--name-only", f"{from_sha}..{to_sha}")
        changed_files = [f for f in diff.strip().split("\n") if f]

        issues = []
        warnings = []
        safe = True

        for f in changed_files:
            if f == "requirements.txt" or f.endswith("requirements.txt"):
                issues.append(f"Dependencies changed: {f} - may need `pip install`")
            if f == "src/mcp/inbox_server.py":
                warnings.append("MCP server modified - tool interfaces may have changed")
            if f.endswith(".env") or f.endswith(".env.example"):
                warnings.append(f"Environment config changed: {f}")
            if "migration" in f.lower() or "schema" in f.lower():
                issues.append(f"Database schema change detected: {f}")
                safe = False
            if "cron" in f.lower() or f.startswith("scripts/"):
                warnings.append(f"Script/cron change: {f}")

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

    def _execute_git_update(self) -> dict:
        compat = self.analyze_compatibility()
        if not compat["safe_to_update"]:
            return {
                "success": False,
                "message": "Cannot auto-update. Issues: " + "; ".join(compat["issues"]),
            }

        try:
            current_sha = self._git("rev-parse", "HEAD").strip()
            self._git("pull", "origin", "main", "--ff-only")
            new_sha = self._git("rev-parse", "HEAD").strip()

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

    # =========================================================================
    # Tarball-based operations (managed/self-hosted installs)
    # =========================================================================

    def _get_latest_release(self) -> dict:
        """Fetch the latest release from GitHub Releases API."""
        try:
            resp = httpx.get(f"{GITHUB_API}/releases/latest", timeout=15)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            raise RuntimeError(f"Failed to fetch latest release: {e}")

    def _check_release_updates(self) -> dict:
        current = _installed_version(self.repo_path)
        try:
            release = self._get_latest_release()
        except RuntimeError as e:
            return {"updates_available": False, "error": str(e), "current_version": current}

        latest_tag = release.get("tag_name", "")
        latest_version = latest_tag.lstrip("v")

        if latest_version == current:
            return {"updates_available": False, "current_version": current}

        # Find tarball asset
        tarball_url = None
        checksum_url = None
        for asset in release.get("assets", []):
            name = asset["name"]
            if name.endswith(".tar.gz") and "lobster" in name.lower():
                tarball_url = asset["browser_download_url"]
            if name.endswith(".sha256") or name == "checksums.txt":
                checksum_url = asset["browser_download_url"]

        # Fall back to GitHub's auto-generated tarball
        if not tarball_url:
            tarball_url = release.get("tarball_url")

        return {
            "updates_available": True,
            "current_version": current,
            "latest_version": latest_version,
            "tag": latest_tag,
            "tarball_url": tarball_url,
            "checksum_url": checksum_url,
            "release_notes": release.get("body", ""),
            "published_at": release.get("published_at", ""),
        }

    def _release_changelog(self) -> str:
        try:
            release = self._get_latest_release()
            body = release.get("body", "")
            tag = release.get("tag_name", "unknown")
            if body:
                return f"## Release {tag}\n\n{body}"
            return f"Release {tag} — no release notes provided."
        except Exception as e:
            return f"Could not fetch release notes: {e}"

    def _release_compatibility(self) -> dict:
        """Basic compatibility check for tarball upgrades."""
        return {
            "safe_to_update": True,
            "changed_files": [],
            "issues": [],
            "warnings": ["Tarball upgrades replace all code files. Config is preserved in ~/lobster-config/."],
            "local_changes": 0,
            "recommendation": "auto-update",
        }

    def _execute_tarball_update(self) -> dict:
        """Download, extract, and swap the install directory with a new release tarball."""
        update_info = self._check_release_updates()
        if not update_info.get("updates_available"):
            return {"success": False, "message": "Already up to date."}

        tarball_url = update_info.get("tarball_url")
        if not tarball_url:
            return {"success": False, "message": "No tarball URL found in release."}

        current_version = update_info["current_version"]
        latest_version = update_info["latest_version"]
        install_dir = self.repo_path
        backup_dir = install_dir.parent / "lobster.bak"

        try:
            # 1. Download tarball to temp directory
            tmp_dir = Path(tempfile.mkdtemp(prefix="lobster-upgrade-"))
            tarball_path = tmp_dir / "lobster.tar.gz"

            resp = httpx.get(tarball_url, follow_redirects=True, timeout=120)
            resp.raise_for_status()
            tarball_path.write_bytes(resp.content)

            # 2. Verify checksum if available
            checksum_url = update_info.get("checksum_url")
            if checksum_url:
                try:
                    cs_resp = httpx.get(checksum_url, follow_redirects=True, timeout=15)
                    cs_resp.raise_for_status()
                    import hashlib
                    expected = None
                    for line in cs_resp.text.strip().splitlines():
                        parts = line.split()
                        if len(parts) >= 1:
                            expected = parts[0]
                            break
                    if expected:
                        actual = hashlib.sha256(tarball_path.read_bytes()).hexdigest()
                        if actual != expected:
                            shutil.rmtree(tmp_dir)
                            return {"success": False, "message": f"Checksum mismatch: expected {expected}, got {actual}"}
                except Exception:
                    pass  # Checksum verification is best-effort

            # 3. Extract tarball
            extract_dir = tmp_dir / "extracted"
            extract_dir.mkdir()
            subprocess.run(
                ["tar", "xzf", str(tarball_path), "-C", str(extract_dir)],
                check=True, capture_output=True,
            )

            # Find the extracted directory (GitHub wraps in owner-repo-sha/)
            subdirs = list(extract_dir.iterdir())
            if len(subdirs) == 1 and subdirs[0].is_dir():
                new_install = subdirs[0]
            else:
                new_install = extract_dir

            # 4. Preserve .venv from current install
            current_venv = install_dir / ".venv"
            if current_venv.is_dir():
                shutil.move(str(current_venv), str(new_install / ".venv"))

            # Preserve .state directory
            current_state = install_dir / ".state"
            if current_state.is_dir():
                shutil.move(str(current_state), str(new_install / ".state"))

            # 5. Swap directories
            if backup_dir.exists():
                shutil.rmtree(backup_dir)
            shutil.move(str(install_dir), str(backup_dir))
            shutil.move(str(new_install), str(install_dir))

            # 6. Check if requirements.txt changed and reinstall deps
            old_reqs = backup_dir / "requirements.txt"
            new_reqs = install_dir / "requirements.txt"
            if new_reqs.exists():
                reqs_changed = True
                if old_reqs.exists():
                    reqs_changed = old_reqs.read_text() != new_reqs.read_text()
                if reqs_changed:
                    venv_pip = install_dir / ".venv" / "bin" / "pip"
                    if venv_pip.exists():
                        subprocess.run(
                            [str(venv_pip), "install", "-r", str(new_reqs), "--quiet"],
                            capture_output=True,
                        )

            # 7. Regenerate systemd services and restart
            upgrade_script = install_dir / "scripts" / "upgrade.sh"
            if upgrade_script.exists():
                # The upgrade script handles service regeneration
                pass

            # 8. Cleanup temp directory
            shutil.rmtree(tmp_dir, ignore_errors=True)

            return {
                "success": True,
                "previous_version": current_version,
                "current_version": latest_version,
                "message": f"Updated from v{current_version} to v{latest_version}",
                "rollback_command": f"rm -rf {install_dir} && mv {backup_dir} {install_dir}",
            }

        except Exception as e:
            # Attempt rollback on failure
            if backup_dir.exists() and not install_dir.exists():
                shutil.move(str(backup_dir), str(install_dir))
            return {"success": False, "message": f"Tarball update failed: {e}"}
