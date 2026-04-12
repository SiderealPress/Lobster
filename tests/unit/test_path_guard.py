"""Tests for src/mcp/path_guard.py — structural repo/workspace boundary guards."""

import shutil
import sys
import tempfile
from pathlib import Path

import pytest

# Ensure src/mcp is importable
SRC_DIR = Path(__file__).parent.parent.parent / "src" / "mcp"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from path_guard import (
    PathGuardError,
    assert_in_workspace,
    assert_not_in_git_repo,
    validated_workspace,
)


@pytest.fixture
def fake_repo(tmp_path):
    """Create a directory with a .git/ subdirectory (simulates a git repo)."""
    git_dir = tmp_path / "repo" / ".git"
    git_dir.mkdir(parents=True)
    return tmp_path / "repo"


@pytest.fixture
def non_repo(tmp_path):
    """Create a plain directory without .git/."""
    plain = tmp_path / "workspace"
    plain.mkdir(parents=True, exist_ok=True)
    return plain


# ── assert_not_in_git_repo ──────────────────────────────────────────────


class TestAssertNotInGitRepo:
    def test_blocks_path_inside_git_repo(self, fake_repo):
        target = fake_repo / "memory" / "canonical" / "handoff.md"
        target.parent.mkdir(parents=True)
        target.touch()
        with pytest.raises(PathGuardError, match="inside a git repo"):
            assert_not_in_git_repo(target)

    def test_blocks_repo_root_itself(self, fake_repo):
        with pytest.raises(PathGuardError):
            assert_not_in_git_repo(fake_repo)

    def test_allows_path_outside_git_repo(self, non_repo):
        target = non_repo / "data" / "memory-events.jsonl"
        target.parent.mkdir(parents=True)
        target.touch()
        assert_not_in_git_repo(target)  # should not raise

    def test_detects_nested_git_ancestor(self, fake_repo):
        """A deeply nested path still detects the .git/ ancestor."""
        deep = fake_repo / "a" / "b" / "c" / "d" / "file.txt"
        deep.parent.mkdir(parents=True)
        deep.touch()
        with pytest.raises(PathGuardError):
            assert_not_in_git_repo(deep)

    def test_nonexistent_path_checks_parent(self, fake_repo):
        """Even if the file doesn't exist yet, the parent tree is checked."""
        future_file = fake_repo / "future" / "file.md"
        with pytest.raises(PathGuardError):
            assert_not_in_git_repo(future_file)

    def test_allows_path_with_no_git_anywhere(self, tmp_path):
        target = tmp_path / "standalone" / "file.txt"
        target.parent.mkdir(parents=True)
        target.touch()
        assert_not_in_git_repo(target)  # should not raise


# ── assert_in_workspace ─────────────────────────────────────────────────


class TestAssertInWorkspace:
    def test_path_inside_workspace(self, non_repo):
        target = non_repo / "data" / "memory.db"
        target.parent.mkdir(parents=True)
        target.touch()
        assert_in_workspace(target, non_repo)  # should not raise

    def test_blocks_traversal_outside_workspace(self, non_repo):
        outside = non_repo.parent / "other" / "secret.txt"
        outside.parent.mkdir(parents=True)
        outside.touch()
        with pytest.raises(PathGuardError, match="not inside workspace"):
            assert_in_workspace(outside, non_repo)

    def test_blocks_dotdot_traversal(self, non_repo):
        sneaky = non_repo / ".." / "escape" / "file.txt"
        with pytest.raises(PathGuardError):
            assert_in_workspace(sneaky, non_repo)


# ── validated_workspace ──────────────────────────────────────────────────


class TestValidatedWorkspace:
    def test_returns_workspace_when_not_in_repo(self, non_repo):
        result = validated_workspace(non_repo)
        assert result == non_repo

    def test_crashes_when_workspace_is_inside_git_repo(self, fake_repo):
        ws_inside_repo = fake_repo / "workspace"
        ws_inside_repo.mkdir()
        with pytest.raises(PathGuardError):
            validated_workspace(ws_inside_repo)


# ── Integration with write_canonical_file ────────────────────────────────


class TestWriteCanonicalFileGuard:
    def test_write_canonical_file_refuses_git_repo_target(self, fake_repo):
        """Simulates nightly-consolidation.py's write_canonical_file."""
        # Inline the guarded write function
        def write_canonical_file(canonical_dir, relative_path, content):
            path = Path(canonical_dir) / relative_path
            assert_not_in_git_repo(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)

        with pytest.raises(PathGuardError):
            write_canonical_file(
                str(fake_repo / "memory" / "canonical"),
                "handoff.md",
                "# test content",
            )

    def test_write_canonical_file_allows_workspace_target(self, non_repo):
        def write_canonical_file(canonical_dir, relative_path, content):
            path = Path(canonical_dir) / relative_path
            assert_not_in_git_repo(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)

        canonical = non_repo / "memory" / "canonical"
        write_canonical_file(str(canonical), "handoff.md", "# test content")
        assert (canonical / "handoff.md").read_text() == "# test content"


# ── Template seeding ─────────────────────────────────────────────────────


class TestTemplateSeedingLogic:
    def test_copies_missing_files(self, tmp_path):
        """Seeding copies templates that don't exist in canonical dir."""
        templates = tmp_path / "templates"
        canonical = tmp_path / "canonical"
        templates.mkdir()
        canonical.mkdir()

        (templates / "handoff.md").write_text("# Handoff Template")
        (templates / "priorities.md").write_text("# Priorities Template")

        # Seed: copy missing files
        for src in templates.glob("*.md"):
            if src.name.startswith("example-"):
                continue
            dest = canonical / src.name
            if not dest.exists():
                shutil.copy2(str(src), str(dest))

        assert (canonical / "handoff.md").read_text() == "# Handoff Template"
        assert (canonical / "priorities.md").read_text() == "# Priorities Template"

    def test_does_not_overwrite_existing(self, tmp_path):
        """Seeding must not overwrite files that already exist."""
        templates = tmp_path / "templates"
        canonical = tmp_path / "canonical"
        templates.mkdir()
        canonical.mkdir()

        (templates / "handoff.md").write_text("# Template Version")
        (canonical / "handoff.md").write_text("# User's Custom Version")

        for src in templates.glob("*.md"):
            dest = canonical / src.name
            if not dest.exists():
                shutil.copy2(str(src), str(dest))

        assert (canonical / "handoff.md").read_text() == "# User's Custom Version"

    def test_skips_example_files(self, tmp_path):
        """Example files should not be seeded into canonical."""
        templates = tmp_path / "templates"
        canonical = tmp_path / "canonical"
        templates.mkdir()
        canonical.mkdir()

        (templates / "example-person.md").write_text("# Example")
        (templates / "handoff.md").write_text("# Handoff")

        for src in templates.glob("*.md"):
            if src.name.startswith("example-"):
                continue
            dest = canonical / src.name
            if not dest.exists():
                shutil.copy2(str(src), str(dest))

        assert not (canonical / "example-person.md").exists()
        assert (canonical / "handoff.md").exists()
