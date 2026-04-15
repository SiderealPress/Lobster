"""
Unit tests for bot_talk_mirror module — cross-Lobster channel redesign.

Tests cover:
- Payload and log-line builders (pure functions)
- HTTP attempt logic (mock httpx)
- SSH fallback logic (mock subprocess)
- Local log fallback
- mirror_outbound: records OUTBOUND direction, from/to fields
- log_inbound_cross_lobster: records INBOUND direction, routes to inbox
- _route_to_inbox: writes correctly structured inbox file
- _emit_event_bus: emits LobsterEvent with correct payload
- Thread spawning (daemon thread is started)
- Old mirror_inbound() does not exist (removed)
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
        payload = btm._build_http_payload("hello", "status-update", "OUTBOUND", "SaharLobster", "AlbertLobster")
        assert payload["sender"] == btm.LOBSTER_NAME
        assert payload["tier"] == btm.BOT_TALK_TIER
        assert payload["genre"] == "status-update"
        assert payload["content"] == "hello"

    def test_direction_from_to_fields_present(self):
        payload = btm._build_http_payload("msg", "status-update", "OUTBOUND", "SaharLobster", "AlbertLobster")
        assert payload["direction"] == "OUTBOUND"
        assert payload["from"] == "SaharLobster"
        assert payload["to"] == "AlbertLobster"

    def test_inbound_direction(self):
        payload = btm._build_http_payload("msg", "status-update", "INBOUND", "AlbertLobster", "SaharLobster")
        assert payload["direction"] == "INBOUND"
        assert payload["from"] == "AlbertLobster"
        assert payload["to"] == "SaharLobster"

    def test_content_is_passed_through(self):
        payload = btm._build_http_payload("some content here", "query", "OUTBOUND", "A", "B")
        assert payload["content"] == "some content here"


class TestBuildSshLogLine:
    def test_log_line_contains_sender_tier_genre(self):
        line = btm._build_ssh_log_line("msg content", "status-update")
        assert f"[{btm.LOBSTER_NAME}]" in line
        assert "[TIER-BOT]" in line
        assert "[status-update]" in line

    def test_direction_included_when_provided(self):
        line = btm._build_ssh_log_line("msg", "status-update", "OUTBOUND")
        assert "[OUTBOUND]" in line

    def test_no_direction_bracket_when_empty(self):
        line = btm._build_ssh_log_line("msg", "status-update")
        # Should not include empty direction brackets like "[]"
        assert "[]" not in line

    def test_long_content_truncated_to_200(self):
        long_content = "x" * 300
        line = btm._build_ssh_log_line(long_content, "status-update")
        assert "x" * 200 in line
        assert "x" * 201 not in line

    def test_newlines_replaced_in_log_line(self):
        line = btm._build_ssh_log_line("line1\nline2", "status-update")
        assert "\n" not in line


# ---------------------------------------------------------------------------
# Auth header builder
# ---------------------------------------------------------------------------

class TestBuildAuthHeaders:
    def test_includes_token_when_configured(self):
        with patch.object(btm, "BOT_TALK_TOKEN", "mytoken123"):
            headers = btm._build_auth_headers()
        assert headers == {"X-Bot-Token": "mytoken123"}

    def test_returns_empty_dict_when_token_empty(self):
        with patch.object(btm, "BOT_TALK_TOKEN", ""):
            headers = btm._build_auth_headers()
        assert headers == {}

    def test_does_not_include_other_headers(self):
        with patch.object(btm, "BOT_TALK_TOKEN", "tok"):
            headers = btm._build_auth_headers()
        assert set(headers.keys()) == {"X-Bot-Token"}


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

    def test_passes_auth_header_when_token_set(self):
        """Verifies that X-Bot-Token header is included in POST requests."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        with patch.object(btm, "BOT_TALK_HTTP_URL", _FAKE_HTTP_URL), \
             patch.object(btm, "BOT_TALK_TOKEN", "test-token-abc"), \
             patch("bot_talk_mirror.httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.return_value = mock_response
            mock_client_cls.return_value = mock_client

            btm._try_http({"content": "x"})

        _, call_kwargs = mock_client.post.call_args
        assert call_kwargs.get("headers", {}).get("X-Bot-Token") == "test-token-abc"

    def test_no_auth_header_when_token_empty(self):
        """When BOT_TALK_TOKEN is empty, X-Bot-Token header is not included."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        with patch.object(btm, "BOT_TALK_HTTP_URL", _FAKE_HTTP_URL), \
             patch.object(btm, "BOT_TALK_TOKEN", ""), \
             patch("bot_talk_mirror.httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.return_value = mock_response
            mock_client_cls.return_value = mock_client

            btm._try_http({"content": "x"})

        _, call_kwargs = mock_client.post.call_args
        assert "X-Bot-Token" not in call_kwargs.get("headers", {})


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
        assert entry["sender"] == btm.LOBSTER_NAME
        assert entry["genre"] == "status-update"
        assert "test content" in entry["content"]
        assert entry["mirror_failed_reason"] == "http_and_ssh_both_failed"

    def test_does_not_raise_on_write_error(self):
        """_write_local_log must never raise, even if the path is unwritable."""
        with patch.object(btm, "_LOCAL_LOG", Path("/nonexistent/readonly/path/log.log")):
            # Should not raise
            btm._write_local_log("content", "status-update", "reason")


# ---------------------------------------------------------------------------
# _emit_event_bus
# ---------------------------------------------------------------------------

class TestEmitEventBus:
    def test_emits_with_correct_payload(self):
        """_emit_event_bus calls EventBus.emit_sync with correct direction/from/to."""
        captured_events = []

        mock_event_class = MagicMock(side_effect=lambda **kw: kw)
        mock_bus = MagicMock()
        mock_bus.emit_sync.side_effect = lambda e: captured_events.append(e)

        with patch.dict("sys.modules", {
            "event_bus": MagicMock(
                get_event_bus=MagicMock(return_value=mock_bus),
                LobsterEvent=mock_event_class,
            )
        }):
            btm._emit_event_bus("OUTBOUND", "SaharLobster", "AlbertLobster", "hello world")

        assert len(captured_events) == 1
        event_kwargs = captured_events[0]
        assert event_kwargs["event_type"] == "bot_talk.message"
        assert event_kwargs["severity"] == "debug"
        payload = event_kwargs["payload"]
        assert payload["direction"] == "OUTBOUND"
        assert payload["from"] == "SaharLobster"
        assert payload["to"] == "AlbertLobster"
        assert "hello world" in payload["content"]

    def test_does_not_raise_when_event_bus_unavailable(self):
        """_emit_event_bus must not raise when event_bus module is missing."""
        with patch.dict("sys.modules", {"event_bus": None}):
            # Should not raise
            btm._emit_event_bus("INBOUND", "AlbertLobster", "SaharLobster", "test")

    def test_content_truncated_to_500(self):
        """_emit_event_bus truncates content to 500 chars in the payload."""
        captured = []
        mock_event_class = MagicMock(side_effect=lambda **kw: kw)
        mock_bus = MagicMock()
        mock_bus.emit_sync.side_effect = lambda e: captured.append(e)

        with patch.dict("sys.modules", {
            "event_bus": MagicMock(
                get_event_bus=MagicMock(return_value=mock_bus),
                LobsterEvent=mock_event_class,
            )
        }):
            btm._emit_event_bus("INBOUND", "A", "B", "x" * 600)

        payload = captured[0]["payload"]
        assert len(payload["content"]) == 500


# ---------------------------------------------------------------------------
# _route_to_inbox
# ---------------------------------------------------------------------------

class TestRouteToInbox:
    def test_creates_inbox_file(self, tmp_path):
        inbox_dir = tmp_path / "inbox"
        with patch.object(btm, "_INBOX_DIR", inbox_dir):
            btm._route_to_inbox("AlbertLobster", "hello from Albert")

        files = list(inbox_dir.glob("*.json"))
        assert len(files) == 1
        msg = json.loads(files[0].read_text())
        assert msg["source"] == "bot-talk"
        assert msg["type"] == "text"
        assert msg["text"] == "hello from Albert"
        assert msg["direction"] == "INBOUND"
        assert msg["from"] == "AlbertLobster"

    def test_message_has_correct_to_field(self, tmp_path):
        inbox_dir = tmp_path / "inbox"
        with patch.object(btm, "_INBOX_DIR", inbox_dir), \
             patch.object(btm, "LOBSTER_NAME", "SaharLobster"):
            btm._route_to_inbox("AlbertLobster", "content")

        msg = json.loads(list(inbox_dir.glob("*.json"))[0].read_text())
        assert msg["to"] == "SaharLobster"

    def test_message_user_name_is_sender(self, tmp_path):
        inbox_dir = tmp_path / "inbox"
        with patch.object(btm, "_INBOX_DIR", inbox_dir):
            btm._route_to_inbox("AlbertLobster", "hi")

        msg = json.loads(list(inbox_dir.glob("*.json"))[0].read_text())
        assert msg["user_name"] == "AlbertLobster"
        assert msg["chat_id"] == "AlbertLobster"

    def test_does_not_raise_on_io_error(self):
        """_route_to_inbox must not raise even if the inbox dir is unwritable."""
        with patch.object(btm, "_INBOX_DIR", Path("/nonexistent/readonly/inbox")):
            # Should not raise
            btm._route_to_inbox("AlbertLobster", "content")

    def test_atomic_write_uses_tmp_then_rename(self, tmp_path):
        """Verifies that writes go through a .tmp file (atomic rename pattern)."""
        inbox_dir = tmp_path / "inbox"
        written_paths = []

        original_write_text = Path.write_text

        def capturing_write_text(self, *args, **kwargs):
            written_paths.append(str(self))
            return original_write_text(self, *args, **kwargs)

        with patch.object(btm, "_INBOX_DIR", inbox_dir), \
             patch.object(Path, "write_text", capturing_write_text):
            btm._route_to_inbox("AlbertLobster", "content")

        # The tmp file should have been written
        assert any(".tmp" in p for p in written_paths)


# ---------------------------------------------------------------------------
# _do_mirror
# ---------------------------------------------------------------------------

class TestDoMirror:
    def test_http_success_skips_ssh_writes_event_bus(self):
        with patch.object(btm, "_try_http", return_value=True) as mock_http, \
             patch.object(btm, "_try_ssh") as mock_ssh, \
             patch.object(btm, "_write_local_log") as mock_local, \
             patch.object(btm, "_emit_event_bus") as mock_bus:
            btm._do_mirror("content", "status-update", "OUTBOUND", "SaharLobster", "AlbertLobster")

        mock_http.assert_called_once()
        mock_ssh.assert_not_called()
        mock_local.assert_not_called()
        mock_bus.assert_called_once()

    def test_http_failure_falls_back_to_ssh_then_event_bus(self):
        with patch.object(btm, "_try_http", return_value=False), \
             patch.object(btm, "_try_ssh", return_value=True) as mock_ssh, \
             patch.object(btm, "_write_local_log") as mock_local, \
             patch.object(btm, "_emit_event_bus") as mock_bus:
            btm._do_mirror("content", "status-update", "INBOUND", "AlbertLobster", "SaharLobster")

        mock_ssh.assert_called_once()
        mock_local.assert_not_called()
        mock_bus.assert_called_once()

    def test_http_and_ssh_failure_writes_local_then_event_bus(self):
        with patch.object(btm, "_try_http", return_value=False), \
             patch.object(btm, "_try_ssh", return_value=False), \
             patch.object(btm, "_write_local_log") as mock_local, \
             patch.object(btm, "_emit_event_bus") as mock_bus:
            btm._do_mirror("content", "status-update", "OUTBOUND", "A", "B")

        mock_local.assert_called_once()
        mock_bus.assert_called_once()

    def test_event_bus_called_with_correct_direction_fields(self):
        with patch.object(btm, "_try_http", return_value=True), \
             patch.object(btm, "_emit_event_bus") as mock_bus:
            btm._do_mirror("the content", "status-update", "INBOUND", "AlbertLobster", "SaharLobster")

        mock_bus.assert_called_once_with("INBOUND", "AlbertLobster", "SaharLobster", "the content")


# ---------------------------------------------------------------------------
# mirror_outbound
# ---------------------------------------------------------------------------

class TestMirrorOutbound:
    def test_spawns_daemon_thread_with_outbound_direction(self):
        spawned = []

        def capturing_spawn(content, genre, direction, from_, to):
            spawned.append({"content": content, "genre": genre, "direction": direction, "from": from_, "to": to})

        with patch.object(btm, "_spawn_mirror", side_effect=capturing_spawn):
            btm.mirror_outbound("hello world", "bot-talk", "AlbertLobster")

        assert len(spawned) == 1
        call_data = spawned[0]
        assert call_data["direction"] == "OUTBOUND"
        assert call_data["to"] == "AlbertLobster"
        assert call_data["from"] == btm.LOBSTER_NAME
        assert call_data["content"] == "hello world"
        assert call_data["genre"] == "status-update"

    def test_chat_id_integer_converted_to_str_for_to_field(self):
        to_values = []

        def capture_to(content, genre, direction, from_, to):
            to_values.append(to)

        with patch.object(btm, "_spawn_mirror", side_effect=capture_to):
            btm.mirror_outbound("msg", "bot-talk", 9999)

        assert to_values[0] == "9999"


# ---------------------------------------------------------------------------
# log_inbound_cross_lobster
# ---------------------------------------------------------------------------

class TestLogInboundCrossLobster:
    def test_spawns_mirror_with_inbound_direction(self):
        spawned = []

        def capturing_spawn(content, genre, direction, from_, to):
            spawned.append({"content": content, "direction": direction, "from": from_, "to": to})

        with patch.object(btm, "_spawn_mirror", side_effect=capturing_spawn), \
             patch.object(btm, "_route_to_inbox"):
            btm.log_inbound_cross_lobster("AlbertLobster", "hello from Albert")

        assert len(spawned) == 1
        call_data = spawned[0]
        assert call_data["direction"] == "INBOUND"
        assert call_data["from"] == "AlbertLobster"
        assert call_data["to"] == btm.LOBSTER_NAME
        assert call_data["content"] == "hello from Albert"

    def test_routes_to_inbox(self):
        """log_inbound_cross_lobster must call _route_to_inbox."""
        routed = []

        with patch.object(btm, "_spawn_mirror"), \
             patch.object(btm, "_route_to_inbox", side_effect=lambda sender, content: routed.append((sender, content))):
            btm.log_inbound_cross_lobster("AlbertLobster", "some message")

        assert len(routed) == 1
        assert routed[0] == ("AlbertLobster", "some message")

    def test_mirror_and_inbox_both_called(self):
        """Both _spawn_mirror and _route_to_inbox must be called for inbound."""
        mirror_called = []
        inbox_called = []

        with patch.object(btm, "_spawn_mirror", side_effect=lambda *a, **kw: mirror_called.append(True)), \
             patch.object(btm, "_route_to_inbox", side_effect=lambda s, c: inbox_called.append(True)):
            btm.log_inbound_cross_lobster("AlbertLobster", "msg")

        assert len(mirror_called) == 1
        assert len(inbox_called) == 1


# ---------------------------------------------------------------------------
# mirror_inbound removed — verify it does not exist
# ---------------------------------------------------------------------------

class TestMirrorInboundRemoved:
    def test_mirror_inbound_does_not_exist(self):
        """The old mirror_inbound() that incorrectly logged Telegram messages
        as bot-talk entries must be removed."""
        assert not hasattr(btm, "mirror_inbound"), (
            "mirror_inbound() still exists — it was supposed to be removed in #1350. "
            "Owner Telegram messages must never be logged as bot-talk."
        )


# ---------------------------------------------------------------------------
# _spawn_mirror actually starts a daemon thread
# ---------------------------------------------------------------------------

class TestSpawnMirror:
    def test_spawns_daemon_thread(self):
        """_spawn_mirror must start a daemon thread that calls _do_mirror."""
        called = threading.Event()

        def fake_do_mirror(content, genre, direction, from_, to):
            called.set()

        with patch.object(btm, "_do_mirror", side_effect=fake_do_mirror):
            btm._spawn_mirror("test content", "status-update", "OUTBOUND", "A", "B")
            called.wait(timeout=2.0)

        assert called.is_set(), "_do_mirror was not called within 2 seconds"


# ---------------------------------------------------------------------------
# Severity: bot_talk.message events must be emitted at debug (not info)
# ---------------------------------------------------------------------------

class TestEventSeverity:
    def test_emit_event_bus_uses_debug_severity(self):
        """bot_talk.message events must be emitted at debug severity (issue #1425).

        TelegramOutboxListener is gated on LOBSTER_DEBUG=true and only forwards
        warn, error, and debug events. info events are silently dropped.
        Severity must be 'debug' for the existing listener to pick them up.
        """
        captured_events = []

        mock_event_class = MagicMock(side_effect=lambda **kw: kw)
        mock_bus = MagicMock()
        mock_bus.emit_sync.side_effect = lambda e: captured_events.append(e)

        with patch.dict("sys.modules", {
            "event_bus": MagicMock(
                get_event_bus=MagicMock(return_value=mock_bus),
                LobsterEvent=mock_event_class,
            )
        }):
            btm._emit_event_bus("OUTBOUND", "SaharLobster", "AlbertLobster", "test")

        assert len(captured_events) == 1
        assert captured_events[0]["severity"] == "debug", (
            f"Expected severity='debug', got {captured_events[0]['severity']!r}. "
            "TelegramOutboxListener only handles debug/warn/error; info is silently dropped."
        )

    def test_emit_event_bus_does_not_use_info_severity(self):
        """info severity must no longer be used — it bypasses TelegramOutboxListener."""
        captured_events = []

        mock_event_class = MagicMock(side_effect=lambda **kw: kw)
        mock_bus = MagicMock()
        mock_bus.emit_sync.side_effect = lambda e: captured_events.append(e)

        with patch.dict("sys.modules", {
            "event_bus": MagicMock(
                get_event_bus=MagicMock(return_value=mock_bus),
                LobsterEvent=mock_event_class,
            )
        }):
            btm._emit_event_bus("INBOUND", "AlbertLobster", "SaharLobster", "test")

        assert captured_events[0]["severity"] != "info", (
            "Severity must not be 'info' — TelegramOutboxListener drops info events."
        )
