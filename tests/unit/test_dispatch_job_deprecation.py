"""
Tests for issue #1083 Phase 1 — dispatch-job.sh tombstone and upgrade migration.

Verifies:
1. dispatch-job.sh contains the deprecation notice (issue #1083)
2. upgrade.sh contains Migration 70 to remove LOBSTER-SCHEDULED crontab entries
3. Migration 70 only removes LOBSTER-SCHEDULED entries, not other cron entries
4. dispatch-job.sh still functions correctly for backward compatibility
   (existing jobs that haven't migrated yet must not break)
"""

import re
import subprocess
import os
from pathlib import Path

import pytest

REPO_DIR = Path(__file__).parent.parent.parent
DISPATCH_JOB = REPO_DIR / "scheduled-tasks" / "dispatch-job.sh"
UPGRADE_SH = REPO_DIR / "scripts" / "upgrade.sh"


class TestDispatchJobDeprecationNotice:
    """dispatch-job.sh must carry a tombstone comment (issue #1083 Phase 1)."""

    def test_deprecation_notice_present_in_dispatch_job_sh(self):
        content = DISPATCH_JOB.read_text()
        assert "DEPRECATED" in content, (
            "dispatch-job.sh must contain a DEPRECATED marker (issue #1083 Phase 1)"
        )

    def test_deprecation_notice_references_issue_1083(self):
        content = DISPATCH_JOB.read_text()
        assert "1083" in content, (
            "Deprecation notice must reference issue #1083"
        )

    def test_deprecation_mentions_systemd_replacement(self):
        content = DISPATCH_JOB.read_text()
        assert "systemd" in content.lower(), (
            "Deprecation notice must mention systemd as the replacement"
        )

    def test_dispatch_job_sh_is_still_executable(self):
        assert DISPATCH_JOB.exists(), "dispatch-job.sh must still exist on disk (not deleted in Phase 1)"
        assert os.access(DISPATCH_JOB, os.X_OK), "dispatch-job.sh must still be executable"


class TestUpgradeMigration70:
    """upgrade.sh must contain Migration 70 to clean LOBSTER-SCHEDULED crontab entries."""

    def test_migration_70_present_in_upgrade_sh(self):
        content = UPGRADE_SH.read_text()
        assert "Migration 70" in content, (
            "upgrade.sh must contain Migration 70 (LOBSTER-SCHEDULED crontab cleanup)"
        )

    def test_migration_70_targets_lobster_scheduled_marker(self):
        content = UPGRADE_SH.read_text()
        assert "LOBSTER-SCHEDULED" in content, (
            "Migration 70 must reference the LOBSTER-SCHEDULED crontab marker"
        )

    def test_migration_70_grep_only_targets_lobster_scheduled(self):
        """The grep -v command in Migration 70 must filter on 'LOBSTER-SCHEDULED' only,
        not a broader pattern that would also remove system cron entries."""
        content = UPGRADE_SH.read_text()
        m70_start = content.find("Migration 70")
        assert m70_start != -1

        m70_end = content.find("if [ \"$migrated\" -eq 0 ]", m70_start)
        m70_block = content[m70_start:m70_end]

        # The actual grep -v command must use the specific marker
        assert "grep -v '# LOBSTER-SCHEDULED'" in m70_block or \
               'grep -v "# LOBSTER-SCHEDULED"' in m70_block, (
            "Migration 70 must use grep -v '# LOBSTER-SCHEDULED' to avoid "
            "removing LOBSTER-HEALTH, LOBSTER-SELF-CHECK, and other system entries"
        )

    def test_migration_70_references_issue_1083(self):
        content = UPGRADE_SH.read_text()
        m70_start = content.find("Migration 70")
        assert m70_start != -1
        m70_end = content.find("if [ \"$migrated\" -eq 0 ]", m70_start)
        m70_block = content[m70_start:m70_end]
        assert "1083" in m70_block, "Migration 70 must reference issue #1083"

    def test_migration_70_increments_migrated_counter(self):
        """The migration must increment the migrated counter so the success message is accurate."""
        content = UPGRADE_SH.read_text()
        m70_start = content.find("Migration 70")
        assert m70_start != -1
        m70_end = content.find("if [ \"$migrated\" -eq 0 ]", m70_start)
        m70_block = content[m70_start:m70_end]
        assert "migrated=$((migrated + 1))" in m70_block


class TestInstallShDispatchJobComment:
    """install.sh comments must no longer describe dispatch-job.sh as the primary scheduler."""

    def test_install_sh_dispatch_job_comment_updated(self):
        install_sh = REPO_DIR / "install.sh"
        content = install_sh.read_text()
        # The old comment said dispatch-job.sh is the primary scheduler.
        # The new comment must clarify it's for compatibility only.
        assert "compatibility" in content or "systemd" in content, (
            "install.sh comment about dispatch-job.sh must mention it is kept for "
            "compatibility or that systemd is the new approach"
        )

    def test_install_sh_admin_chat_id_comment_no_longer_says_dispatch_job(self):
        install_sh = REPO_DIR / "install.sh"
        # Count how many times "dispatch-job.sh" appears in the admin_chat_id comment block
        # There used to be two occurrences of "Used by dispatch-job.sh" in config templates.
        content = install_sh.read_text()
        # Should have at most 1 reference (the chmod line), not the old "Used by dispatch-job.sh" comment
        lines_with_dispatch = [
            l for l in content.splitlines()
            if "dispatch-job.sh" in l and "Used by" in l
        ]
        assert len(lines_with_dispatch) == 0, (
            "install.sh should no longer have 'Used by dispatch-job.sh' in any comment. "
            f"Found: {lines_with_dispatch}"
        )
