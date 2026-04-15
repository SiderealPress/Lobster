"""
Slack Log Store — read-only query interface over JSONL log files.

Provides structured access to the raw Slack message logs produced
by ingress_logger.py. All functions are pure or read-only — no
mutations to the log files.

Design principles:
- Pure query functions: data in, data out
- Lazy file reading with generators for memory efficiency
- Composable query primitives (filter, date range, channel listing)
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

log = logging.getLogger("slack-log-store")

_DEFAULT_LOG_ROOT = Path(
    os.environ.get(
        "LOBSTER_WORKSPACE", Path.home() / "lobster-workspace"
    )
) / "slack-connector" / "logs"


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _parse_jsonl_lines(path: Path) -> Iterator[dict[str, Any]]:
    """Lazily parse JSONL lines from a file. Skips malformed lines."""
    if not path.exists():
        return
    with open(path, "r") as f:
        for line_num, line in enumerate(f, 1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                yield json.loads(stripped)
            except json.JSONDecodeError:
                log.warning("Malformed JSON at %s:%d, skipping", path, line_num)


def _date_range(start_date: str, end_date: str) -> list[str]:
    """Generate a list of YYYY-MM-DD strings from start_date to end_date inclusive.

    Pure function.
    """
    start = datetime.strptime(start_date, "%Y-%m-%d")
    end = datetime.strptime(end_date, "%Y-%m-%d")
    if end < start:
        return []
    days = (end - start).days + 1
    return [
        (start + timedelta(days=i)).strftime("%Y-%m-%d")
        for i in range(days)
    ]


def _channel_log_dir(log_root: Path, channel_id: str) -> Path:
    """Determine the log directory for a channel.

    Checks both channels/ and dms/ subdirectories.
    """
    channels_path = log_root / "channels" / channel_id
    if channels_path.exists():
        return channels_path
    dms_path = log_root / "dms" / channel_id
    if dms_path.exists():
        return dms_path
    # Default to channels/ if neither exists
    return channels_path


# ---------------------------------------------------------------------------
# SlackLogStore
# ---------------------------------------------------------------------------


class SlackLogStore:
    """Read-only query interface over the Slack JSONL log store.

    All methods are either pure or perform read-only file I/O.
    No mutations to log files.
    """

    def __init__(self, log_root: Optional[Path] = None) -> None:
        self._log_root = log_root or _DEFAULT_LOG_ROOT

    def query(self, channel_id: str, date: str) -> list[dict[str, Any]]:
        """Read all log records for a channel on a specific date.

        Args:
            channel_id: Slack channel ID (e.g., "C01ABC123")
            date: Date string in YYYY-MM-DD format

        Returns:
            List of parsed JSONL records, ordered by file position.
        """
        log_dir = _channel_log_dir(self._log_root, channel_id)
        path = log_dir / f"{date}.jsonl"
        return list(_parse_jsonl_lines(path))

    def query_range(
        self, channel_id: str, start_date: str, end_date: str
    ) -> list[dict[str, Any]]:
        """Read all log records for a channel across a date range (inclusive).

        Args:
            channel_id: Slack channel ID
            start_date: Start date in YYYY-MM-DD format
            end_date: End date in YYYY-MM-DD format

        Returns:
            List of parsed JSONL records across all dates in range.
        """
        dates = _date_range(start_date, end_date)
        return [
            record
            for date in dates
            for record in self.query(channel_id, date)
        ]

    def list_channels(self) -> list[str]:
        """List all channel IDs that have log files.

        Scans both channels/ and dms/ directories.

        Returns:
            Sorted list of channel/DM IDs.
        """
        channels: set[str] = set()

        for category in ("channels", "dms"):
            category_dir = self._log_root / category
            if not category_dir.exists():
                continue
            channels.update(
                entry.name
                for entry in category_dir.iterdir()
                if entry.is_dir()
            )

        return sorted(channels)

    def list_dates(self, channel_id: str) -> list[str]:
        """List all dates that have log files for a channel.

        Returns:
            Sorted list of date strings (YYYY-MM-DD).
        """
        log_dir = _channel_log_dir(self._log_root, channel_id)
        if not log_dir.exists():
            return []

        return sorted(
            path.stem
            for path in log_dir.iterdir()
            if path.suffix == ".jsonl" and path.is_file()
        )

    def query_iter(
        self, channel_id: str, date: str
    ) -> Iterator[dict[str, Any]]:
        """Lazily iterate over log records for a channel on a specific date.

        Memory-efficient alternative to query() for large files.
        """
        log_dir = _channel_log_dir(self._log_root, channel_id)
        path = log_dir / f"{date}.jsonl"
        yield from _parse_jsonl_lines(path)
