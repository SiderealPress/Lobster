"""Unit tests for the filesystem-based ghost detection additions in ghost-detector.py.

All tests operate on pure functions — no DB access, no real filesystem reads
beyond temp-dir fixtures.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import importlib.util

import pytest

# ghost-detector.py has a hyphenated filename so it can't be imported via
# normal sys.path manipulation. Use importlib to load it directly.
_SCRIPT_PATH = Path(__file__).parent.parent.parent / "scripts" / "ghost-detector.py"
_spec = importlib.util.spec_from_file_location("ghost_detector", _SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
gd = importlib.util.module_from_spec(_spec)
sys.modules["ghost_detector"] = gd  # register before exec so dataclasses can resolve the module
_spec.loader.exec_module(gd)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

NOW = datetime(2026, 3, 15, 12, 0, 0, tzinfo=timezone.utc)


def _make_symlink(tmp_path: Path, agent_id: str, mtime_offset_seconds: int = 0) -> Path:
    """Create a fake agent JSONL symlink in a tasks/ dir with a controlled mtime."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)

    target = tmp_path / f"{agent_id}.jsonl"
    target.write_text("fake jsonl content")

    # Set mtime on the target relative to NOW
    target_mtime = NOW.timestamp() - mtime_offset_seconds
    os.utime(target, (target_mtime, target_mtime))

    symlink = tasks_dir / f"agent-{agent_id}.jsonl"
    symlink.symlink_to(target)
    return symlink


# ---------------------------------------------------------------------------
# extract_agent_id_from_symlink
# ---------------------------------------------------------------------------


class TestExtractAgentIdFromSymlink:
    def test_valid_hex_id(self, tmp_path: Path) -> None:
        symlink = tmp_path / "agent-a63b24cac13519415.jsonl"
        symlink.touch()
        assert gd.extract_agent_id_from_symlink(symlink) == "a63b24cac13519415"

    def test_returns_none_for_non_matching_name(self, tmp_path: Path) -> None:
        symlink = tmp_path / "not-an-agent.jsonl"
        symlink.touch()
        assert gd.extract_agent_id_from_symlink(symlink) is None

    def test_returns_none_for_uppercase_hex(self, tmp_path: Path) -> None:
        # Pattern only matches lowercase hex — uppercase is invalid
        symlink = tmp_path / "agent-A63B24CAC13519415.jsonl"
        symlink.touch()
        assert gd.extract_agent_id_from_symlink(symlink) is None

    def test_short_hex_id_accepted(self, tmp_path: Path) -> None:
        symlink = tmp_path / "agent-abc123.jsonl"
        symlink.touch()
        assert gd.extract_agent_id_from_symlink(symlink) == "abc123"


# ---------------------------------------------------------------------------
# find_agent_symlinks
# ---------------------------------------------------------------------------


class TestFindAgentSymlinks:
    def test_finds_symlinks(self, tmp_path: Path) -> None:
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()

        target = tmp_path / "abc123.jsonl"
        target.touch()
        symlink = tasks_dir / "agent-abc123.jsonl"
        symlink.symlink_to(target)

        result = gd.find_agent_symlinks(tasks_dir)
        assert len(result) == 1
        assert result[0].name == "agent-abc123.jsonl"

    def test_ignores_non_symlinks(self, tmp_path: Path) -> None:
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        (tasks_dir / "agent-abc123.jsonl").write_text("not a symlink")

        result = gd.find_agent_symlinks(tasks_dir)
        assert result == []

    def test_ignores_symlinks_with_non_matching_names(self, tmp_path: Path) -> None:
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()

        target = tmp_path / "other.jsonl"
        target.touch()
        symlink = tasks_dir / "other.jsonl"
        symlink.symlink_to(target)

        result = gd.find_agent_symlinks(tasks_dir)
        assert result == []

    def test_returns_empty_for_nonexistent_dir(self, tmp_path: Path) -> None:
        result = gd.find_agent_symlinks(tmp_path / "does-not-exist")
        assert result == []

    def test_finds_multiple_symlinks(self, tmp_path: Path) -> None:
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()

        for agent_id in ["aaa111", "bbb222", "ccc333"]:
            target = tmp_path / f"{agent_id}.jsonl"
            target.touch()
            (tasks_dir / f"agent-{agent_id}.jsonl").symlink_to(target)

        result = gd.find_agent_symlinks(tasks_dir)
        assert len(result) == 3


