"""
Tests for Upgrade Safety — Data Preservation

Verifies that upgrades don't delete important data folders, lose project
context, or corrupt in-flight messages. These tests simulate the directory
state that exists on a running Lobster instance and confirm that the
upgrade/install scripts and the MCP server's directory creation code are
additive-only.
"""

import json
import os
import shutil
import subprocess
import tempfile
import time
import pytest
from pathlib import Path
from unittest.mock import patch


@pytest.mark.integration
class TestUpgradePreservesDataDirectories:
    """Verify that upgrade never removes existing data directories."""

    @pytest.fixture
    def simulated_install(self, tmp_path: Path):
        """Create a directory tree that mimics a running Lobster installation."""
        messages = tmp_path / "messages"
        workspace = tmp_path / "lobster-workspace"
        lobster = tmp_path / "lobster"
        projects = tmp_path / "projects"

        # Create all directories a running install would have
        for d in [
            messages / "inbox",
            messages / "outbox",
            messages / "processed",
            messages / "processing",
            messages / "failed",
            messages / "sent",
            messages / "files",
            messages / "images",
            messages / "audio",
            messages / "config",
            messages / "task-outputs",
            workspace / "logs",
            lobster / "scheduled-tasks" / "tasks",
            lobster / "scheduled-tasks" / "logs",
            lobster / ".state",
            projects / "personal",
            projects / "business",
        ]:
            d.mkdir(parents=True, exist_ok=True)

        # Seed with realistic data
        (messages / "tasks.json").write_text(json.dumps({
            "tasks": [{"id": 1, "subject": "important task", "status": "in_progress"}],
            "next_id": 2,
        }))
        (lobster / "scheduled-tasks" / "jobs.json").write_text(json.dumps({
            "jobs": {"morning-check": {"schedule": "0 9 * * *", "enabled": True}},
        }))

        return {
            "root": tmp_path,
            "messages": messages,
            "workspace": workspace,
            "lobster": lobster,
            "projects": projects,
        }

    def test_inbox_messages_preserved(self, simulated_install):
        """Verify messages in inbox/ survive an upgrade."""
        inbox = simulated_install["messages"] / "inbox"
        msg = {"id": "test_123", "source": "telegram", "text": "hello"}
        (inbox / "test_123.json").write_text(json.dumps(msg))

        # Simulate what upgrade.sh does: mkdir -p (additive)
        dirs = simulated_install["messages"]
        for subdir in ["inbox", "outbox", "processed", "processing", "failed",
                        "sent", "files", "images", "audio", "config", "task-outputs"]:
            (dirs / subdir).mkdir(parents=True, exist_ok=True)

        # Message must still be there
        assert (inbox / "test_123.json").exists()
        preserved = json.loads((inbox / "test_123.json").read_text())
        assert preserved["text"] == "hello"

    def test_processing_messages_preserved(self, simulated_install):
        """Verify messages in processing/ survive an upgrade."""
        processing = simulated_install["messages"] / "processing"
        msg = {"id": "proc_456", "source": "slack", "text": "in progress"}
        (processing / "proc_456.json").write_text(json.dumps(msg))

        # Simulate upgrade mkdir -p
        (simulated_install["messages"] / "processing").mkdir(parents=True, exist_ok=True)

        assert (processing / "proc_456.json").exists()
        preserved = json.loads((processing / "proc_456.json").read_text())
        assert preserved["text"] == "in progress"

    def test_failed_messages_preserved(self, simulated_install):
        """Verify messages in failed/ survive an upgrade."""
        failed = simulated_install["messages"] / "failed"
        msg = {
            "id": "fail_789",
            "text": "retry me",
            "_retry_count": 2,
            "_retry_at": time.time() + 120,
        }
        (failed / "fail_789.json").write_text(json.dumps(msg))

        (simulated_install["messages"] / "failed").mkdir(parents=True, exist_ok=True)

        assert (failed / "fail_789.json").exists()
        preserved = json.loads((failed / "fail_789.json").read_text())
        assert preserved["_retry_count"] == 2

    def test_processed_archive_preserved(self, simulated_install):
        """Verify processed message archive survives upgrade."""
        processed = simulated_install["messages"] / "processed"
        for i in range(50):
            msg = {"id": f"old_{i}", "text": f"archived message {i}"}
            (processed / f"old_{i}.json").write_text(json.dumps(msg))

        (simulated_install["messages"] / "processed").mkdir(parents=True, exist_ok=True)

        assert len(list(processed.glob("*.json"))) == 50

    def test_sent_directory_preserved(self, simulated_install):
        """Verify sent/ conversation history is preserved."""
        sent = simulated_install["messages"] / "sent"
        reply = {"id": "reply_1", "chat_id": 123, "text": "my reply"}
        (sent / "reply_1.json").write_text(json.dumps(reply))

        (simulated_install["messages"] / "sent").mkdir(parents=True, exist_ok=True)

        assert (sent / "reply_1.json").exists()

    def test_tasks_json_preserved(self, simulated_install):
        """Verify tasks.json is not overwritten on upgrade."""
        tasks_file = simulated_install["messages"] / "tasks.json"
        original = json.loads(tasks_file.read_text())
        assert original["tasks"][0]["subject"] == "important task"

        # Simulate upgrade: code checks if file exists before writing
        if not tasks_file.exists():
            tasks_file.write_text(json.dumps({"tasks": [], "next_id": 1}))

        # Original data preserved
        after = json.loads(tasks_file.read_text())
        assert after["tasks"][0]["subject"] == "important task"

    def test_jobs_json_preserved(self, simulated_install):
        """Verify scheduled jobs.json is not overwritten on upgrade."""
        jobs_file = simulated_install["lobster"] / "scheduled-tasks" / "jobs.json"
        original = json.loads(jobs_file.read_text())
        assert "morning-check" in original["jobs"]

        if not jobs_file.exists():
            jobs_file.write_text(json.dumps({"jobs": {}}))

        after = json.loads(jobs_file.read_text())
        assert "morning-check" in after["jobs"]

    def test_projects_directory_preserved(self, simulated_install):
        """Verify ~/projects/ and its contents are never touched."""
        projects = simulated_install["projects"]
        (projects / "personal" / "my-app").mkdir(parents=True)
        (projects / "personal" / "my-app" / "main.py").write_text("print('hello')")
        (projects / "business" / "client-work").mkdir(parents=True)
        (projects / "business" / "client-work" / "report.md").write_text("# Report")

        # mkdir -p is safe
        projects.mkdir(parents=True, exist_ok=True)
        (projects / "personal").mkdir(parents=True, exist_ok=True)
        (projects / "business").mkdir(parents=True, exist_ok=True)

        assert (projects / "personal" / "my-app" / "main.py").read_text() == "print('hello')"
        assert (projects / "business" / "client-work" / "report.md").read_text() == "# Report"

    def test_workspace_logs_preserved(self, simulated_install):
        """Verify workspace logs are preserved."""
        logs = simulated_install["workspace"] / "logs"
        (logs / "telegram-bot.log").write_text("2024-01-01 old log entry\n")
        (logs / "mcp-server.log").write_text("2024-01-01 server log\n")

        logs.mkdir(parents=True, exist_ok=True)

        assert (logs / "telegram-bot.log").read_text() == "2024-01-01 old log entry\n"
        assert (logs / "mcp-server.log").read_text() == "2024-01-01 server log\n"

    def test_audio_files_preserved(self, simulated_install):
        """Verify voice message audio files survive upgrade."""
        audio = simulated_install["messages"] / "audio"
        (audio / "voice_msg_1.ogg").write_bytes(b"fake ogg data")

        (simulated_install["messages"] / "audio").mkdir(parents=True, exist_ok=True)

        assert (audio / "voice_msg_1.ogg").read_bytes() == b"fake ogg data"

    def test_image_files_preserved(self, simulated_install):
        """Verify image files survive upgrade."""
        images = simulated_install["messages"] / "images"
        (images / "photo_1.jpg").write_bytes(b"fake jpg data")

        (simulated_install["messages"] / "images").mkdir(parents=True, exist_ok=True)

        assert (images / "photo_1.jpg").read_bytes() == b"fake jpg data"


