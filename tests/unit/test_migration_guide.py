"""
Tests for the scheduled job migration guide (issue #1084).

Verifies that:
1. HOW-TO-MIGRATE.md exists and covers all required sections
2. poller.py.template is syntactically valid Python
3. The template has no TODOs left in critical paths (only YOUR_* placeholders)
4. The guide does not describe dispatch-job.sh or jobs.json as the "right way"
"""

import ast
import sys
from pathlib import Path

import pytest

REPO_DIR = Path(__file__).parent.parent.parent
HOW_TO_MIGRATE = REPO_DIR / "scheduled-tasks" / "HOW-TO-MIGRATE.md"
POLLER_TEMPLATE = REPO_DIR / "scheduled-tasks" / "templates" / "poller.py.template"


class TestHowToMigrateExists:
    def test_file_exists(self):
        assert HOW_TO_MIGRATE.exists(), "HOW-TO-MIGRATE.md must exist in scheduled-tasks/"

    def test_file_is_not_empty(self):
        assert HOW_TO_MIGRATE.stat().st_size > 500, "HOW-TO-MIGRATE.md must not be empty"


class TestHowToMigrateSections:
    """The guide must cover all five sections from the issue spec."""

    @pytest.fixture(autouse=True)
    def content(self):
        self.text = HOW_TO_MIGRATE.read_text()

    def test_covers_three_job_kinds(self):
        """Section 1: The three job kinds (Kind 1/2/3) with descriptions."""
        assert "Kind 1" in self.text, "Guide must describe Kind 1 jobs"
        assert "Kind 2" in self.text, "Guide must describe Kind 2 jobs"

    def test_covers_when_to_migrate_decision_criteria(self):
        """Section 2: Decision criteria for migrating to Kind 1."""
        # Should have guidance on when to migrate
        assert "When to" in self.text or "when to" in self.text, (
            "Guide must explain when to migrate a job"
        )
        assert "deterministic" in self.text.lower(), (
            "Guide must mention determinism as a migration criterion"
        )

    def test_covers_step_by_step_conversion(self):
        """Section 3: Step-by-step conversion guide."""
        assert "Step" in self.text or "step" in self.text
        assert "state" in self.text.lower(), "Guide must cover state file handling"
        assert "watermark" in self.text.lower() or "last_check_ts" in self.text, (
            "Guide must mention the watermark/last_check_ts pattern"
        )

    def test_covers_systemd_timer_creation(self):
        """Section 3: Creating the systemd timer via create_scheduled_job MCP tool."""
        assert "create_scheduled_job" in self.text, (
            "Guide must mention the create_scheduled_job MCP tool"
        )
        assert "systemd" in self.text.lower(), "Guide must cover systemd timer creation"

    def test_covers_tombstoning(self):
        """Section 3: Tombstoning the jobs.json entry."""
        assert "tombstone" in self.text.lower() or "jobs.json" in self.text, (
            "Guide must cover tombstoning jobs.json entries"
        )

    def test_covers_testing_checklist(self):
        """Section 3: Testing checklist."""
        assert "checklist" in self.text.lower() or "- [ ]" in self.text, (
            "Guide must include a testing checklist"
        )

    def test_covers_common_mistakes(self):
        """Section 5: Common mistakes."""
        assert "Common" in self.text and ("mistake" in self.text.lower() or "Mistake" in self.text), (
            "Guide must have a Common Mistakes section"
        )
        assert "watermark" in self.text.lower() or "before writing" in self.text.lower(), (
            "Guide must warn about advancing watermark before writing to inbox"
        )

    def test_no_dispatch_job_presented_as_right_way(self):
        """Guide must not present dispatch-job.sh as the recommended new-job approach."""
        lines_with_dispatch = [
            ln for ln in self.text.splitlines()
            if "dispatch-job.sh" in ln and "new job" in ln.lower()
        ]
        assert len(lines_with_dispatch) == 0, (
            "Guide must not describe dispatch-job.sh as how to create new jobs. "
            f"Found: {lines_with_dispatch}"
        )

    def test_no_lobster_scheduled_crontab_as_right_way(self):
        """Guide must not instruct the reader to create LOBSTER-SCHEDULED crontab entries."""
        assert "# LOBSTER-SCHEDULED" not in self.text or "deprecated" in self.text.lower() or "Do not" in self.text, (
            "If guide mentions LOBSTER-SCHEDULED, it must also note that it is deprecated"
        )