# ---------------------------------------------------------------------------
# compute_symlink_target_age_minutes
# ---------------------------------------------------------------------------


class TestComputeSymlinkTargetAgeMinutes:
    def test_returns_correct_age(self, tmp_path: Path) -> None:
        agent_id = "abc123def456"
        symlink = _make_symlink(tmp_path, agent_id, mtime_offset_seconds=600)  # 10 minutes ago
        age = gd.compute_symlink_target_age_minutes(symlink, NOW)
        assert age is not None
        assert 9.9 <= age <= 10.1

    def test_returns_none_for_broken_symlink(self, tmp_path: Path) -> None:
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        symlink = tasks_dir / "agent-abc123.jsonl"
        symlink.symlink_to(tmp_path / "does-not-exist.jsonl")

        age = gd.compute_symlink_target_age_minutes(symlink, NOW)
        assert age is None

    def test_fresh_file_has_near_zero_age(self, tmp_path: Path) -> None:
        agent_id = "fresh0000"
        symlink = _make_symlink(tmp_path, agent_id, mtime_offset_seconds=5)  # 5 seconds ago
        age = gd.compute_symlink_target_age_minutes(symlink, NOW)
        assert age is not None
        assert age < 1.0


# ---------------------------------------------------------------------------
# discover_filesystem_agents
# ---------------------------------------------------------------------------


class TestDiscoverFilesystemAgents:
    def _build_glob_base(self, tmp_path: Path) -> str:
        """Return a glob pattern that points to our fake session directory."""
        session_dir = tmp_path / "session-abc" / "tasks"
        session_dir.mkdir(parents=True)
        # glob base should match the tasks/ dirs
        return str(tmp_path / "*/tasks/")

    def test_returns_empty_when_no_task_dirs(self, tmp_path: Path) -> None:
        result = gd.discover_filesystem_agents(NOW, set(), glob_base=str(tmp_path / "*/tasks/"))
        assert result == []

    def test_detects_unregistered_agent(self, tmp_path: Path) -> None:
        agent_id = "deadbeef1234"
        session_dir = tmp_path / "session-1"
        _make_symlink(session_dir, agent_id, mtime_offset_seconds=120)  # 2 min ago = active

        result = gd.discover_filesystem_agents(
            NOW,
            known_agent_ids=set(),
            active_threshold_minutes=30.0,
            glob_base=str(tmp_path / "*/tasks/"),
        )
        assert len(result) == 1
        assert result[0].agent_id == agent_id
        assert result[0].is_active is True

    def test_skips_known_agent(self, tmp_path: Path) -> None:
        agent_id = "known000001"
        session_dir = tmp_path / "session-1"
        _make_symlink(session_dir, agent_id, mtime_offset_seconds=60)

        result = gd.discover_filesystem_agents(
            NOW,
            known_agent_ids={agent_id},
            active_threshold_minutes=30.0,
            glob_base=str(tmp_path / "*/tasks/"),
        )
        assert result == []

    def test_marks_stale_agent_inactive(self, tmp_path: Path) -> None:
        agent_id = "dead000001"
        session_dir = tmp_path / "session-1"
        _make_symlink(session_dir, agent_id, mtime_offset_seconds=3600)  # 60 min ago

        result = gd.discover_filesystem_agents(
            NOW,
            known_agent_ids=set(),
            active_threshold_minutes=30.0,
            glob_base=str(tmp_path / "*/tasks/"),
        )
        assert len(result) == 1
        assert result[0].is_active is False

    def test_skips_broken_symlinks(self, tmp_path: Path) -> None:
        tasks_dir = tmp_path / "session-1" / "tasks"
        tasks_dir.mkdir(parents=True)
        broken = tasks_dir / "agent-deadbeef.jsonl"
        broken.symlink_to(tmp_path / "missing-target.jsonl")

        result = gd.discover_filesystem_agents(
            NOW,
            known_agent_ids=set(),
            glob_base=str(tmp_path / "*/tasks/"),
        )
        assert result == []

    def test_discovers_across_multiple_session_dirs(self, tmp_path: Path) -> None:
        for i, agent_id in enumerate(["aaaa111", "bbbb222", "cccc333"]):
            session_dir = tmp_path / f"session-{i}"
            _make_symlink(session_dir, agent_id, mtime_offset_seconds=60)

        result = gd.discover_filesystem_agents(
            NOW,
            known_agent_ids={"aaaa111"},  # one known — should be skipped
            glob_base=str(tmp_path / "*/tasks/"),
        )
        assert len(result) == 2
        found_ids = {r.agent_id for r in result}
        assert "bbbb222" in found_ids
        assert "cccc333" in found_ids


