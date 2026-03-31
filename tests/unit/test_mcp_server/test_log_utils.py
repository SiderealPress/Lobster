"""
Unit tests for src/mcp/log_utils — JsonFormatter and configure_file_handler.
"""

import json
import logging
import re
import sys
from pathlib import Path
from logging.handlers import RotatingFileHandler

import pytest

# Ensure the mcp package is importable.
_MCP_SRC = str(Path(__file__).resolve().parents[3] / "src" / "mcp")
if _MCP_SRC not in sys.path:
    sys.path.insert(0, _MCP_SRC)

from log_utils import GzipRotatingFileHandler, JsonFormatter, configure_file_handler


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_record(
    msg: str = "hello",
    level: int = logging.INFO,
    name: str = "test.logger",
    extra: dict | None = None,
) -> logging.LogRecord:
    """Construct a minimal LogRecord for testing."""
    record = logging.LogRecord(
        name=name,
        level=level,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=(),
        exc_info=None,
    )
    if extra:
        for k, v in extra.items():
            setattr(record, k, v)
    return record


_ISO8601_Z_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


# ---------------------------------------------------------------------------
# JsonFormatter
# ---------------------------------------------------------------------------

class TestJsonFormatter:
    def test_output_is_valid_json(self):
        fmt = JsonFormatter("my_component")
        record = _make_record()
        line = fmt.format(record)
        parsed = json.loads(line)  # must not raise
        assert isinstance(parsed, dict)

    def test_required_fields_present(self):
        fmt = JsonFormatter("my_component")
        parsed = json.loads(fmt.format(_make_record()))
        assert set(parsed.keys()) >= {"ts", "level", "component", "msg"}

    def test_component_field_matches_constructor_arg(self):
        fmt = JsonFormatter("inbox_server")
        parsed = json.loads(fmt.format(_make_record()))
        assert parsed["component"] == "inbox_server"

    def test_level_name_is_string(self):
        for level in (logging.DEBUG, logging.INFO, logging.WARNING, logging.ERROR):
            fmt = JsonFormatter("c")
            record = _make_record(level=level)
            parsed = json.loads(fmt.format(record))
            assert parsed["level"] == logging.getLevelName(level)

    def test_ts_is_iso8601_utc(self):
        fmt = JsonFormatter("c")
        parsed = json.loads(fmt.format(_make_record()))
        assert _ISO8601_Z_RE.match(parsed["ts"]), f"Unexpected ts format: {parsed['ts']}"

    def test_msg_matches_record_message(self):
        fmt = JsonFormatter("c")
        parsed = json.loads(fmt.format(_make_record(msg="test message")))
        assert parsed["msg"] == "test message"

    def test_optional_fields_forwarded_when_present(self):
        """Fields set via extra={} on the LogRecord are included in the JSON."""
        fmt = JsonFormatter("c")
        extra = {"message_id": "msg_123", "task_id": "task_456", "chat_id": "789"}
        parsed = json.loads(fmt.format(_make_record(extra=extra)))
        assert parsed["message_id"] == "msg_123"
        assert parsed["task_id"] == "task_456"
        assert parsed["chat_id"] == "789"

    def test_optional_fields_absent_when_not_set(self):
        """Optional fields must not appear when not set (no null noise)."""
        fmt = JsonFormatter("c")
        parsed = json.loads(fmt.format(_make_record()))
        for field in ("message_id", "task_id", "chat_id", "source", "duration_ms"):
            assert field not in parsed, f"Unexpected field {field!r} in output"

    def test_exc_info_included_when_present(self):
        fmt = JsonFormatter("c")
        try:
            raise ValueError("boom")
        except ValueError:
            import sys as _sys
            exc_info = _sys.exc_info()
        record = logging.LogRecord(
            name="t", level=logging.ERROR,
            pathname=__file__, lineno=1,
            msg="error", args=(), exc_info=exc_info,
        )
        parsed = json.loads(fmt.format(record))
        assert "exc_info" in parsed
        assert "ValueError" in parsed["exc_info"]

    def test_no_newline_in_output(self):
        """Each record must be a single line — no embedded newlines."""
        fmt = JsonFormatter("c")
        line = fmt.format(_make_record())
        assert "\n" not in line

    def test_msg_with_format_args(self):
        """LogRecord with args should have them interpolated into msg."""
        fmt = JsonFormatter("c")
        record = logging.LogRecord(
            name="t", level=logging.INFO,
            pathname=__file__, lineno=1,
            msg="count=%d name=%s", args=(42, "foo"), exc_info=None,
        )
        parsed = json.loads(fmt.format(record))
        assert parsed["msg"] == "count=42 name=foo"


# ---------------------------------------------------------------------------
# configure_file_handler
# ---------------------------------------------------------------------------

class TestConfigureFileHandler:
    def test_adds_rotating_file_handler(self, tmp_path):
        logger = logging.getLogger(f"test.cfg.{tmp_path.name}")
        logger.handlers.clear()
        handler = configure_file_handler(logger, component="srv", log_dir=tmp_path)
        assert isinstance(handler, GzipRotatingFileHandler)
        assert any(isinstance(h, GzipRotatingFileHandler) for h in logger.handlers)

    def test_log_file_created(self, tmp_path):
        logger = logging.getLogger(f"test.file.{tmp_path.name}")
        logger.handlers.clear()
        logger.setLevel(logging.INFO)
        configure_file_handler(logger, component="srv", log_dir=tmp_path)
        logger.info("hello from test")
        log_file = tmp_path / "srv.log"
        assert log_file.exists()

    def test_log_file_contains_json(self, tmp_path):
        logger = logging.getLogger(f"test.json.{tmp_path.name}")
        logger.handlers.clear()
        logger.setLevel(logging.INFO)
        configure_file_handler(logger, component="srv", log_dir=tmp_path)
        logger.info("structured test")
        log_file = tmp_path / "srv.log"
        line = log_file.read_text().strip()
        parsed = json.loads(line)
        assert parsed["msg"] == "structured test"
        assert parsed["component"] == "srv"

    def test_idempotent_does_not_add_duplicate_handler(self, tmp_path):
        logger = logging.getLogger(f"test.idem.{tmp_path.name}")
        logger.handlers.clear()
        configure_file_handler(logger, component="srv", log_dir=tmp_path)
        configure_file_handler(logger, component="srv", log_dir=tmp_path)
        rotating_handlers = [h for h in logger.handlers if isinstance(h, GzipRotatingFileHandler)]
        assert len(rotating_handlers) == 1

    def test_custom_filename(self, tmp_path):
        logger = logging.getLogger(f"test.fname.{tmp_path.name}")
        logger.handlers.clear()
        configure_file_handler(
            logger, component="srv", log_dir=tmp_path, filename="mcp-server.log"
        )
        logger.info("custom filename test")
        assert (tmp_path / "mcp-server.log").exists()

    def test_log_dir_created_if_missing(self, tmp_path):
        nested = tmp_path / "deep" / "logs"
        assert not nested.exists()
        logger = logging.getLogger(f"test.mkdir.{tmp_path.name}")
        logger.handlers.clear()
        configure_file_handler(logger, component="srv", log_dir=nested)
        assert nested.exists()

    def test_returns_existing_handler_on_second_call(self, tmp_path):
        logger = logging.getLogger(f"test.ret.{tmp_path.name}")
        logger.handlers.clear()
        h1 = configure_file_handler(logger, component="srv", log_dir=tmp_path)
        h2 = configure_file_handler(logger, component="srv", log_dir=tmp_path)
        assert h1 is h2
