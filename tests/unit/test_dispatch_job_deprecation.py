"""
Tests for issue #1083 Phase 1 — dispatch-job.sh tombstone and upgrade migration.

Verifies:
1. dispatch-job.sh contains the deprecation notice (issue #1083)
2. upgrade.sh contains Migration 71 to remove LOBSTER-SCHEDULED crontab entries
3. Migration 71 only removes LOBSTER-SCHEDULED entries that have a corresponding
   systemd timer — orphaned entries (no timer) are left in place with a warning
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


class TestUpgradeMigration71:
    """upgrade.sh must contain Migration 71 to clean LOBSTER-SCHEDULED crontab entries."""

    def test_migration_71_present_in_upgrade_sh(self):
        content = UPGRADE_SH.read_text()
        assert "Migration 71" in content, (
            "upgrade.sh must contain Migration 71 (LOBSTER-SCHEDULED crontab cleanup)"
        )

    def test_migration_71_targets_lobster_scheduled_marker(self):
        content = UPGRADE_SH.read_text()
        assert "LOBSTER-SCHEDULED" in content, (
            "Migration 71 must reference the LOBSTER-SCHEDULED crontab marker"
        )

    def test_migration_71_grep_only_targets_lobster_scheduled(self):
        """Migration 71 must only touch entries marked with LOBSTER-SCHEDULED,
        not broader patterns that would remove system cron entries."""
        content = UPGRADE_SH.read_text()
        m70_start = content.find("Migration 71")
        assert m70_start != -1

        m70_end = content.find("if [ \"$migrated\" -eq 0 ]", m70_start)
        m70_block = content[m70_start:m70_end]

        # The block must reference the LOBSTER-SCHEDULED marker
        assert "LOBSTER-SCHEDULED" in m70_block, (
            "Migration 71 must reference the LOBSTER-SCHEDULED crontab marker to avoid "
            "removing LOBSTER-HEALTH, LOBSTER-SELF-CHECK, and other system entries"
        )

    def test_migration_71_checks_for_systemd_timer_before_removing(self):
        """Migration 71 must only remove a LOBSTER-SCHEDULED cron entry if a
        corresponding lobster-managed systemd timer exists — preventing silent job loss."""
        content = UPGRADE_SH.read_text()
        m70_start = content.find("Migration 71")
        assert m70_start != -1

        m70_end = content.find("if [ \"$migrated\" -eq 0 ]", m70_start)
        m70_block = content[m70_start:m70_end]

        # Must check for the systemd timer file before removing a cron entry
        assert "/etc/systemd/system/lobster-" in m70_block, (
            "Migration 71 must check for a lobster-managed systemd timer file "
            "(/etc/systemd/system/lobster-<name>.timer) before removing each cron entry"
        )
        # Must reference the LOBSTER-MANAGED marker check
        assert "LOBSTER-MANAGED" in m70_block, (
            "Migration 71 must verify the systemd timer carries the LOBSTER-MANAGED marker"
        )

    def test_migration_71_warns_when_no_systemd_timer_exists(self):
        """When a LOBSTER-SCHEDULED cron entry has no systemd timer,
        Migration 71 must warn the operator rather than silently removing it."""
        content = UPGRADE_SH.read_text()
        m70_start = content.find("Migration 71")
        assert m70_start != -1

        m70_end = content.find("if [ \"$migrated\" -eq 0 ]", m70_start)
        m70_block = content[m70_start:m70_end]

        assert "WARNING" in m70_block, (
            "Migration 71 must print a WARNING when a LOBSTER-SCHEDULED cron entry "
            "has no corresponding systemd timer, so the operator knows to fix it"
        )
        assert "create_scheduled_job" in m70_block, (
            "Migration 71 must tell the operator to use create_scheduled_job MCP tool "
            "to create the missing systemd timer"
        )

    def test_migration_71_references_issue_1083(self):
        content = UPGRADE_SH.read_text()
        m70_start = content.find("Migration 71")
        assert m70_start != -1
        m70_end = content.find("if [ \"$migrated\" -eq 0 ]", m70_start)
        m70_block = content[m70_start:m70_end]
        assert "1083" in m70_block, "Migration 71 must reference issue #1083"

    def test_migration_71_increments_migrated_counter(self):
        """The migration must increment the migrated counter so the success message is accurate."""
        content = UPGRADE_SH.read_text()
        m70_start = content.find("Migration 71")
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
