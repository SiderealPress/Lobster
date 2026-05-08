"""
Unit tests for _is_compact_event() in hooks/on-compact.py (issues #2009, #2010).

Three-tier detection:
  1. source == "compact"  (primary, CC-documented)
  2. hook_name == "compact"  (fallback when source absent)
  3. Filesystem fallback (WFM_ACTIVE_FILE contains a digit-only timestamp)
     when both source and hook_name are absent from the payload

Constants:
  WFM_ACTIVE_FILE — path override via LOBSTER_WFM_ACTIVE_OVERRIDE env var

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
    - both absent + WFM_ACTIVE_FILE contains digit-only timestamp → True
    - both absent + WFM_ACTIVE_FILE contains "exited" → False
    - both absent + WFM_ACTIVE_FILE absent → False
    - both absent + WFM_ACTIVE_FILE contains non-digit, non-"exited" content → False

  Edge cases:
    - empty dict (no fields) + WFM file absent → False
    - source="compact" wins over any WFM file content
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


def _load_on_compact(wfm_active_override: str | None = None) -> object:
    """Load on-compact.py with LOBSTER_WFM_ACTIVE_OVERRIDE set to an isolated path."""
    import uuid as _uuid

    env: dict = {}
    if wfm_active_override is not None:
        env["LOBSTER_WFM_ACTIVE_OVERRIDE"] = wfm_active_override

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
        wfm_file = tmp_path / "dispatcher-wfm-active"
        mod = _load_on_compact(wfm_active_override=str(wfm_file))
        assert mod._is_compact_event({"source": "compact"}) is True

    def test_source_startup_returns_false(self, tmp_path):
        """source='startup' must return False."""
        wfm_file = tmp_path / "dispatcher-wfm-active"
        mod = _load_on_compact(wfm_active_override=str(wfm_file))
        assert mod._is_compact_event({"source": "startup"}) is False

    def test_source_resume_returns_false(self, tmp_path):
        """source='resume' must return False."""
        wfm_file = tmp_path / "dispatcher-wfm-active"
        mod = _load_on_compact(wfm_active_override=str(wfm_file))
        assert mod._is_compact_event({"source": "resume"}) is False

    def test_source_clear_returns_false(self, tmp_path):
        """source='clear' must return False."""
        wfm_file = tmp_path / "dispatcher-wfm-active"
        mod = _load_on_compact(wfm_active_override=str(wfm_file))
        assert mod._is_compact_event({"source": "clear"}) is False

    def test_source_non_compact_overrides_hook_name_compact(self, tmp_path):
        """source='startup' must return False even when hook_name='compact'."""
        wfm_file = tmp_path / "dispatcher-wfm-active"
        mod = _load_on_compact(wfm_active_override=str(wfm_file))
        assert mod._is_compact_event({"source": "startup", "hook_name": "compact"}) is False

    def test_source_compact_with_other_fields(self, tmp_path):
        """Real CC payloads include extra fields — source='compact' still returns True."""
        wfm_file = tmp_path / "dispatcher-wfm-active"
        mod = _load_on_compact(wfm_active_override=str(wfm_file))
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
        wfm_file = tmp_path / "dispatcher-wfm-active"
        mod = _load_on_compact(wfm_active_override=str(wfm_file))
        assert mod._is_compact_event({"hook_name": "compact"}) is True

    def test_hook_name_startup_returns_false(self, tmp_path):
        """hook_name='startup' (no source) must return False."""
        wfm_file = tmp_path / "dispatcher-wfm-active"
        mod = _load_on_compact(wfm_active_override=str(wfm_file))
        assert mod._is_compact_event({"hook_name": "startup"}) is False

    def test_hook_name_non_compact_returns_false(self, tmp_path):
        """hook_name with any non-compact value must return False."""
        wfm_file = tmp_path / "dispatcher-wfm-active"
        mod = _load_on_compact(wfm_active_override=str(wfm_file))
        assert mod._is_compact_event({"hook_name": "resume"}) is False


# -----------------------------------------------------------------------
# Tier 3: filesystem fallback (both source and hook_name absent)
# -----------------------------------------------------------------------

# Named constant matching the spec requirement from issue #2009.
# "both absent + WFM file contains a digit-only timestamp → treat as compaction"
_WFM_ACTIVE_DIGIT_MEANS_COMPACT = True


class TestIsCompactEventFilesystemFallback:
    """When both source and hook_name are absent, fall back to WFM_ACTIVE_FILE."""

    def test_both_absent_wfm_has_digit_timestamp_returns_true(self, tmp_path):
        """Both fields absent + WFM active file has digit-only timestamp → True (compaction).

        A digit-only Unix timestamp means the dispatcher was blocking in WFM
        immediately before this session started — strong signal of compaction.
        """
        wfm_file = tmp_path / "dispatcher-wfm-active"
        wfm_file.write_text(str(int(time.time())) + "\n")

        mod = _load_on_compact(wfm_active_override=str(wfm_file))
        result = mod._is_compact_event({})

        assert result is _WFM_ACTIVE_DIGIT_MEANS_COMPACT, (
            "Both payload fields absent + WFM file has active timestamp must return True"
        )

    def test_both_absent_wfm_has_exited_tombstone_returns_false(self, tmp_path):
        """Both fields absent + WFM file contains 'exited' tombstone → False (not compaction).

        The string 'exited' is written by _clear_wfm_active_signal() when WFM
        exits cleanly.  It means the dispatcher was NOT in WFM when this session
        started — so this is not a WFM-interrupted compaction.
        """
        wfm_file = tmp_path / "dispatcher-wfm-active"
        wfm_file.write_text("exited\n")

        mod = _load_on_compact(wfm_active_override=str(wfm_file))
        result = mod._is_compact_event({})

        assert result is False, (
            "Both fields absent + WFM file says 'exited' must return False"
        )

    def test_both_absent_wfm_file_absent_returns_false(self, tmp_path):
        """Both fields absent + WFM file does not exist → False (not compaction).

        No WFM signal means the file was never written or has been cleaned up.
        Cannot infer compaction — default to False.
        """
        wfm_file = tmp_path / "dispatcher-wfm-active"
        # File does NOT exist

        mod = _load_on_compact(wfm_active_override=str(wfm_file))
        result = mod._is_compact_event({})

        assert result is False, (
            "Both fields absent + WFM file absent must return False"
        )

    def test_both_absent_wfm_has_non_digit_content_returns_false(self, tmp_path):
        """Both fields absent + WFM file has non-digit, non-exited content → False."""
        wfm_file = tmp_path / "dispatcher-wfm-active"
        wfm_file.write_text("corrupted-content\n")

        mod = _load_on_compact(wfm_active_override=str(wfm_file))
        result = mod._is_compact_event({})

        assert result is False, (
            "Both fields absent + non-digit WFM file content must return False"
        )

    def test_source_compact_ignores_wfm_file(self, tmp_path):
        """source='compact' returns True regardless of WFM file state."""
        wfm_file = tmp_path / "dispatcher-wfm-active"
        # WFM file absent — would be False for tier-3 fallback
        # But source='compact' is tier-1 and must win.
        mod = _load_on_compact(wfm_active_override=str(wfm_file))
        assert mod._is_compact_event({"source": "compact"}) is True

    def test_hook_name_absent_wfm_exited_not_compact(self, tmp_path):
        """hook_name absent + WFM 'exited' must return False (no compaction signal at all)."""
        wfm_file = tmp_path / "dispatcher-wfm-active"
        wfm_file.write_text("exited\n")

        mod = _load_on_compact(wfm_active_override=str(wfm_file))
        result = mod._is_compact_event({"session_id": "abc123"})  # no source, no hook_name

        assert result is False

    def test_empty_dict_wfm_absent_returns_false(self, tmp_path):
        """Empty payload with no WFM file must return False (no compaction signals)."""
        wfm_file = tmp_path / "dispatcher-wfm-active"
        # File does NOT exist

        mod = _load_on_compact(wfm_active_override=str(wfm_file))
        assert mod._is_compact_event({}) is False