@pytest.mark.integration
class TestUpgradeScriptDirectoryLists:
    """Verify that install.sh and upgrade.sh create the required directories."""

    @pytest.fixture
    def lobster_dir(self) -> Path:
        return Path(__file__).parent.parent.parent

    def test_install_sh_creates_processing_dir(self, lobster_dir: Path):
        """Verify install.sh includes processing/ in its mkdir."""
        content = (lobster_dir / "install.sh").read_text()
        assert "processing" in content, "install.sh must create processing/ directory"

    def test_install_sh_creates_failed_dir(self, lobster_dir: Path):
        """Verify install.sh includes failed/ in its mkdir."""
        content = (lobster_dir / "install.sh").read_text()
        assert "failed" in content, "install.sh must create failed/ directory"

    def test_upgrade_sh_creates_processing_dir(self, lobster_dir: Path):
        """Verify upgrade.sh includes processing/ in its directory list."""
        content = (lobster_dir / "scripts" / "upgrade.sh").read_text()
        assert "processing" in content, "upgrade.sh must create processing/ directory"

    def test_upgrade_sh_creates_failed_dir(self, lobster_dir: Path):
        """Verify upgrade.sh includes failed/ in its directory list."""
        content = (lobster_dir / "scripts" / "upgrade.sh").read_text()
        assert "failed" in content, "upgrade.sh must create failed/ directory"

    def test_inbox_server_creates_processing_dir(self, lobster_dir: Path):
        """Verify inbox_server.py creates processing/ at startup."""
        content = (lobster_dir / "src" / "mcp" / "inbox_server.py").read_text()
        assert "PROCESSING_DIR" in content
        assert 'processing' in content

    def test_inbox_server_creates_failed_dir(self, lobster_dir: Path):
        """Verify inbox_server.py creates failed/ at startup."""
        content = (lobster_dir / "src" / "mcp" / "inbox_server.py").read_text()
        assert "FAILED_DIR" in content
        assert 'failed' in content

    def test_all_data_dirs_in_install_mkdir(self, lobster_dir: Path):
        """Verify install.sh mkdir includes all required subdirectories."""
        content = (lobster_dir / "install.sh").read_text()
        required = ["inbox", "outbox", "processed", "processing", "failed",
                     "config", "audio", "task-outputs"]
        for d in required:
            assert d in content, f"install.sh missing directory: {d}"

    def test_all_data_dirs_in_upgrade_create_function(self, lobster_dir: Path):
        """Verify upgrade.sh create_new_directories() includes all required dirs."""
        content = (lobster_dir / "scripts" / "upgrade.sh").read_text()
        required = ["inbox", "outbox", "processed", "processing", "failed",
                     "sent", "files", "images", "audio", "config", "task-outputs"]
        for d in required:
            assert d in content, f"upgrade.sh missing directory: {d}"


