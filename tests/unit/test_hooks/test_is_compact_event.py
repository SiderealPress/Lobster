"""
Unit tests for _is_compact_event() in hooks/on-compact.py (issues #2009, #2010).

Three-tier detection:
  1. source == "compact"  (primary, CC-documented)
  2. hook_name == "compact"  (fallback when source absent)
  3. Filesystem fallback (DISPATCHER_HEARTBEAT_FILE contains a recent Unix epoch
     timestamp) when both source and hook_name are absent from the payload

Constants:
  DISPATCHER_HEARTBEAT_FILE — path override via LOBSTER_DISPATCHER_HEARTBEAT_OVERRIDE env var
  DISPATCHER_WFM_RECENCY_SECONDS — max age (15 min) for heartbeat to be considered "recent"

Behaviors tested:
  Tier 1 (source field):
    - source="compact" → True
    - source="startup" → False (non-compact source)
    - source="resume" → False
    - source="clear" → False
    - source present and non-compact → False even when hook_name="compact"

  Tier 2 (hook_name field, source absent):
    - hook_name="compact" (no source) → True
    - hook_name="startup" (no source) → False

  Tier 3 (filesystem fallback, both absent):
    - both absent + heartbeat file contains recent epoch timestamp → True
    - both absent + heartbeat file contains stale epoch timestamp → False
    - both absent + heartbeat file absent → False
    - both absent + heartbeat file contains non-digit content → False

  Edge cases:
    - empty dict (no fields) + heartbeat file absent → False
    - source="compact" wins over any heartbeat file state
"""

from __future__ import annotations

import importlib.util
import os
import time
from pathlib import Path

import pytest

_HOOKS_DIR = Path(__file__).parents[3] / "hooks"
_HOOK_PATH = _HOOKS_DIR / "on-compact.py"


