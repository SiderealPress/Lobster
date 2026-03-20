"""
Shared structured logging utilities for Lobster MCP servers.

Provides a JSON-lines formatter and a factory function for attaching
RotatingFileHandlers to named loggers. All Python servers should call
``configure_file_handler`` during startup so that log output lands in
~/lobster-workspace/logs/ as structured JSON that can be parsed by
standard log aggregators (jq, Loki, filebeat, etc.).

Format (one JSON object per line)::

    {"ts": "2026-03-19T21:54:29.488Z", "level": "INFO",
     "component": "inbox_server", "msg": "Message claimed",
     "message_id": "1773957246474_13217"}

Required fields: ``ts``, ``level``, ``component``, ``msg``.
Optional contextual fields forwarded from ``LogRecord.extra`` when present:
``message_id``, ``task_id``, ``chat_id``, ``source``, ``duration_ms``.
"""

import json
import logging
import os
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Default log directory — overridable via env var for tests
# ---------------------------------------------------------------------------
_WORKSPACE = Path(os.environ.get("LOBSTER_WORKSPACE", Path.home() / "lobster-workspace"))
_DEFAULT_LOG_DIR = _WORKSPACE / "logs"

# Optional contextual fields that are forwarded when present on a LogRecord.
# Code that wants to attach them should use logger.info("...", extra={...}).
_OPTIONAL_FIELDS = ("message_id", "task_id", "chat_id", "source", "duration_ms")


class JsonFormatter(logging.Formatter):
    """Format each log record as a single-line JSON object.

    The formatter is intentionally minimal: it reads only from the
    ``LogRecord`` and never performs I/O, making it safe to use as a
    pure function in tests.

    Args:
        component: Value written to the ``component`` field.  Typically
            the server's module name (e.g. ``"inbox_server"``).
    """

    def __init__(self, component: str) -> None:
        super().__init__()
        self._component = component

    def format(self, record: logging.LogRecord) -> str:  # type: ignore[override]
        ts = datetime.fromtimestamp(record.created, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.%f"
        )[:-3] + "Z"

        entry: dict[str, Any] = {
            "ts": ts,
            "level": record.levelname,
            "component": self._component,
            "msg": record.getMessage(),
        }

        # Forward optional contextual fields when attached via extra={...}
        for field in _OPTIONAL_FIELDS:
            value = getattr(record, field, None)
            if value is not None:
                entry[field] = value

        # Attach exception info when present
        if record.exc_info:
            entry["exc_info"] = self.formatException(record.exc_info)

        return json.dumps(entry, ensure_ascii=False)


def configure_file_handler(
    logger: logging.Logger,
    component: str,
    log_dir: Path | None = None,
    filename: str | None = None,
    max_bytes: int = 5 * 1024 * 1024,
    backup_count: int = 3,
) -> RotatingFileHandler:
    """Attach a JSON-formatted RotatingFileHandler to *logger*.

    Idempotent: if a RotatingFileHandler is already attached, returns it
    without adding a second one.  This prevents duplicate log entries when
    the function is called more than once (e.g. in tests that import the
    module multiple times).

    Args:
        logger: The logger instance to configure.
        component: Written to the ``component`` JSON field on every record.
        log_dir: Directory for the log file.  Defaults to
            ``~/lobster-workspace/logs/``.
        filename: Name of the log file.  Defaults to ``{component}.log``.
        max_bytes: Rotate when the file reaches this size (default 5 MB).
        backup_count: Number of rotated backups to keep (default 3).

    Returns:
        The (possibly pre-existing) RotatingFileHandler attached to *logger*.
    """
    # Check idempotency
    for handler in logger.handlers:
        if isinstance(handler, RotatingFileHandler):
            return handler

    resolved_log_dir = log_dir if log_dir is not None else _DEFAULT_LOG_DIR
    resolved_log_dir.mkdir(parents=True, exist_ok=True)

    log_file = resolved_log_dir / (filename or f"{component}.log")
    handler = RotatingFileHandler(log_file, maxBytes=max_bytes, backupCount=backup_count)
    handler.setFormatter(JsonFormatter(component))
    logger.addHandler(handler)
    return handler