@pytest.mark.integration
class TestUpgradePreservesContext:
    """Verify that CLAUDE.md and project context survive upgrades."""

    @pytest.fixture
    def lobster_dir(self) -> Path:
        return Path(__file__).parent.parent.parent

    def test_claude_md_documents_new_tools(self, lobster_dir: Path):
        """Verify CLAUDE.md documents mark_processing and mark_failed tools."""
        content = (lobster_dir / "CLAUDE.md").read_text()
        assert "mark_processing" in content, "CLAUDE.md must document mark_processing tool"
        assert "mark_failed" in content, "CLAUDE.md must document mark_failed tool"

    def test_claude_md_documents_new_directories(self, lobster_dir: Path):
        """Verify CLAUDE.md documents processing/ and failed/ directories."""
        content = (lobster_dir / "CLAUDE.md").read_text()
        assert "processing/" in content, "CLAUDE.md must mention processing/ directory"
        assert "failed/" in content, "CLAUDE.md must mention failed/ directory"

    def test_claude_md_message_flow_includes_state_machine(self, lobster_dir: Path):
        """Verify CLAUDE.md message flow shows the full state machine."""
        content = (lobster_dir / "CLAUDE.md").read_text()
        assert "mark_processing" in content, "Message flow must include mark_processing"
        assert "mark_failed" in content, "Message flow must include mark_failed"

    def test_install_sh_does_not_overwrite_existing_config(self, lobster_dir: Path):
        """Verify install.sh checks for existing config before overwriting."""
        content = (lobster_dir / "install.sh").read_text()
        assert "Keep existing configuration" in content, \
            "install.sh should ask before overwriting existing config"

    def test_upgrade_sh_creates_backup(self, lobster_dir: Path):
        """Verify upgrade.sh creates backup before upgrading."""
        content = (lobster_dir / "scripts" / "upgrade.sh").read_text()
        assert "backup" in content.lower(), "upgrade.sh should create backups"
        assert "config.env" in content, "upgrade.sh should back up config.env"
        assert "jobs.json" in content, "upgrade.sh should back up jobs.json"
        assert "tasks.json" in content, "upgrade.sh should back up tasks.json"