class _PatchEnv:
    """Context manager to temporarily set/unset environment variables."""

    def __init__(self, env: dict):
        self._env = env
        self._saved: dict = {}

    def __enter__(self):
        for k, v in self._env.items():
            self._saved[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return self

    def __exit__(self, *_):
        for k, saved_v in self._saved.items():
            if saved_v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = saved_v


def _load_on_compact(heartbeat_override: str | None = None) -> object:
    """Load on-compact.py with LOBSTER_DISPATCHER_HEARTBEAT_OVERRIDE set to an isolated path."""
    import uuid as _uuid

    env: dict = {}
    if heartbeat_override is not None:
        env["LOBSTER_DISPATCHER_HEARTBEAT_OVERRIDE"] = heartbeat_override

    unique_name = f"on_compact_{_uuid.uuid4().hex}"
    with _PatchEnv(env):
        spec = importlib.util.spec_from_file_location(unique_name, _HOOK_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    return mod


# -----------------------------------------------------------------------
# Tier 1: source field (primary)
# -----------------------------------------------------------------------


class TestIsCompactEventSourceField:
    """source field takes priority over all other signals."""

    def test_source_compact_returns_true(self, tmp_path):
        """source='compact' must return True."""
        hb_file = tmp_path / "dispatcher-heartbeat"
        mod = _load_on_compact(heartbeat_override=str(hb_file))
        assert mod._is_compact_event({"source": "compact"}) is True

    def test_source_startup_returns_false(self, tmp_path):
        """source='startup' must return False."""
        hb_file = tmp_path / "dispatcher-heartbeat"
        mod = _load_on_compact(heartbeat_override=str(hb_file))
        assert mod._is_compact_event({"source": "startup"}) is False

    def test_source_resume_returns_false(self, tmp_path):
        """source='resume' must return False."""
        hb_file = tmp_path / "dispatcher-heartbeat"
        mod = _load_on_compact(heartbeat_override=str(hb_file))
        assert mod._is_compact_event({"source": "resume"}) is False

    def test_source_clear_returns_false(self, tmp_path):
        """source='clear' must return False."""
        hb_file = tmp_path / "dispatcher-heartbeat"
        mod = _load_on_compact(heartbeat_override=str(hb_file))
        assert mod._is_compact_event({"source": "clear"}) is False

    def test_source_non_compact_overrides_hook_name_compact(self, tmp_path):
        """source='startup' must return False even when hook_name='compact'."""
        hb_file = tmp_path / "dispatcher-heartbeat"
        mod = _load_on_compact(heartbeat_override=str(hb_file))
        assert mod._is_compact_event({"source": "startup", "hook_name": "compact"}) is False

    def test_source_compact_with_other_fields(self, tmp_path):
        """Real CC payloads include extra fields — source='compact' still returns True."""
        hb_file = tmp_path / "dispatcher-heartbeat"
        mod = _load_on_compact(heartbeat_override=str(hb_file))
        payload = {
            "source": "compact",
            "session_id": "abc123",
            "transcript_path": "/home/lobster/.claude/projects/foo/bar.jsonl",
        }
        assert mod._is_compact_event(payload) is True


# -----------------------------------------------------------------------
# Tier 2: hook_name fallback (source absent)
# -----------------------------------------------------------------------


class TestIsCompactEventHookNameFallback:
    """hook_name is used when source is absent; ignored when source is present."""

    def test_hook_name_compact_returns_true(self, tmp_path):
        """hook_name='compact' (no source) must return True."""
        hb_file = tmp_path / "dispatcher-heartbeat"
        mod = _load_on_compact(heartbeat_override=str(hb_file))
        assert mod._is_compact_event({"hook_name": "compact"}) is True

    def test_hook_name_startup_returns_false(self, tmp_path):
        """hook_name='startup' (no source) must return False."""
        hb_file = tmp_path / "dispatcher-heartbeat"
        mod = _load_on_compact(heartbeat_override=str(hb_file))
        assert mod._is_compact_event({"hook_name": "startup"}) is False

    def test_hook_name_non_compact_returns_false(self, tmp_path):
        """hook_name with any non-compact value must return False."""
        hb_file = tmp_path / "dispatcher-heartbeat"
        mod = _load_on_compact(heartbeat_override=str(hb_file))
        assert mod._is_compact_event({"hook_name": "resume"}) is False


# -----------------------------------------------------------------------
# Tier 3: filesystem fallback (both source and hook_name absent)
# -----------------------------------------------------------------------

# Named constant matching the spec requirement from issue #2009.
# "both absent + heartbeat file contains a recent timestamp → treat as compaction"
_RECENT_HEARTBEAT_MEANS_COMPACT = True

# Threshold used by _wfm_was_active() — imported from the module under test
# after loading to avoid hardcoding it here (changes propagate automatically).
# 900 seconds = 15 minutes.
_DISPATCHER_WFM_RECENCY_SECONDS = 900


class TestIsCompactEventFilesystemFallback:
    """When both source and hook_name are absent, fall back to DISPATCHER_HEARTBEAT_FILE."""

    def test_both_absent_heartbeat_recent_returns_true(self, tmp_path):
        """Both fields absent + heartbeat file has recent epoch timestamp → True (compaction).

        A recent Unix epoch integer means the dispatcher was running within the
        last DISPATCHER_WFM_RECENCY_SECONDS (15 min) — strong signal of compaction.
        """
        hb_file = tmp_path / "dispatcher-heartbeat"
        hb_file.write_text(str(int(time.time())) + "\n")

        mod = _load_on_compact(heartbeat_override=str(hb_file))
        result = mod._is_compact_event({})

        assert result is _RECENT_HEARTBEAT_MEANS_COMPACT, (
            "Both payload fields absent + heartbeat file has recent timestamp must return True"
        )

    def test_both_absent_heartbeat_stale_returns_false(self, tmp_path):
        """Both fields absent + heartbeat timestamp older than DISPATCHER_WFM_RECENCY_SECONDS → False.

        A stale heartbeat means the dispatcher has not been active recently — this
        is likely a genuine fresh start after a long idle period, not a compaction.
        """
        hb_file = tmp_path / "dispatcher-heartbeat"
        stale_ts = int(time.time()) - _DISPATCHER_WFM_RECENCY_SECONDS - 60  # 1 min past threshold
        hb_file.write_text(str(stale_ts) + "\n")

        mod = _load_on_compact(heartbeat_override=str(hb_file))
        result = mod._is_compact_event({})

        assert result is False, (
            "Both fields absent + stale heartbeat must return False (fresh start, not compaction)"
        )

    def test_both_absent_heartbeat_file_absent_returns_false(self, tmp_path):
        """Both fields absent + heartbeat file does not exist → False (not compaction).

        No heartbeat file means the dispatcher has never written a heartbeat or
        this is a fresh install.  Cannot infer compaction — default to False.
        """
        hb_file = tmp_path / "dispatcher-heartbeat"
        # File does NOT exist

        mod = _load_on_compact(heartbeat_override=str(hb_file))
        result = mod._is_compact_event({})

        assert result is False, (
            "Both fields absent + heartbeat file absent must return False"
        )

    def test_both_absent_heartbeat_non_digit_content_returns_false(self, tmp_path):
        """Both fields absent + heartbeat file has non-integer content → False."""
        hb_file = tmp_path / "dispatcher-heartbeat"
        hb_file.write_text("corrupted-content\n")

        mod = _load_on_compact(heartbeat_override=str(hb_file))
        result = mod._is_compact_event({})

        assert result is False, (
            "Both fields absent + non-digit heartbeat content must return False"
        )

    def test_source_compact_ignores_heartbeat_file(self, tmp_path):
        """source='compact' returns True regardless of heartbeat file state."""
        hb_file = tmp_path / "dispatcher-heartbeat"
        # Heartbeat file absent — would be False for tier-3 fallback
        # But source='compact' is tier-1 and must win.
        mod = _load_on_compact(heartbeat_override=str(hb_file))
        assert mod._is_compact_event({"source": "compact"}) is True

    def test_hook_name_absent_heartbeat_stale_not_compact(self, tmp_path):
        """hook_name absent + stale heartbeat must return False (no compaction signal)."""
        hb_file = tmp_path / "dispatcher-heartbeat"
        stale_ts = int(time.time()) - _DISPATCHER_WFM_RECENCY_SECONDS - 60
        hb_file.write_text(str(stale_ts) + "\n")

        mod = _load_on_compact(heartbeat_override=str(hb_file))
        result = mod._is_compact_event({"session_id": "abc123"})  # no source, no hook_name

        assert result is False

    def test_empty_dict_heartbeat_absent_returns_false(self, tmp_path):
        """Empty payload with no heartbeat file must return False (no compaction signals)."""
        hb_file = tmp_path / "dispatcher-heartbeat"
        # File does NOT exist

        mod = _load_on_compact(heartbeat_override=str(hb_file))
        assert mod._is_compact_event({}) is False
