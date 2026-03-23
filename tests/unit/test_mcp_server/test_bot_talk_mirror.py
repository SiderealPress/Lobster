"""
Unit tests for bot_talk_mirror module.

Tests cover:
- Payload and log-line builders (pure functions)
- HTTP attempt logic (mock httpx)
- SSH fallback logic (mock subprocess)
- Local log fallback
- mirror_outbound / mirror_inbound filtering
- Thread spawning (daemon thread is started)
"""

import json
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

# Ensure src/mcp is on sys.path so bot_talk_mirror can be imported directly.
_MCP_DIR = Path(__file__).parent.parent.parent.parent / "src" / "mcp"
if str(_MCP_DIR) not in sys.path:
    sys.path.insert(0, str(_MCP_DIR))

import bot_talk_mirror as btm


# ---------------------------------------------------------------------------
# Pure builder tests
# ---------------------------------------------------------------------------

class TestBuildHttpPayload:
    def test_required_fields_present(self):
        payload = btm._build_http_payload("hello", "status-update")
        assert payload["sender"] == btm.BOT_TALK_SENDER
        assert payload["tier"] == btm.BOT_TALK_TIER
        assert payload["genre"] == "status-update"
        assert payload["content"] == "hello"

    def test_content_is_passed_through(self):
        payload = btm._build_http_payload("some content here", "query")
        assert payload["content"] == "some content here"

    def test_sender_is_saharlобster(self):
        payload = btm._build_http_payload("x", "status-update")
        assert payload["sender"] == "SaharLobster"


class TestBuildSshLogLine:
    def test_log_line_contains_sender_tier_genre(self):
        line = btm._build_ssh_log_line("msg content", "status-update")
        assert "[SaharLobster]" in line
        assert "[TIER-BOT]" in line
        assert "[status-update]" in line

    def test_long_content_truncated_to_200(self):
        long_content = "x" * 300
        line = btm._build_ssh_log_line(long_content, "status-update")
        # 200 chars of content + surrounding brackets and timestamp
        assert "x" * 200 in line
        assert "x" * 201 not in line

    def test_newlines_replaced_in_log_line(self):
        line = btm._build_ssh_log_line("line1\nline2", "status-update")
        assert "\n" not in line


# ---------------------------------------------------------------------------
# HTTP attempt logic
# ---------------------------------------------------------------------------

_FAKE_HTTP_URL = "http://test-bot-talk:4242/message"


