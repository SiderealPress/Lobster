"""
Tests for Full Message Flow

Tests end-to-end message processing through the system.
"""

import asyncio
import json
import pytest
import time
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock


@pytest.mark.integration
class TestMessageFlow:
    """Tests for end-to-end message flow."""

    @pytest.fixture
    def message_dirs(self, temp_messages_dir: Path):
        """Set up message directories."""
        return {
            "inbox": temp_messages_dir / "inbox",
            "outbox": temp_messages_dir / "outbox",
            "processed": temp_messages_dir / "processed",
        }

    def test_message_arrives_in_inbox(
        self, message_dirs, message_generator
    ):
        """Test that messages arrive in inbox correctly."""
        inbox = message_dirs["inbox"]

        msg = message_generator.generate_text_message(
            text="Hello from test",
            user_name="TestUser",
            chat_id=123456,
        )

        msg_file = inbox / f"{msg['id']}.json"
        msg_file.write_text(json.dumps(msg))

        assert msg_file.exists()

        content = json.loads(msg_file.read_text())
        assert content["text"] == "Hello from test"
        assert content["chat_id"] == 123456

    def test_reply_created_in_outbox(self, message_dirs):
        """Test that replies are created in outbox."""
        outbox = message_dirs["outbox"]

        reply = {
            "id": f"reply_{int(time.time() * 1000)}",
            "source": "telegram",
            "chat_id": 123456,
            "text": "This is a reply",
            "timestamp": "2024-01-01T00:00:00Z",
        }

        reply_file = outbox / f"{reply['id']}.json"
        reply_file.write_text(json.dumps(reply))

        assert reply_file.exists()

        content = json.loads(reply_file.read_text())
        assert content["text"] == "This is a reply"

    def test_processed_message_moves(self, message_dirs, message_generator):
        """Test that processed messages move to processed directory."""
        inbox = message_dirs["inbox"]
        processed = message_dirs["processed"]

        msg = message_generator.generate_text_message()
        msg_file = inbox / f"{msg['id']}.json"
        msg_file.write_text(json.dumps(msg))

        # Simulate processing (move file)
        dest = processed / msg_file.name
        msg_file.rename(dest)

        assert not msg_file.exists()
        assert dest.exists()


@pytest.mark.integration
class TestMCPToolFlow:
    """Tests for MCP tool-based message flow."""

    @pytest.fixture
    def mcp_dirs(self, temp_messages_dir: Path, temp_scheduled_tasks_dir: Path):
        """Set up directories for MCP testing."""
        # Initialize tasks.json
        (temp_messages_dir / "tasks.json").write_text(
            json.dumps({"tasks": [], "next_id": 1})
        )

        return {
            "inbox": temp_messages_dir / "inbox",
            "outbox": temp_messages_dir / "outbox",
            "processed": temp_messages_dir / "processed",
            "tasks_file": temp_messages_dir / "tasks.json",
            "base": temp_messages_dir,
        }

    @pytest.mark.asyncio
    async def test_check_inbox_then_reply_flow(
        self, mcp_dirs, message_generator
    ):
        """Test the check_inbox -> send_reply flow."""
        inbox = mcp_dirs["inbox"]
        outbox = mcp_dirs["outbox"]

        # Create incoming message
        msg = message_generator.generate_text_message(
            text="What's the weather?",
            chat_id=123456,
        )
        (inbox / f"{msg['id']}.json").write_text(json.dumps(msg))

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            OUTBOX_DIR=outbox,
        ):
            from src.mcp.inbox_server import (
                handle_check_inbox,
                handle_send_reply,
            )

            # Check inbox
            check_result = await handle_check_inbox({})
            assert "1 new message" in check_result[0].text
            assert "What's the weather" in check_result[0].text

            # Send reply
            reply_result = await handle_send_reply({
                "chat_id": 123456,
                "text": "It's sunny today!",
            })
            assert "Reply queued" in reply_result[0].text

            # Verify reply file exists
            outbox_files = list(outbox.glob("*.json"))
            assert len(outbox_files) == 1

            reply_content = json.loads(outbox_files[0].read_text())
            assert reply_content["text"] == "It's sunny today!"
            assert reply_content["chat_id"] == 123456

    @pytest.mark.asyncio
    async def test_full_message_lifecycle(
        self, mcp_dirs, message_generator
    ):
        """Test complete message lifecycle: receive -> process -> reply -> mark done."""
        inbox = mcp_dirs["inbox"]
        outbox = mcp_dirs["outbox"]
        processed = mcp_dirs["processed"]

        # Create incoming message
        msg = message_generator.generate_text_message(
            text="Hello Lobster!",
            chat_id=123456,
        )
        msg_id = msg["id"]
        (inbox / f"{msg_id}.json").write_text(json.dumps(msg))

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            OUTBOX_DIR=outbox,
            PROCESSED_DIR=processed,
        ):
            from src.mcp.inbox_server import (
                handle_check_inbox,
                handle_send_reply,
                handle_mark_processed,
            )

            # 1. Check inbox
            check_result = await handle_check_inbox({})
            assert "1 new message" in check_result[0].text

            # 2. Send reply
            await handle_send_reply({
                "chat_id": 123456,
                "text": "Hello! How can I help?",
            })

            # 3. Mark as processed
            process_result = await handle_mark_processed({"message_id": msg_id})
            assert "processed" in process_result[0].text.lower()

            # 4. Verify state
            assert len(list(inbox.glob("*.json"))) == 0
            assert len(list(processed.glob("*.json"))) == 1
            assert len(list(outbox.glob("*.json"))) == 1


@pytest.mark.integration
class TestTaskFlow:
    """Tests for task management flow."""

    @pytest.fixture
    def tasks_file(self, temp_messages_dir: Path) -> Path:
        """Create tasks file."""
        tasks_path = temp_messages_dir / "tasks.json"
        tasks_path.write_text(json.dumps({"tasks": [], "next_id": 1}))
        return tasks_path

    @pytest.mark.asyncio
    async def test_task_lifecycle(self, tasks_file: Path):
        """Test complete task lifecycle: create -> update -> complete -> delete."""
        with patch("src.mcp.inbox_server.TASKS_FILE", tasks_file):
            from src.mcp.inbox_server import (
                handle_create_task,
                handle_update_task,
                handle_list_tasks,
                handle_delete_task,
            )

            # 1. Create task
            create_result = await handle_create_task({
                "subject": "Test Task",
                "description": "This is a test",
            })
            assert "#1" in create_result[0].text

            # 2. List tasks
            list_result = await handle_list_tasks({})
            assert "Test Task" in list_result[0].text

            # 3. Update to in_progress
            await handle_update_task({
                "task_id": 1,
                "status": "in_progress",
            })

            # 4. Complete task
            await handle_update_task({
                "task_id": 1,
                "status": "completed",
            })

            # 5. Verify completed
            list_result = await handle_list_tasks({"status": "completed"})
            assert "Test Task" in list_result[0].text

            # 6. Delete task
            delete_result = await handle_delete_task({"task_id": 1})
            assert "deleted" in delete_result[0].text.lower()

            # 7. Verify deleted
            list_result = await handle_list_tasks({})
            assert "No tasks" in list_result[0].text
