"""
Tests for src/integrations/fireflies/sync.py

Covers:
1. _merge_transcripts_deduplicated — pure merge/dedup across accounts
2. _fetch_transcripts_with_detail — per-account API key routing (regression
   test for the Granola bug where the wrong account's key could be used)
3. run_sync — end-to-end orchestration with mocked client/vault_writer calls
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_SRC_DIR = Path(__file__).parent.parent.parent / "src"
sys.path.insert(0, str(_SRC_DIR))

from integrations.fireflies.client import (  # noqa: E402
    ACCOUNT_PRIMARY,
    FirefliesAccountConfig,
    FirefliesAPIError,
    FirefliesAuthError,
    FirefliesTranscript,
    FirefliesUnknownAccountError,
)
from integrations.fireflies.sync import (  # noqa: E402
    _fetch_transcripts_with_detail,
    _merge_transcripts_deduplicated,
    run_sync,
)


def _make_transcript(tid: str = "t1", title: str = "Call", account: str = ACCOUNT_PRIMARY) -> FirefliesTranscript:
    return FirefliesTranscript(
        id=tid,
        title=title,
        date=datetime(2026, 6, 1, tzinfo=timezone.utc),
        fireflies_account=account,
    )


# ---------------------------------------------------------------------------
# _merge_transcripts_deduplicated
# ---------------------------------------------------------------------------


class TestMergeTranscriptsDeduplicated:
    def test_non_overlapping_all_kept(self):
        result = _merge_transcripts_deduplicated({
            ACCOUNT_PRIMARY: [_make_transcript("a"), _make_transcript("b")],
            "jake": [_make_transcript("c", account="jake")],
        })
        assert {t.id for t in result} == {"a", "b", "c"}

    def test_primary_wins_on_duplicate_id(self):
        shared_id = "shared"
        primary_t = _make_transcript(shared_id, title="primary version", account=ACCOUNT_PRIMARY)
        jake_t = _make_transcript(shared_id, title="jake version", account="jake")
        result = _merge_transcripts_deduplicated({
            ACCOUNT_PRIMARY: [primary_t],
            "jake": [jake_t],
        })
        assert len(result) == 1
        assert result[0].title == "primary version"

    def test_empty_accounts_returns_empty(self):
        assert _merge_transcripts_deduplicated({}) == []

    def test_account_field_preserved(self):
        result = _merge_transcripts_deduplicated({
            ACCOUNT_PRIMARY: [_make_transcript("a", account=ACCOUNT_PRIMARY)],
            "ben": [_make_transcript("b", account="ben")],
        })
        by_id = {t.id: t for t in result}
        assert by_id["a"].fireflies_account == ACCOUNT_PRIMARY
        assert by_id["b"].fireflies_account == "ben"

    def test_many_named_accounts_all_merged(self):
        """
        Unlike Granola's merge (hardcoded to 'primary'/'secondary'), this must
        handle an arbitrary number of named accounts since Fireflies account
        discovery is dynamic.
        """
        result = _merge_transcripts_deduplicated({
            ACCOUNT_PRIMARY: [_make_transcript("a")],
            "jake": [_make_transcript("b", account="jake")],
            "ben": [_make_transcript("c", account="ben")],
            "priya": [_make_transcript("d", account="priya")],
        })
        assert {t.id for t in result} == {"a", "b", "c", "d"}


# ---------------------------------------------------------------------------
# _fetch_transcripts_with_detail — per-account API key routing
# ---------------------------------------------------------------------------

PRIMARY_KEY = "ff_primary_key"
JAKE_KEY = "ff_jake_key"


class TestFetchTranscriptsWithDetail:
    def _accounts(self) -> list[FirefliesAccountConfig]:
        return [
            FirefliesAccountConfig(name=ACCOUNT_PRIMARY, api_key=PRIMARY_KEY),
            FirefliesAccountConfig(name="jake", api_key=JAKE_KEY),
        ]

    @patch("integrations.fireflies.sync.get_transcript")
    def test_primary_account_uses_primary_key(self, mock_get):
        t = _make_transcript("a", account=ACCOUNT_PRIMARY)
        mock_get.return_value = t
        _fetch_transcripts_with_detail([t], self._accounts())
        assert mock_get.call_args.kwargs.get("api_key") == PRIMARY_KEY

    @patch("integrations.fireflies.sync.get_transcript")
    def test_named_account_uses_its_own_key(self, mock_get):
        t = _make_transcript("b", account="jake")
        mock_get.return_value = t
        _fetch_transcripts_with_detail([t], self._accounts())
        assert mock_get.call_args.kwargs.get("api_key") == JAKE_KEY

    @patch("integrations.fireflies.sync.get_transcript")
    def test_unknown_account_raises_not_silent_fallback(self, mock_get):
        t = _make_transcript("x", account="ghost")
        with pytest.raises(FirefliesUnknownAccountError):
            _fetch_transcripts_with_detail([t], self._accounts())
        mock_get.assert_not_called()

    @patch("integrations.fireflies.sync.get_transcript")
    def test_api_error_falls_back_to_summary(self, mock_get):
        t = _make_transcript("a", account=ACCOUNT_PRIMARY)
        mock_get.side_effect = FirefliesAPIError(500, "boom")
        result = _fetch_transcripts_with_detail([t], self._accounts())
        assert result == [t]


# ---------------------------------------------------------------------------
# run_sync — end to end with mocked account discovery / client / vault writer
# ---------------------------------------------------------------------------


class TestRunSync:
    def test_no_accounts_configured_returns_failed(self, tmp_path, monkeypatch):
        monkeypatch.setattr("integrations.fireflies.sync.build_account_configs_from_env", lambda: [])
        monkeypatch.setattr("integrations.fireflies.sync._STATE_FILE", tmp_path / "state.json")
        result = run_sync()
        assert result["status"] == "failed"
        assert "FIREFLIES_API_KEY" in result["message"]

    def test_auth_error_returns_failed(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "integrations.fireflies.sync.build_account_configs_from_env",
            lambda: [FirefliesAccountConfig(name=ACCOUNT_PRIMARY, api_key="bad")],
        )
        monkeypatch.setattr("integrations.fireflies.sync._STATE_FILE", tmp_path / "state.json")
        monkeypatch.setattr(
            "integrations.fireflies.sync.iter_all_transcripts_for_account",
            MagicMock(side_effect=FirefliesAuthError()),
        )
        result = run_sync()
        assert result["status"] == "failed"

    def test_no_new_transcripts_returns_success_with_zero(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "integrations.fireflies.sync.build_account_configs_from_env",
            lambda: [FirefliesAccountConfig(name=ACCOUNT_PRIMARY, api_key="k")],
        )
        monkeypatch.setattr("integrations.fireflies.sync._STATE_FILE", tmp_path / "state.json")
        monkeypatch.setattr(
            "integrations.fireflies.sync.iter_all_transcripts_for_account",
            MagicMock(return_value=[]),
        )
        result = run_sync()
        assert result["status"] == "success"
        assert result["transcripts_written"] == 0

    def test_dry_run_does_not_write_or_persist_state(self, tmp_path, monkeypatch):
        state_file = tmp_path / "state.json"
        t = _make_transcript("a")
        monkeypatch.setattr(
            "integrations.fireflies.sync.build_account_configs_from_env",
            lambda: [FirefliesAccountConfig(name=ACCOUNT_PRIMARY, api_key="k")],
        )
        monkeypatch.setattr("integrations.fireflies.sync._STATE_FILE", state_file)
        monkeypatch.setattr(
            "integrations.fireflies.sync.iter_all_transcripts_for_account",
            MagicMock(return_value=[t]),
        )
        monkeypatch.setattr("integrations.fireflies.sync.get_transcript", MagicMock(return_value=t))

        result = run_sync(dry_run=True)
        assert result["status"] == "dry_run"
        assert not state_file.exists()

    def test_successful_sync_writes_to_vault_and_updates_state(self, tmp_path, monkeypatch):
        state_file = tmp_path / "state.json"
        vault_path = tmp_path / "vault"
        t = _make_transcript("a", title="Real Call")
        monkeypatch.setattr(
            "integrations.fireflies.sync.build_account_configs_from_env",
            lambda: [FirefliesAccountConfig(name=ACCOUNT_PRIMARY, api_key="k")],
        )
        monkeypatch.setattr("integrations.fireflies.sync._STATE_FILE", state_file)
        monkeypatch.setattr("integrations.fireflies.sync._VAULT_PATH", vault_path)
        monkeypatch.setattr(
            "integrations.fireflies.sync.iter_all_transcripts_for_account",
            MagicMock(return_value=[t]),
        )
        monkeypatch.setattr("integrations.fireflies.sync.get_transcript", MagicMock(return_value=t))

        result = run_sync()

        assert result["status"] == "success"
        assert result["transcripts_written"] == 1
        assert state_file.exists()
        written_file = vault_path / "fireflies" / "2026" / "06" / "2026-06-01-real-call.md"
        assert written_file.exists()

    def test_one_account_auth_failure_does_not_abort_others(self, tmp_path, monkeypatch):
        """
        Design-gap fix: a single account's auth failure must not abort the
        entire multi-account sync. The healthy account's transcripts should
        still be fetched and written; the failing account should be reported
        in `account_errors`, not swallow the whole run.
        """
        state_file = tmp_path / "state.json"
        vault_path = tmp_path / "vault"
        good_t = _make_transcript("a", title="Good Call", account="jake")

        monkeypatch.setattr(
            "integrations.fireflies.sync.build_account_configs_from_env",
            lambda: [
                FirefliesAccountConfig(name=ACCOUNT_PRIMARY, api_key="bad"),
                FirefliesAccountConfig(name="jake", api_key="good"),
            ],
        )
        monkeypatch.setattr("integrations.fireflies.sync._STATE_FILE", state_file)
        monkeypatch.setattr("integrations.fireflies.sync._VAULT_PATH", vault_path)

        def fake_iter(account, since=None):
            if account.name == ACCOUNT_PRIMARY:
                raise FirefliesAuthError()
            return [good_t]

        monkeypatch.setattr("integrations.fireflies.sync.iter_all_transcripts_for_account", fake_iter)
        monkeypatch.setattr("integrations.fireflies.sync.get_transcript", MagicMock(return_value=good_t))

        result = run_sync()

        assert result["status"] == "partial"
        assert result["transcripts_written"] == 1
        assert ACCOUNT_PRIMARY in result["account_errors"]
        assert "jake" not in result["account_errors"]

    def test_all_accounts_failing_still_returns_failed(self, tmp_path, monkeypatch):
        """Unlike the partial-failure case, if every account fails, the run fails."""
        monkeypatch.setattr(
            "integrations.fireflies.sync.build_account_configs_from_env",
            lambda: [
                FirefliesAccountConfig(name=ACCOUNT_PRIMARY, api_key="bad"),
                FirefliesAccountConfig(name="jake", api_key="also-bad"),
            ],
        )
        monkeypatch.setattr("integrations.fireflies.sync._STATE_FILE", tmp_path / "state.json")
        monkeypatch.setattr(
            "integrations.fireflies.sync.iter_all_transcripts_for_account",
            MagicMock(side_effect=FirefliesAuthError()),
        )

        result = run_sync()

        assert result["status"] == "failed"
        assert set(result["account_errors"]) == {ACCOUNT_PRIMARY, "jake"}

    def test_query_since_applies_lookback_buffer_before_watermark(self, tmp_path, monkeypatch):
        """
        Design-gap fix: Fireflies' `fromDate` filter is meeting-date based,
        not ingestion-time based, so late-finalised transcripts with an older
        meeting date could be silently missed if we queried using the raw
        watermark. The sync must query a buffer window *before* the stored
        watermark, not the watermark itself.
        """
        from datetime import datetime as dt
        from datetime import timezone as tz

        state_file = tmp_path / "state.json"
        watermark = "2026-07-10T00:00:00.000Z"
        state_file.write_text(json.dumps({"last_sync_at": watermark, "total_synced": 0, "last_run_at": None}))

        monkeypatch.setattr(
            "integrations.fireflies.sync.build_account_configs_from_env",
            lambda: [FirefliesAccountConfig(name=ACCOUNT_PRIMARY, api_key="k")],
        )
        monkeypatch.setattr("integrations.fireflies.sync._STATE_FILE", state_file)

        captured_since = {}

        def fake_iter(account, since=None):
            captured_since["value"] = since
            return []

        monkeypatch.setattr("integrations.fireflies.sync.iter_all_transcripts_for_account", fake_iter)

        run_sync()

        raw_watermark = dt.fromisoformat(watermark.replace("Z", "+00:00"))
        assert captured_since["value"] is not None
        assert captured_since["value"] < raw_watermark
        assert (raw_watermark - captured_since["value"]) == timedelta(days=3)