class TestTryHttp:
    def test_returns_true_on_201(self):
        mock_response = MagicMock()
        mock_response.status_code = 201
        with patch.object(btm, "BOT_TALK_HTTP_URL", _FAKE_HTTP_URL), \
             patch("bot_talk_mirror.httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.return_value = mock_response
            mock_client_cls.return_value = mock_client

            result = btm._try_http({"sender": "SaharLobster", "content": "x", "tier": "TIER-BOT", "genre": "status-update"})

        assert result is True

    def test_returns_true_on_200(self):
        mock_response = MagicMock()
        mock_response.status_code = 200
        with patch.object(btm, "BOT_TALK_HTTP_URL", _FAKE_HTTP_URL), \
             patch("bot_talk_mirror.httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.return_value = mock_response
            mock_client_cls.return_value = mock_client

            result = btm._try_http({})

        assert result is True

    def test_returns_false_when_url_empty(self):
        """When BOT_TALK_HTTP_URL is empty, _try_http returns False immediately."""
        with patch.object(btm, "BOT_TALK_HTTP_URL", ""), \
             patch("bot_talk_mirror.httpx.Client") as mock_client_cls:
            result = btm._try_http({})
        assert result is False
        mock_client_cls.assert_not_called()

    def test_returns_false_on_500(self):
        mock_response = MagicMock()
        mock_response.status_code = 500
        with patch.object(btm, "BOT_TALK_HTTP_URL", _FAKE_HTTP_URL), \
             patch("bot_talk_mirror.httpx.Client") as mock_client_cls, \
             patch("bot_talk_mirror.time.sleep"):
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.return_value = mock_response
            mock_client_cls.return_value = mock_client

            result = btm._try_http({})

        assert result is False

    def test_returns_false_on_connection_error(self):
        with patch.object(btm, "BOT_TALK_HTTP_URL", _FAKE_HTTP_URL), \
             patch("bot_talk_mirror.httpx.Client") as mock_client_cls, \
             patch("bot_talk_mirror.time.sleep"):
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.side_effect = Exception("connection refused")
            mock_client_cls.return_value = mock_client

            result = btm._try_http({})

        assert result is False

    def test_retries_on_failure(self):
        """Confirms that _try_http makes up to BOT_TALK_HTTP_RETRIES + 1 attempts."""
        call_count = 0

        def counting_post(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            raise Exception("fail")

        with patch.object(btm, "BOT_TALK_HTTP_URL", _FAKE_HTTP_URL), \
             patch("bot_talk_mirror.httpx.Client") as mock_client_cls, \
             patch("bot_talk_mirror.time.sleep"):
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.side_effect = counting_post
            mock_client_cls.return_value = mock_client

            btm._try_http({})

        assert call_count == btm.BOT_TALK_HTTP_RETRIES + 1


# ---------------------------------------------------------------------------
# SSH fallback
# ---------------------------------------------------------------------------

class TestTrySsh:
    def test_returns_true_on_success(self):
        with patch("bot_talk_mirror.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            result = btm._try_ssh("some log line")
        assert result is True

    def test_returns_false_on_nonzero_exit(self):
        with patch("bot_talk_mirror.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1)
            result = btm._try_ssh("some log line")
        assert result is False

    def test_returns_false_on_exception(self):
        with patch("bot_talk_mirror.subprocess.run", side_effect=Exception("timeout")):
            result = btm._try_ssh("some log line")
        assert result is False

    def test_ssh_command_contains_host(self):
        with patch("bot_talk_mirror.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            btm._try_ssh("log line content")
        cmd = mock_run.call_args[0][0]
        assert btm.BOT_TALK_SSH_HOST in cmd


# ---------------------------------------------------------------------------
# Local log fallback
# ---------------------------------------------------------------------------

class TestWriteLocalLog:
    def test_writes_json_entry(self, tmp_path):
        with patch.object(btm, "_LOCAL_LOG", tmp_path / "bot-talk-mirror.log"):
            btm._write_local_log("test content", "status-update", "http_and_ssh_both_failed")

        log_file = tmp_path / "bot-talk-mirror.log"
        assert log_file.exists()
        lines = log_file.read_text().strip().splitlines()
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["sender"] == "SaharLobster"
        assert entry["genre"] == "status-update"
        assert "test content" in entry["content"]
        assert entry["mirror_failed_reason"] == "http_and_ssh_both_failed"

    def test_does_not_raise_on_write_error(self):
        """_write_local_log must never raise, even if the path is unwritable."""
        with patch.object(btm, "_LOCAL_LOG", Path("/nonexistent/readonly/path/log.log")):
            # Should not raise
            btm._write_local_log("content", "status-update", "reason")


# ---------------------------------------------------------------------------
# do_mirror integration (unit-level: mock all I/O)
# ---------------------------------------------------------------------------

class TestDoMirror:
    def test_http_success_skips_ssh_and_local(self):
        with patch.object(btm, "_try_http", return_value=True) as mock_http, \
             patch.object(btm, "_try_ssh") as mock_ssh, \
             patch.object(btm, "_write_local_log") as mock_local:
            btm._do_mirror("content", "status-update")

        mock_http.assert_called_once()
        mock_ssh.assert_not_called()
        mock_local.assert_not_called()

    def test_http_failure_falls_back_to_ssh(self):
        with patch.object(btm, "_try_http", return_value=False), \
             patch.object(btm, "_try_ssh", return_value=True) as mock_ssh, \
             patch.object(btm, "_write_local_log") as mock_local:
            btm._do_mirror("content", "status-update")

        mock_ssh.assert_called_once()
        mock_local.assert_not_called()

    def test_http_and_ssh_failure_writes_local(self):
        with patch.object(btm, "_try_http", return_value=False), \
             patch.object(btm, "_try_ssh", return_value=False), \
             patch.object(btm, "_write_local_log") as mock_local:
            btm._do_mirror("content", "status-update")

        mock_local.assert_called_once()


# ---------------------------------------------------------------------------
# mirror_outbound
# ---------------------------------------------------------------------------

class TestMirrorOutbound:
    def test_spawns_daemon_thread(self):
        spawned = []

        def capturing_spawn(content, genre):
            spawned.append((content, genre))

        with patch.object(btm, "_spawn_mirror", side_effect=capturing_spawn):
            btm.mirror_outbound("hello world", "telegram", 12345)

        assert len(spawned) == 1
        content, genre = spawned[0]
        assert "OUTBOUND" in content
        assert "TELEGRAM" in content
        assert "12345" in content
        assert "hello world" in content
        assert genre == "status-update"

    def test_includes_source_and_chat_id(self):
        spawned = []
        with patch.object(btm, "_spawn_mirror", side_effect=lambda c, genre: spawned.append(c)):
            btm.mirror_outbound("msg", "slack", 999)

        assert "SLACK" in spawned[0]
        assert "999" in spawned[0]


# ---------------------------------------------------------------------------
# mirror_inbound — filtering
# ---------------------------------------------------------------------------

class TestMirrorInbound:
    def _make_msg(self, msg_type="text", subtype="", source="telegram", user="Alice", text="hi"):
        msg = {
            "type": msg_type,
            "source": source,
            "user_name": user,
            "text": text,
        }
        if subtype:
            msg["subtype"] = subtype
        return msg

    def test_text_message_is_mirrored(self):
        spawned = []
        with patch.object(btm, "_spawn_mirror", side_effect=lambda c, genre: spawned.append(c)):
            btm.mirror_inbound(self._make_msg(msg_type="text", text="hello"))
        assert len(spawned) == 1
        assert "hello" in spawned[0]
        assert "Alice" in spawned[0]

    def test_voice_message_is_mirrored_with_label(self):
        spawned = []
        with patch.object(btm, "_spawn_mirror", side_effect=lambda c, genre: spawned.append(c)):
            btm.mirror_inbound(self._make_msg(msg_type="voice"))
        assert len(spawned) == 1
        assert "voice message" in spawned[0]

    def test_photo_message_is_mirrored(self):
        spawned = []
        with patch.object(btm, "_spawn_mirror", side_effect=lambda c, genre: spawned.append(c)):
            btm.mirror_inbound(self._make_msg(msg_type="photo"))
        assert len(spawned) == 1
        assert "photo" in spawned[0]

    def test_document_message_shows_filename(self):
        spawned = []
        msg = self._make_msg(msg_type="document")
        msg["file_name"] = "report.pdf"
        with patch.object(btm, "_spawn_mirror", side_effect=lambda c, genre: spawned.append(c)):
            btm.mirror_inbound(msg)
        assert "report.pdf" in spawned[0]

    def test_self_check_subtype_excluded(self):
        spawned = []
        with patch.object(btm, "_spawn_mirror", side_effect=lambda c, genre: spawned.append(c)):
            btm.mirror_inbound(self._make_msg(subtype="self_check"))
        assert len(spawned) == 0

    def test_subagent_notification_excluded(self):
        spawned = []
        with patch.object(btm, "_spawn_mirror", side_effect=lambda c, genre: spawned.append(c)):
            btm.mirror_inbound(self._make_msg(msg_type="text", subtype="subagent_notification"))
        assert len(spawned) == 0

    def test_subagent_result_type_excluded(self):
        spawned = []
        with patch.object(btm, "_spawn_mirror", side_effect=lambda c, genre: spawned.append(c)):
            btm.mirror_inbound(self._make_msg(msg_type="subagent_result"))
        assert len(spawned) == 0

    def test_subagent_error_type_excluded(self):
        spawned = []
        with patch.object(btm, "_spawn_mirror", side_effect=lambda c, genre: spawned.append(c)):
            btm.mirror_inbound(self._make_msg(msg_type="subagent_error"))
        assert len(spawned) == 0

    def test_includes_source_in_content(self):
        spawned = []
        with patch.object(btm, "_spawn_mirror", side_effect=lambda c, genre: spawned.append(c)):
            btm.mirror_inbound(self._make_msg(source="slack"))
        assert "SLACK" in spawned[0]


# ---------------------------------------------------------------------------
# _spawn_mirror actually starts a daemon thread
# ---------------------------------------------------------------------------

class TestSpawnMirror:
    def test_spawns_daemon_thread(self):
        """_spawn_mirror must start a daemon thread that calls _do_mirror."""
        called = threading.Event()

        def fake_do_mirror(content, genre):
            called.set()

        with patch.object(btm, "_do_mirror", side_effect=fake_do_mirror):
            btm._spawn_mirror("test content", "status-update")
            called.wait(timeout=2.0)

        assert called.is_set(), "_do_mirror was not called within 2 seconds"