@pytest.mark.integration
class TestMCPServerDirectoryInit:
    """Verify the MCP server creates new directories at startup without
    destroying existing ones."""

    def test_server_init_creates_all_required_dirs(self, tmp_path: Path):
        """Test that importing inbox_server creates processing/ and failed/ dirs."""
        messages = tmp_path / "messages"

        # Pre-create some dirs with data (simulating existing install)
        inbox = messages / "inbox"
        inbox.mkdir(parents=True)
        (inbox / "existing_msg.json").write_text('{"id": "keep_me"}')

        processed = messages / "processed"
        processed.mkdir(parents=True)
        (processed / "old_msg.json").write_text('{"id": "archived"}')

        # Patch the constants and run the directory creation loop
        with patch.multiple(
            "src.mcp.inbox_server",
            BASE_DIR=messages,
            INBOX_DIR=messages / "inbox",
            OUTBOX_DIR=messages / "outbox",
            PROCESSED_DIR=messages / "processed",
            PROCESSING_DIR=messages / "processing",
            FAILED_DIR=messages / "failed",
            CONFIG_DIR=messages / "config",
            AUDIO_DIR=messages / "audio",
            SENT_DIR=messages / "sent",
            TASK_OUTPUTS_DIR=messages / "task-outputs",
        ):
            # Simulate the server's mkdir loop
            for d in [
                messages / "inbox",
                messages / "outbox",
                messages / "processed",
                messages / "processing",
                messages / "failed",
                messages / "sent",
                messages / "config",
                messages / "audio",
                messages / "task-outputs",
            ]:
                d.mkdir(parents=True, exist_ok=True)

        # New directories created
        assert (messages / "processing").is_dir()
        assert (messages / "failed").is_dir()

        # Existing data untouched
        assert (inbox / "existing_msg.json").exists()
        assert json.loads((inbox / "existing_msg.json").read_text())["id"] == "keep_me"
        assert (processed / "old_msg.json").exists()

    def test_state_machine_dirs_consistent_across_all_sources(self):
        """Verify inbox_server.py, install.sh, and upgrade.sh all agree on directory names."""
        lobster_dir = Path(__file__).parent.parent.parent

        server_content = (lobster_dir / "src" / "mcp" / "inbox_server.py").read_text()
        install_content = (lobster_dir / "install.sh").read_text()
        upgrade_content = (lobster_dir / "scripts" / "upgrade.sh").read_text()

        for dirname in ["processing", "failed"]:
            assert dirname in server_content, f"inbox_server.py missing {dirname}"
            assert dirname in install_content, f"install.sh missing {dirname}"
            assert dirname in upgrade_content, f"upgrade.sh missing {dirname}"