class TestPollerPythonTemplate:
    """poller.py.template must be syntactically valid and complete."""

    @pytest.fixture(autouse=True)
    def content(self):
        self.raw = POLLER_TEMPLATE.read_text()

    def test_template_file_exists(self):
        assert POLLER_TEMPLATE.exists(), "poller.py.template must exist"

    def test_template_is_valid_python(self):
        """Template must parse as valid Python (minus the shebang line)."""
        # Strip the shebang for the AST parser
        lines = self.raw.splitlines()
        code = "\n".join(lines[1:]) if lines[0].startswith("#!") else self.raw
        try:
            ast.parse(code)
        except SyntaxError as e:
            pytest.fail(f"poller.py.template has syntax error: {e}")

    def test_template_has_job_name_constant(self):
        assert "JOB_NAME" in self.raw, "Template must define JOB_NAME constant"

    def test_template_has_state_file_constant(self):
        assert "STATE_FILE" in self.raw, "Template must define STATE_FILE constant"

    def test_template_has_inbox_dir_constant(self):
        assert "INBOX_DIR" in self.raw, "Template must define INBOX_DIR constant"

    def test_template_has_load_state_function(self):
        assert "_load_state" in self.raw, "Template must include _load_state() function"

    def test_template_has_write_state_function(self):
        assert "_write_state" in self.raw, "Template must include _write_state() function"

    def test_template_has_poll_function(self):
        assert "_poll" in self.raw, "Template must include a polling function"

    def test_template_has_write_inbox_function(self):
        assert "_write_inbox" in self.raw, "Template must include inbox write function"

    def test_template_has_main_function(self):
        assert "def main" in self.raw, "Template must include main() function"

    def test_template_watermark_write_before_state_advance(self):
        """Template must write inbox message BEFORE advancing watermark."""
        write_pos = self.raw.find("_write_inbox_message")
        state_pos = self.raw.find("_write_state(new_state)")
        assert write_pos != -1, "Template must call _write_inbox_message"
        assert state_pos != -1, "Template must call _write_state to advance watermark"
        assert write_pos < state_pos, (
            "Template must call _write_inbox_message() BEFORE _write_state() "
            "(watermark must not advance until inbox write succeeds)"
        )

    def test_template_uses_atomic_write(self):
        """Template must use atomic tmp→replace pattern for writes."""
        assert "tmp.replace" in self.raw or ".replace(" in self.raw, (
            "Template must use atomic write: write to .tmp then os.replace/Path.replace"
        )

    def test_template_no_claude_invocation(self):
        """Template must not invoke Claude directly."""
        import re
        claude_calls = re.findall(r'\bclaude\s+(-p|--print)', self.raw)
        assert len(claude_calls) == 0, (
            "Template must not invoke `claude -p` or `claude --print` directly"
        )

    def test_template_exits_zero_on_error(self):
        """Template must exit 0 on errors to avoid failing the systemd unit."""
        assert "sys.exit(0)" in self.raw, (
            "Template must use sys.exit(0) in the error handler to prevent "
            "systemd from marking the unit as failed on transient errors"
        )

    def test_template_no_leftover_todos(self):
        """Template must have no TODO comments left in critical code paths."""
        import re
        # Allow TODOs in comments that are clearly instructions to the user
        # Disallow TODOs inside function bodies where they'd cause a NameError
        todo_lines = [
            ln for ln in self.raw.splitlines()
            if "TODO" in ln and not ln.strip().startswith("#")
        ]
        assert len(todo_lines) == 0, (
            f"Template has TODO in non-comment lines: {todo_lines}"
        )
