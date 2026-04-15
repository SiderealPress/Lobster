"""
Tests for pipeline hygiene modules.

Tests: idempotency_check, validator, audit_log, dry_run

Run: python -m pytest tests/test_pipeline.py -v
(from ~/lobster/lobster-shop/prospect-enrichment/)
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# Add pipeline to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from pipeline.idempotency_check import is_fresh, check_all_sources, FreshnessResult
from pipeline.validator import (
    validate_contact,
    validate_provenance,
    filter_valid,
    ValidationResult,
)
from pipeline.audit_log import AuditLog, read_run_summary, list_recent_runs
from pipeline.dry_run import DryRunContext


# ---------------------------------------------------------------------------
# idempotency_check tests
# ---------------------------------------------------------------------------

class TestIsFresh:
    def _meta(self, **kv) -> list[dict]:
        return [{"key": k, "value": v} for k, v in kv.items()]

    def test_never_enriched_is_not_fresh(self):
        result = is_fresh(entity_meta=[], source_id="apollo", data_freshness_days=30)
        assert result.skip is False
        assert result.last_enriched_at is None
        assert "No prior enrichment" in result.reason

    def test_source_specific_key_fresh(self):
        now = datetime.now(tz=timezone.utc)
        ts = (now - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        meta = self._meta(**{"provenance.enriched_at.apollo": ts})
        result = is_fresh(entity_meta=meta, source_id="apollo", data_freshness_days=30, now=now)
        assert result.skip is True
        assert result.age_days is not None
        assert result.age_days < 30

    def test_source_specific_key_stale(self):
        now = datetime.now(tz=timezone.utc)
        ts = (now - timedelta(days=35)).strftime("%Y-%m-%dT%H:%M:%SZ")
        meta = self._meta(**{"provenance.enriched_at.apollo": ts})
        result = is_fresh(entity_meta=meta, source_id="apollo", data_freshness_days=30, now=now)
        assert result.skip is False
        assert result.age_days > 30

    def test_generic_key_fallback_matching_source(self):
        now = datetime.now(tz=timezone.utc)
        ts = (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        meta = self._meta(**{
            "provenance.enriched_at": ts,
            "provenance.source": "apollo",
        })
        result = is_fresh(entity_meta=meta, source_id="apollo", data_freshness_days=30, now=now)
        assert result.skip is True

    def test_generic_key_fallback_different_source(self):
        now = datetime.now(tz=timezone.utc)
        ts = (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        meta = self._meta(**{
            "provenance.enriched_at": ts,
            "provenance.source": "hunter",  # Different source
        })
        result = is_fresh(entity_meta=meta, source_id="apollo", data_freshness_days=30, now=now)
        # Generic key doesn't match for "apollo" because source is "hunter"
        assert result.skip is False

    def test_unparseable_timestamp_treated_as_stale(self):
        meta = [{"key": "provenance.enriched_at.apollo", "value": "not-a-timestamp"}]
        result = is_fresh(entity_meta=meta, source_id="apollo", data_freshness_days=30)
        assert result.skip is False

    def test_exactly_at_boundary_is_fresh(self):
        now = datetime.now(tz=timezone.utc)
        # 29.9 days ago — still within 30-day window
        ts = (now - timedelta(days=29, hours=23)).strftime("%Y-%m-%dT%H:%M:%SZ")
        meta = [{"key": "provenance.enriched_at.apollo", "value": ts}]
        result = is_fresh(entity_meta=meta, source_id="apollo", data_freshness_days=30, now=now)
        assert result.skip is True

    def test_check_all_sources(self):
        now = datetime.now(tz=timezone.utc)
        recent = (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        meta = [{"key": "provenance.enriched_at.apollo", "value": recent}]
        sources = [
            {"source_id": "apollo", "data_freshness_days": 30},
            {"source_id": "hunter", "data_freshness_days": 30},
        ]
        results = check_all_sources(entity_meta=meta, sources=sources, now=now)
        assert results["apollo"].skip is True
        assert results["hunter"].skip is False


# ---------------------------------------------------------------------------
# validator tests
# ---------------------------------------------------------------------------

class TestValidateContact:
    def _valid_contact(self, **overrides) -> dict:
        base = {
            "name": "Jane Smith",
            "title": "VP Supply Chain",
            "company": "Acme Corp",
            "source_url": "https://acme.com/team",
            "org_kissinger_id": "ent_abc123",
        }
        base.update(overrides)
        return base

    def test_valid_contact_passes(self):
        result = validate_contact(self._valid_contact())
        assert result.valid is True
        assert not result.errors

    def test_missing_name_fails(self):
        result = validate_contact(self._valid_contact(name=""))
        assert result.valid is False
        assert any("name" in e for e in result.errors)

    def test_none_name_fails(self):
        c = self._valid_contact()
        del c["name"]
        result = validate_contact(c)
        assert result.valid is False

    def test_missing_org_id_fails(self):
        result = validate_contact(self._valid_contact(org_kissinger_id=""))
        assert result.valid is False
        assert any("org_kissinger_id" in e for e in result.errors)

    def test_missing_title_is_warning(self):
        c = self._valid_contact()
        del c["title"]
        result = validate_contact(c)
        assert result.valid is True  # Still valid
        assert any("title" in w for w in result.warnings)

    def test_missing_source_url_is_warning(self):
        c = self._valid_contact()
        del c["source_url"]
        result = validate_contact(c)
        assert result.valid is True
        assert any("source_url" in w for w in result.warnings)

    def test_non_http_url_is_warning(self):
        result = validate_contact(self._valid_contact(source_url="ftp://example.com"))
        assert result.valid is True
        assert any("source_url" in w for w in result.warnings)

    def test_name_too_long_fails(self):
        result = validate_contact(self._valid_contact(name="x" * 300))
        assert result.valid is False

    def test_filter_valid_splits_correctly(self):
        contacts = [
            self._valid_contact(),                          # valid
            self._valid_contact(name=""),                   # invalid
            self._valid_contact(org_kissinger_id=""),       # invalid
        ]
        valid, invalid = filter_valid(contacts)
        assert len(valid) == 1
        assert len(invalid) == 2


class TestValidateProvenance:
    def _valid_provenance(self) -> list[dict]:
        return [
            {"key": "provenance.source", "value": "apollo"},
            {"key": "provenance.source_url", "value": "https://api.apollo.io/v1/people"},
            {"key": "provenance.enriched_at", "value": "2026-04-09T18:00:00Z"},
            {"key": "provenance.enriched_by", "value": "wallace"},
            {"key": "provenance.pipeline_run_id", "value": "a3f8c1d2-1234-4567-89ab-abcdef012345"},
            {"key": "provenance.confidence", "value": "high"},
            {"key": "provenance.goal", "value": "org_chart"},
            {"key": "provenance.raw_response_hash", "value": "sha256:" + "a" * 64},
        ]

    def test_valid_provenance_passes(self):
        result = validate_provenance(self._valid_provenance())
        assert result.valid is True

    def test_missing_required_field_fails(self):
        meta = [m for m in self._valid_provenance() if m["key"] != "provenance.goal"]
        result = validate_provenance(meta)
        assert result.valid is False
        assert any("provenance.goal" in e for e in result.errors)

    def test_invalid_confidence_fails(self):
        meta = self._valid_provenance()
        for m in meta:
            if m["key"] == "provenance.confidence":
                m["value"] = "very_high"
        result = validate_provenance(meta)
        assert result.valid is False

    def test_invalid_goal_fails(self):
        meta = self._valid_provenance()
        for m in meta:
            if m["key"] == "provenance.goal":
                m["value"] = "magic"
        result = validate_provenance(meta)
        assert result.valid is False

    def test_bad_timestamp_fails(self):
        meta = self._valid_provenance()
        for m in meta:
            if m["key"] == "provenance.enriched_at":
                m["value"] = "not-a-timestamp"
        result = validate_provenance(meta)
        assert result.valid is False

    def test_bad_hash_fails(self):
        meta = self._valid_provenance()
        for m in meta:
            if m["key"] == "provenance.raw_response_hash":
                m["value"] = "md5:abc123"
        result = validate_provenance(meta)
        assert result.valid is False

    def test_bad_uuid_fails(self):
        meta = self._valid_provenance()
        for m in meta:
            if m["key"] == "provenance.pipeline_run_id":
                m["value"] = "not-a-uuid"
        result = validate_provenance(meta)
        assert result.valid is False

    def test_non_wallace_enriched_by_is_warning(self):
        meta = self._valid_provenance()
        for m in meta:
            if m["key"] == "provenance.enriched_by":
                m["value"] = "human"
        result = validate_provenance(meta)
        assert result.valid is True  # Warning, not error
        assert any("enriched_by" in w for w in result.warnings)


# ---------------------------------------------------------------------------
# audit_log tests
# ---------------------------------------------------------------------------

class TestAuditLog:
    def test_creates_rollback_file(self, tmp_path):
        log = AuditLog(run_id="test-run-001", dry_run=False, base_dir=tmp_path)
        log.entity_created(
            entity_id="ent_abc",
            entity_name="Jane Smith",
            source="apollo",
            goal="org_chart",
            meta_written={"provenance.source": "apollo"},
        )
        assert log.rollback_path.exists()
        lines = log.rollback_path.read_text().strip().split("\n")
        assert len(lines) == 1
        event = json.loads(lines[0])
        assert event["event"] == "entity_created"
        assert event["entity_name"] == "Jane Smith"

    def test_multiple_events_written(self, tmp_path):
        log = AuditLog(run_id="test-run-002", dry_run=False, base_dir=tmp_path)
        log.entity_created("ent_1", "Alice", "apollo", "org_chart", {})
        log.edge_created("ent_1", "ent_org", "works_at")
        log.skipped_fresh("ent_2", "Bob", "apollo", "2026-04-01T00:00:00Z", 8.0)
        log.write_error("Unknown", "hunter", "HTTP 500")

        lines = log.rollback_path.read_text().strip().split("\n")
        assert len(lines) == 4
        events = [json.loads(l) for l in lines]
        assert events[0]["event"] == "entity_created"
        assert events[1]["event"] == "edge_created"
        assert events[2]["event"] == "skipped_fresh"
        assert events[3]["event"] == "error"

    def test_close_writes_summary(self, tmp_path):
        log = AuditLog(run_id="test-run-003", dry_run=True, base_dir=tmp_path)
        summary_path = log.close({"status": "completed", "contacts_added": 5})
        assert summary_path.exists()
        summary = json.loads(summary_path.read_text())
        assert summary["run_id"] == "test-run-003"
        assert summary["dry_run"] is True
        assert summary["contacts_added"] == 5
        assert "started_at" in summary
        assert "finished_at" in summary

    def test_read_run_summary(self, tmp_path):
        log = AuditLog(run_id="test-run-004", dry_run=False, base_dir=tmp_path)
        log.close({"status": "completed"})
        result = read_run_summary("test-run-004", base_dir=tmp_path)
        assert result is not None
        assert result["run_id"] == "test-run-004"

    def test_read_nonexistent_run(self, tmp_path):
        result = read_run_summary("nonexistent-run", base_dir=tmp_path)
        assert result is None

    def test_list_recent_runs(self, tmp_path):
        for i in range(3):
            log = AuditLog(run_id=f"test-run-{i:03d}", dry_run=False, base_dir=tmp_path)
            log.close({"status": "completed", "index": i})
        runs = list_recent_runs(base_dir=tmp_path)
        assert len(runs) == 3

    def test_dry_run_flag_in_events(self, tmp_path):
        log = AuditLog(run_id="dry-run-001", dry_run=True, base_dir=tmp_path)
        log.entity_created("ent_x", "X", "apollo", "org_chart", {})
        lines = log.rollback_path.read_text().strip().split("\n")
        event = json.loads(lines[0])
        assert event["dry_run"] is True


# ---------------------------------------------------------------------------
# dry_run tests
# ---------------------------------------------------------------------------

class TestDryRunContext:
    def test_disabled_would_write_returns_true(self):
        ctx = DryRunContext(enabled=False)
        assert ctx.would_write("Jane", "createEntity") is True
        assert not ctx.skipped_writes

    def test_enabled_would_write_returns_false(self):
        ctx = DryRunContext(enabled=True)
        assert ctx.would_write("Jane", "createEntity") is False
        assert len(ctx.skipped_writes) == 1
        assert ctx.skipped_writes[0].entity_name == "Jane"
        assert ctx.skipped_writes[0].operation == "createEntity"

    def test_enabled_records_multiple_skips(self):
        ctx = DryRunContext(enabled=True)
        ctx.would_write("Alice", "createEntity")
        ctx.would_write("Bob", "createEdge")
        assert len(ctx.skipped_writes) == 2

    def test_summary_disabled(self):
        ctx = DryRunContext(enabled=False)
        assert "disabled" in ctx.summary().lower()

    def test_summary_enabled_no_writes(self):
        ctx = DryRunContext(enabled=True)
        assert "no writes" in ctx.summary().lower()

    def test_summary_enabled_with_writes(self):
        ctx = DryRunContext(enabled=True)
        ctx.would_write("Jane", "createEntity")
        ctx.would_write("Bob", "createEdge")
        summary = ctx.summary()
        assert "2 write" in summary
        assert "Jane" in summary
        assert "Bob" in summary

    def test_context_manager(self, capsys):
        with DryRunContext(enabled=True) as ctx:
            ctx.would_write("Alice", "createEntity")
        # Should print summary to stderr on exit
        captured = capsys.readouterr()
        assert "Alice" in captured.err

    def test_live_mode_context_manager(self, capsys):
        with DryRunContext(enabled=False) as ctx:
            assert ctx.would_write("Alice", "createEntity") is True
        # No summary printed in live mode
        captured = capsys.readouterr()
        assert captured.err == ""