# ---------------------------------------------------------------------------
# build_report — unregistered section
# ---------------------------------------------------------------------------


class TestBuildReportUnregistered:
    def _make_unregistered(self, agent_id: str, age_min: float, active: bool) -> gd.UnregisteredAgent:
        return gd.UnregisteredAgent(
            agent_id=agent_id,
            output_file=f"/tmp/tasks/agent-{agent_id}.jsonl",
            output_file_age_minutes=age_min,
            is_active=active,
        )

    def test_report_includes_unregistered_section(self) -> None:
        unreg = [self._make_unregistered("abc123", 5.0, True)]
        report = gd.build_report([], unreg, NOW, 30.0, 10.0)
        assert "UNREGISTERED" in report
        assert "abc123" in report

    def test_report_omits_unregistered_section_when_empty(self) -> None:
        report = gd.build_report([], [], NOW, 30.0, 10.0)
        assert "UNREGISTERED" not in report

    def test_report_summary_includes_unregistered_count(self) -> None:
        unreg = [
            self._make_unregistered("aaa111", 5.0, True),
            self._make_unregistered("bbb222", 60.0, False),
        ]
        report = gd.build_report([], unreg, NOW, 30.0, 10.0)
        # Summary line should mention 2 unregistered
        assert "2 unregistered" in report

    def test_format_unregistered_line_active(self) -> None:
        agent = self._make_unregistered("deadbeef", 5.0, True)
        line = gd.format_unregistered_line(agent)
        assert "ACTIVE" in line
        assert "deadbeef" in line

    def test_format_unregistered_line_stale(self) -> None:
        agent = self._make_unregistered("deadbeef", 60.0, False)
        line = gd.format_unregistered_line(agent)
        assert "STALE" in line


# ---------------------------------------------------------------------------
# build_unregistered_mark_failed_payload — pure output
# ---------------------------------------------------------------------------


class TestBuildUnregisteredMarkFailedPayload:
    def test_returns_dict_with_required_keys(self) -> None:
        agent = gd.UnregisteredAgent(
            agent_id="abc123def456",
            output_file="/tmp/tasks/agent-abc123def456.jsonl",
            output_file_age_minutes=45.0,
            is_active=False,
        )
        payload = gd.build_unregistered_mark_failed_payload(agent)
        # issue #669: must use agent_failed (not subagent_result) routed to dispatcher
        assert payload["type"] == "agent_failed"
        assert payload["source"] == "system"
        assert payload["chat_id"] == 0
        assert payload.get("forward") is not True  # must not be forwarded to user directly
        assert "abc123def456" in payload["text"]
        assert payload["task_id"].startswith("ghost-unregistered-")
        assert "id" in payload
        assert "timestamp" in payload


# ---------------------------------------------------------------------------
# mark_failed_unregistered_dead — only acts on stale agents
# ---------------------------------------------------------------------------