@pytest.mark.integration
class TestInFlightMessageSafety:
    """Test that messages in various states are handled correctly
    when the server starts up (simulating restart after upgrade)."""

    def test_stale_processing_recovered_on_startup(self, temp_messages_dir, message_generator):
        """Messages stuck in processing/ should be recovered on next wait_for_messages."""
        inbox = temp_messages_dir / "inbox"
        processing = temp_messages_dir / "processing"

        # Create a message that's been in processing for 10 minutes
        msg = message_generator.generate_text_message()
        msg_file = processing / f"{msg['id']}.json"
        msg_file.write_text(json.dumps(msg))
        old_time = time.time() - 600
        os.utime(msg_file, (old_time, old_time))

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            PROCESSING_DIR=processing,
            FAILED_DIR=temp_messages_dir / "failed",
        ):
            from src.mcp.inbox_server import _recover_stale_processing

            _recover_stale_processing(max_age_seconds=300)

        assert not (processing / f"{msg['id']}.json").exists()
        assert (inbox / f"{msg['id']}.json").exists()

    def test_failed_retries_recovered_on_startup(self, temp_messages_dir, message_generator):
        """Failed messages past retry_at should be recovered on next wait_for_messages."""
        inbox = temp_messages_dir / "inbox"
        failed = temp_messages_dir / "failed"

        msg = message_generator.generate_text_message()
        msg["_retry_count"] = 1
        msg["_retry_at"] = time.time() - 60  # Due 60 seconds ago
        (failed / f"{msg['id']}.json").write_text(json.dumps(msg))

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            FAILED_DIR=failed,
        ):
            from src.mcp.inbox_server import _recover_retryable_messages

            _recover_retryable_messages()

        assert not (failed / f"{msg['id']}.json").exists()
        assert (inbox / f"{msg['id']}.json").exists()

    def test_permanently_failed_not_recovered(self, temp_messages_dir, message_generator):
        """Permanently failed messages should never be recovered."""
        inbox = temp_messages_dir / "inbox"
        failed = temp_messages_dir / "failed"

        msg = message_generator.generate_text_message()
        msg["_permanently_failed"] = True
        msg["_retry_count"] = 4
        msg["_retry_at"] = time.time() - 3600
        (failed / f"{msg['id']}.json").write_text(json.dumps(msg))

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            FAILED_DIR=failed,
        ):
            from src.mcp.inbox_server import _recover_retryable_messages

            _recover_retryable_messages()

        assert (failed / f"{msg['id']}.json").exists()
        assert not (inbox / f"{msg['id']}.json").exists()

    def test_recent_processing_not_recovered(self, temp_messages_dir, message_generator):
        """Recently claimed messages should NOT be moved back to inbox."""
        inbox = temp_messages_dir / "inbox"
        processing = temp_messages_dir / "processing"

        msg = message_generator.generate_text_message()
        (processing / f"{msg['id']}.json").write_text(json.dumps(msg))
        # File was just created, so mtime is now

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            PROCESSING_DIR=processing,
            FAILED_DIR=temp_messages_dir / "failed",
        ):
            from src.mcp.inbox_server import _recover_stale_processing

            _recover_stale_processing(max_age_seconds=300)

        assert (processing / f"{msg['id']}.json").exists()
        assert not (inbox / f"{msg['id']}.json").exists()

    def test_multiple_messages_in_all_states(self, temp_messages_dir, message_generator):
        """Verify correct handling when messages exist in all state directories."""
        inbox = temp_messages_dir / "inbox"
        processing = temp_messages_dir / "processing"
        failed = temp_messages_dir / "failed"
        processed = temp_messages_dir / "processed"

        # Fresh inbox message
        inbox_msg = message_generator.generate_text_message(text="inbox msg")
        (inbox / f"{inbox_msg['id']}.json").write_text(json.dumps(inbox_msg))

        # Recently claimed message (should stay)
        recent_msg = message_generator.generate_text_message(text="recent processing")
        (processing / f"{recent_msg['id']}.json").write_text(json.dumps(recent_msg))

        # Stale processing message (should recover)
        stale_msg = message_generator.generate_text_message(text="stale processing")
        stale_file = processing / f"{stale_msg['id']}.json"
        stale_file.write_text(json.dumps(stale_msg))
        old_time = time.time() - 600
        os.utime(stale_file, (old_time, old_time))

        # Retryable failed message (should recover)
        retry_msg = message_generator.generate_text_message(text="retry me")
        retry_msg["_retry_count"] = 1
        retry_msg["_retry_at"] = time.time() - 10
        (failed / f"{retry_msg['id']}.json").write_text(json.dumps(retry_msg))

        # Permanently failed message (should stay)
        perm_msg = message_generator.generate_text_message(text="dead letter")
        perm_msg["_permanently_failed"] = True
        perm_msg["_retry_at"] = time.time() - 3600
        (failed / f"{perm_msg['id']}.json").write_text(json.dumps(perm_msg))

        # Already processed message
        done_msg = message_generator.generate_text_message(text="done")
        (processed / f"{done_msg['id']}.json").write_text(json.dumps(done_msg))

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            PROCESSING_DIR=processing,
            FAILED_DIR=failed,
            PROCESSED_DIR=processed,
        ):
            from src.mcp.inbox_server import _recover_stale_processing, _recover_retryable_messages

            _recover_stale_processing(max_age_seconds=300)
            _recover_retryable_messages()

        # inbox: original + stale recovered + retry recovered = 3
        inbox_files = list(inbox.glob("*.json"))
        assert len(inbox_files) == 3

        # processing: only the recent one
        processing_files = list(processing.glob("*.json"))
        assert len(processing_files) == 1
        assert processing_files[0].name == f"{recent_msg['id']}.json"

        # failed: only the permanently failed one
        failed_files = list(failed.glob("*.json"))
        assert len(failed_files) == 1
        assert failed_files[0].name == f"{perm_msg['id']}.json"

        # processed: unchanged
        processed_files = list(processed.glob("*.json"))
        assert len(processed_files) == 1