class TestMarkFailedUnregisteredDead:
    def test_only_notifies_stale_agents(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        dropped: list[dict] = []
        monkeypatch.setattr(gd, "drop_inbox_message", lambda payload: dropped.append(payload))

        active = gd.UnregisteredAgent("active00", "/tmp/a.jsonl", 5.0, True)
        stale = gd.UnregisteredAgent("stale000", "/tmp/b.jsonl", 60.0, False)

        gd.mark_failed_unregistered_dead([active, stale])

        assert len(dropped) == 1
        assert "stale000" in dropped[0]["text"]

    def test_does_nothing_when_all_active(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        dropped: list[dict] = []
        monkeypatch.setattr(gd, "drop_inbox_message", lambda payload: dropped.append(payload))

        agent = gd.UnregisteredAgent("active00", "/tmp/a.jsonl", 5.0, True)
        gd.mark_failed_unregistered_dead([agent])

        assert dropped == []


# ---------------------------------------------------------------------------
# check_transcript_for_write_result
# ---------------------------------------------------------------------------


class TestCheckTranscriptForWriteResult:
    def test_returns_true_when_write_result_present(self, tmp_path: Path) -> None:
        """A transcript containing mcp__lobster-inbox__write_result → True."""
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text(
            '{"type":"tool_use","name":"mcp__lobster-inbox__write_result","input":{"task_id":"t1"}}\n'
        )
        assert gd.check_transcript_for_write_result(str(transcript)) is True

    def test_returns_false_for_empty_file(self, tmp_path: Path) -> None:
        """Empty transcript → False."""
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text("")
        assert gd.check_transcript_for_write_result(str(transcript)) is False

    def test_returns_false_when_write_result_wrong_namespace(self, tmp_path: Path) -> None:
        """write_result from a different namespace (not mcp__lobster-inbox) → False."""
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text(
            '{"type":"tool_use","name":"some_other_tool__write_result","input":{}}\n'
        )
        assert gd.check_transcript_for_write_result(str(transcript)) is False

    def test_returns_false_for_nonexistent_file(self) -> None:
        """Non-existent file path → False (no exception raised)."""
        assert gd.check_transcript_for_write_result("/tmp/does-not-exist-xyz.jsonl") is False

    def test_returns_false_when_only_mcp_namespace_no_write_result(self, tmp_path: Path) -> None:
        """mcp__lobster-inbox prefix present but no write_result call → False."""
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text(
            '{"type":"tool_use","name":"mcp__lobster-inbox__send_reply","input":{}}\n'
        )
        assert gd.check_transcript_for_write_result(str(transcript)) is False

    def test_returns_true_with_multiline_jsonl(self, tmp_path: Path) -> None:
        """write_result appears after many other tool calls → True."""
        transcript = tmp_path / "transcript.jsonl"
        lines = [
            '{"type":"tool_use","name":"mcp__lobster-inbox__send_reply","input":{}}\n',
            '{"type":"tool_use","name":"Bash","input":{"command":"ls"}}\n',
            '{"type":"tool_use","name":"mcp__lobster-inbox__write_result","input":{"task_id":"t99"}}\n',
        ]
        transcript.write_text("".join(lines))
        assert gd.check_transcript_for_write_result(str(transcript)) is True


# ---------------------------------------------------------------------------
# detect_completed_not_updated
# ---------------------------------------------------------------------------


class TestDetectCompletedNotUpdated:
    def _make_row(
        self,
        agent_id: str,
        output_file: str | None,
        description: str = "test agent",
    ) -> gd.AgentRow:
        return gd.AgentRow(
            agent_id=agent_id,
            task_id=None,
            description=description,
            chat_id="12345",
            status="running",
            spawned_at="2026-03-15T11:00:00+00:00",
            output_file=output_file,
            last_seen_at=None,
        )

    def test_detects_agent_with_write_result_in_transcript(self, tmp_path: Path) -> None:
        transcript = tmp_path / "agent-abc.jsonl"
        transcript.write_text(
            '{"type":"tool_use","name":"mcp__lobster-inbox__write_result","input":{"task_id":"t1"}}\n'
        )
        row = self._make_row("abc", str(transcript))
        result = gd.detect_completed_not_updated([row])
        assert len(result) == 1
        assert result[0].agent_id == "abc"

    def test_skips_agent_with_no_write_result(self, tmp_path: Path) -> None:
        transcript = tmp_path / "agent-def.jsonl"
        transcript.write_text('{"type":"tool_use","name":"Bash","input":{}}\n')
        row = self._make_row("def", str(transcript))
        result = gd.detect_completed_not_updated([row])
        assert result == []

    def test_skips_agent_with_no_output_file(self) -> None:
        row = self._make_row("ghi", None)
        result = gd.detect_completed_not_updated([row])
        assert result == []

    def test_skips_agent_with_missing_file(self) -> None:
        row = self._make_row("jkl", "/tmp/nonexistent-agent-jkl.jsonl")
        result = gd.detect_completed_not_updated([row])
        assert result == []
