"""
Tests for the Skill Manager — composable skill layering system.

Tests manifest parsing, context assembly, priority ordering,
conditional composition, state CRUD, and graceful degradation.
"""

import json
import os
import sys
from pathlib import Path

import pytest

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src" / "mcp"))

from skill_manager import (
    _parse_manifest,
    _resolve_skill_dirs,
    _merge_behavior,
    _read_context,
    _read_preferences_defaults,
    _read_preferences_schema,
    _assemble_context,
    _extract_skill_name,
    list_available_skills,
    get_active_skills,
    get_skill_context,
    activate_skill,
    deactivate_skill,
    mark_installed,
    get_skill_preferences,
    set_skill_preference,
)


# =============================================================================
# Helpers
# =============================================================================

def _create_skill_toml(skill_dir: Path, name: str, **kwargs):
    """Create a minimal skill.toml in the given directory."""
    version = kwargs.get("version", "1.0.0")
    description = kwargs.get("description", f"Test skill {name}")
    category = kwargs.get("category", "workflow")
    priority = kwargs.get("priority", 50)
    mode = kwargs.get("mode", "always")

    content = f"""[skill]
name = "{name}"
version = "{version}"
description = "{description}"
category = "{category}"

[activation]
mode = "{mode}"

[layering]
priority = {priority}
"""
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "skill.toml").write_text(content)


def _create_skill_json(skill_dir: Path, name: str, **kwargs):
    """Create a minimal skill.json in the given directory."""
    data = {
        "name": name,
        "version": kwargs.get("version", "1.0.0"),
        "description": kwargs.get("description", f"Test skill {name}"),
        "adds": {"mcp_tools": [], "bot_commands": []},
    }
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "skill.json").write_text(json.dumps(data, indent=2))


def _create_behavior(skill_dir: Path, filename: str, content: str):
    """Create a behavior markdown file."""
    behavior_dir = skill_dir / "behavior"
    behavior_dir.mkdir(parents=True, exist_ok=True)
    (behavior_dir / filename).write_text(content)


def _create_context(skill_dir: Path, filename: str, content: str):
    """Create a context markdown file."""
    context_dir = skill_dir / "context"
    context_dir.mkdir(parents=True, exist_ok=True)
    (context_dir / filename).write_text(content)


# =============================================================================
# Manifest Parsing
# =============================================================================

class TestParseManifest:
    def test_parses_toml(self, tmp_path):
        """skill.toml is parsed correctly."""
        _create_skill_toml(tmp_path, "test-skill", version="2.0.0")
        result = _parse_manifest(tmp_path)
        assert result is not None
        assert result["skill"]["name"] == "test-skill"
        assert result["skill"]["version"] == "2.0.0"

    def test_parses_json_fallback(self, tmp_path):
        """Falls back to skill.json when skill.toml is absent."""
        _create_skill_json(tmp_path, "json-skill")
        result = _parse_manifest(tmp_path)
        assert result is not None
        # JSON gets wrapped in {"skill": ...}
        assert result["skill"]["name"] == "json-skill"

    def test_toml_preferred_over_json(self, tmp_path):
        """skill.toml takes priority when both exist."""
        _create_skill_toml(tmp_path, "toml-wins", version="2.0.0")
        _create_skill_json(tmp_path, "json-loses", version="1.0.0")
        result = _parse_manifest(tmp_path)
        assert result["skill"]["name"] == "toml-wins"

    def test_returns_none_for_empty_dir(self, tmp_path):
        """Returns None when no manifest exists."""
        result = _parse_manifest(tmp_path)
        assert result is None

    def test_returns_none_for_malformed_toml(self, tmp_path):
        """Returns None for unparseable TOML (no JSON fallback)."""
        (tmp_path / "skill.toml").write_text("this is not valid [[[toml")
        result = _parse_manifest(tmp_path)
        assert result is None

    def test_returns_none_for_malformed_json(self, tmp_path):
        """Returns None for unparseable JSON."""
        (tmp_path / "skill.json").write_text("{not valid json")
        result = _parse_manifest(tmp_path)
        assert result is None

    def test_malformed_toml_falls_through_to_json(self, tmp_path):
        """If TOML is malformed, falls through to JSON."""
        (tmp_path / "skill.toml").write_text("bad [[[toml")
        _create_skill_json(tmp_path, "json-fallback")
        result = _parse_manifest(tmp_path)
        assert result is not None
        assert result["skill"]["name"] == "json-fallback"


# =============================================================================
# Skill Directory Resolution
# =============================================================================

class TestResolveSkillDirs:
    def test_finds_skills_in_shop(self, tmp_path):
        """Discovers skills in lobster-shop/."""
        shop = tmp_path / "lobster-shop"
        _create_skill_toml(shop / "skill-a", "skill-a")
        _create_skill_toml(shop / "skill-b", "skill-b")

        dirs = _resolve_skill_dirs(repo_dir=tmp_path)
        names = [d.name for d in dirs]
        assert "skill-a" in names
        assert "skill-b" in names

    def test_ignores_dirs_without_manifest(self, tmp_path):
        """Directories without manifests are skipped."""
        shop = tmp_path / "lobster-shop"
        _create_skill_toml(shop / "real-skill", "real-skill")
        (shop / "not-a-skill").mkdir(parents=True)

        dirs = _resolve_skill_dirs(repo_dir=tmp_path)
        assert len(dirs) == 1
        assert dirs[0].name == "real-skill"

    def test_ignores_hidden_dirs(self, tmp_path):
        """Directories starting with . are skipped."""
        shop = tmp_path / "lobster-shop"
        _create_skill_toml(shop / ".hidden", "hidden")
        _create_skill_toml(shop / "visible", "visible")

        dirs = _resolve_skill_dirs(repo_dir=tmp_path)
        assert len(dirs) == 1

    def test_private_overlay_overrides(self, tmp_path):
        """Private config overlay overrides repo skills with same name."""
        shop = tmp_path / "lobster-shop"
        _create_skill_toml(shop / "my-skill", "my-skill", version="1.0.0")

        config = tmp_path / "config"
        _create_skill_toml(config / "skills" / "my-skill", "my-skill", version="2.0.0")

        dirs = _resolve_skill_dirs(repo_dir=tmp_path, config_dir=str(config))
        assert len(dirs) == 1
        # Should be the overlay version
        manifest = _parse_manifest(dirs[0])
        assert manifest["skill"]["version"] == "2.0.0"


# =============================================================================
# Behavior Assembly
# =============================================================================

class TestMergeBehavior:
    def test_reads_system_md(self, tmp_path):
        """Reads behavior/system.md."""
        _create_behavior(tmp_path, "system.md", "Use the browser wisely.")
        result = _merge_behavior(tmp_path, [])
        assert "Use the browser wisely." in result

    def test_reads_multiple_files(self, tmp_path):
        """Reads and concatenates multiple behavior files."""
        _create_behavior(tmp_path, "01-basics.md", "Basic behavior.")
        _create_behavior(tmp_path, "02-advanced.md", "Advanced behavior.")
        result = _merge_behavior(tmp_path, [])
        assert "Basic behavior." in result
        assert "Advanced behavior." in result

    def test_conditional_included_when_active(self, tmp_path):
        """with-<skill>.md is included when that skill is active."""
        _create_behavior(tmp_path, "system.md", "Base behavior.")
        _create_behavior(tmp_path, "with-calendar.md", "Calendar integration notes.")
        result = _merge_behavior(tmp_path, ["calendar"])
        assert "Calendar integration notes." in result

    def test_conditional_excluded_when_inactive(self, tmp_path):
        """with-<skill>.md is excluded when that skill is NOT active."""
        _create_behavior(tmp_path, "system.md", "Base behavior.")
        _create_behavior(tmp_path, "with-calendar.md", "Calendar integration notes.")
        result = _merge_behavior(tmp_path, [])
        assert "Calendar integration notes." not in result

    def test_no_behavior_dir(self, tmp_path):
        """Returns empty string when behavior/ doesn't exist."""
        result = _merge_behavior(tmp_path, [])
        assert result == ""

    def test_empty_files_skipped(self, tmp_path):
        """Empty .md files don't produce extra whitespace."""
        _create_behavior(tmp_path, "system.md", "Content.")
        _create_behavior(tmp_path, "empty.md", "   ")
        result = _merge_behavior(tmp_path, [])
        assert result == "Content."


# =============================================================================
# Context Assembly
# =============================================================================

class TestAssembleContext:
    def test_single_skill(self, tmp_path):
        """Assembles context for a single active skill."""
        skill_dir = tmp_path / "skill-a"
        _create_skill_toml(skill_dir, "skill-a")
        _create_behavior(skill_dir, "system.md", "Skill A behavior.")
        _create_context(skill_dir, "domain.md", "Skill A domain context.")

        result = _assemble_context(
            skill_dirs={"skill-a": skill_dir},
            active_skills=["skill-a"],
            state={"skills": {"skill-a": {"priority": 50}}},
        )

        assert "# Active Skills" in result
        assert "## Skill: skill-a" in result
        assert "Skill A behavior." in result
        assert "Skill A domain context." in result

    def test_priority_ordering(self, tmp_path):
        """Higher priority skills appear later in output."""
        skill_low = tmp_path / "low"
        _create_skill_toml(skill_low, "low-priority")
        _create_behavior(skill_low, "system.md", "LOW_PRIORITY_CONTENT")

        skill_high = tmp_path / "high"
        _create_skill_toml(skill_high, "high-priority")
        _create_behavior(skill_high, "system.md", "HIGH_PRIORITY_CONTENT")

        result = _assemble_context(
            skill_dirs={"low-priority": skill_low, "high-priority": skill_high},
            active_skills=["low-priority", "high-priority"],
            state={"skills": {
                "low-priority": {"priority": 10},
                "high-priority": {"priority": 90},
            }},
        )

        low_pos = result.index("LOW_PRIORITY_CONTENT")
        high_pos = result.index("HIGH_PRIORITY_CONTENT")
        assert low_pos < high_pos, "Lower priority should appear first"

    def test_empty_when_no_active(self, tmp_path):
        """Returns empty string when no skills are active."""
        result = _assemble_context(
            skill_dirs={},
            active_skills=[],
            state={"skills": {}},
        )
        assert result == ""

    def test_missing_skill_dir_ignored(self, tmp_path):
        """Active skill with no matching directory is gracefully skipped."""
        result = _assemble_context(
            skill_dirs={},
            active_skills=["ghost-skill"],
            state={"skills": {"ghost-skill": {"priority": 50}}},
        )
        assert result == ""


# =============================================================================
# State CRUD
# =============================================================================

class TestStateCrud:
    def test_activate_skill(self, tmp_path):
        """Activating a skill records it in state."""
        state_path = tmp_path / "state.json"
        shop = tmp_path / "lobster-shop"
        _create_skill_toml(shop / "my-skill", "my-skill")

        result = activate_skill(
            "my-skill", mode="always",
            state_path=state_path, repo_dir=tmp_path,
        )
        assert "activated" in result

        active = get_active_skills(state_path)
        assert "my-skill" in active

    def test_deactivate_skill(self, tmp_path):
        """Deactivating a skill marks it inactive."""
        state_path = tmp_path / "state.json"
        shop = tmp_path / "lobster-shop"
        _create_skill_toml(shop / "my-skill", "my-skill")

        activate_skill("my-skill", state_path=state_path, repo_dir=tmp_path)
        deactivate_skill("my-skill", state_path=state_path)

        active = get_active_skills(state_path)
        assert "my-skill" not in active

    def test_activate_invalid_mode(self, tmp_path):
        """Invalid activation mode returns error."""
        state_path = tmp_path / "state.json"
        shop = tmp_path / "lobster-shop"
        _create_skill_toml(shop / "my-skill", "my-skill")

        result = activate_skill(
            "my-skill", mode="bogus",
            state_path=state_path, repo_dir=tmp_path,
        )
        assert "Error" in result

    def test_activate_nonexistent_skill(self, tmp_path):
        """Activating a nonexistent skill returns error."""
        state_path = tmp_path / "state.json"
        result = activate_skill(
            "ghost", state_path=state_path, repo_dir=tmp_path,
        )
        assert "not found" in result

    def test_mark_installed(self, tmp_path):
        """mark_installed records version and timestamp."""
        state_path = tmp_path / "state.json"
        mark_installed("my-skill", "1.2.3", state_path=state_path)

        data = json.loads(state_path.read_text())
        skill = data["skills"]["my-skill"]
        assert skill["installed"] is True
        assert skill["version"] == "1.2.3"
        assert "installed_at" in skill

    def test_deactivate_nonexistent_is_noop(self, tmp_path):
        """Deactivating a non-tracked skill is a no-op."""
        state_path = tmp_path / "state.json"
        result = deactivate_skill("ghost", state_path=state_path)
        assert "deactivated" in result


# =============================================================================
# Preferences
# =============================================================================

class TestPreferences:
    def test_get_defaults(self, tmp_path):
        """Get preferences returns defaults when no overrides."""
        skill_dir = tmp_path / "lobster-shop" / "my-skill"
        _create_skill_toml(skill_dir, "my-skill")
        prefs_dir = skill_dir / "preferences"
        prefs_dir.mkdir(parents=True)
        (prefs_dir / "defaults.toml").write_text('port = 9377\nauto_close = true\n')

        state_path = tmp_path / "state.json"
        result = get_skill_preferences(
            "my-skill", state_path=state_path, repo_dir=tmp_path,
        )
        assert result["port"] == 9377
        assert result["auto_close"] is True

    def test_user_overrides_defaults(self, tmp_path):
        """User preferences override defaults."""
        skill_dir = tmp_path / "lobster-shop" / "my-skill"
        _create_skill_toml(skill_dir, "my-skill")
        prefs_dir = skill_dir / "preferences"
        prefs_dir.mkdir(parents=True)
        (prefs_dir / "defaults.toml").write_text('port = 9377\n')

        state_path = tmp_path / "state.json"
        # Write state with user override
        data = {"skills": {"my-skill": {"preferences": {"port": 8080}}}}
        state_path.write_text(json.dumps(data))

        result = get_skill_preferences(
            "my-skill", state_path=state_path, repo_dir=tmp_path,
        )
        assert result["port"] == 8080

    def test_set_preference(self, tmp_path):
        """set_skill_preference writes to state."""
        skill_dir = tmp_path / "lobster-shop" / "my-skill"
        _create_skill_toml(skill_dir, "my-skill")

        state_path = tmp_path / "state.json"
        result = set_skill_preference(
            "my-skill", "port", 8080,
            state_path=state_path, repo_dir=tmp_path,
        )
        assert "Set" in result

        data = json.loads(state_path.read_text())
        assert data["skills"]["my-skill"]["preferences"]["port"] == 8080

    def test_set_preference_validates_schema(self, tmp_path):
        """set_skill_preference rejects unknown keys when schema exists."""
        skill_dir = tmp_path / "lobster-shop" / "my-skill"
        _create_skill_toml(skill_dir, "my-skill")
        prefs_dir = skill_dir / "preferences"
        prefs_dir.mkdir(parents=True)
        (prefs_dir / "schema.toml").write_text('[port]\ntype = "integer"\n')

        state_path = tmp_path / "state.json"
        result = set_skill_preference(
            "my-skill", "unknown_key", "value",
            state_path=state_path, repo_dir=tmp_path,
        )
        assert "Error" in result
        assert "unknown preference" in result


# =============================================================================
# List Available Skills
# =============================================================================

class TestListAvailableSkills:
    def test_lists_skills_with_status(self, tmp_path):
        """Lists all skills with install/active status from state."""
        shop = tmp_path / "lobster-shop"
        _create_skill_toml(shop / "skill-a", "skill-a")
        _create_skill_toml(shop / "skill-b", "skill-b")

        state_path = tmp_path / "state.json"
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps({
            "skills": {"skill-a": {"installed": True, "active": True}}
        }))

        result = list_available_skills(
            repo_dir=tmp_path, state_path=state_path,
        )
        names = {s["name"] for s in result}
        assert "skill-a" in names
        assert "skill-b" in names

        skill_a = next(s for s in result if s["name"] == "skill-a")
        assert skill_a["installed"] is True
        assert skill_a["active"] is True

        skill_b = next(s for s in result if s["name"] == "skill-b")
        assert skill_b["installed"] is False


# =============================================================================
# Graceful Degradation
# =============================================================================

class TestGracefulDegradation:
    def test_missing_state_file(self, tmp_path):
        """Operations work when state file doesn't exist."""
        state_path = tmp_path / "nonexistent" / "state.json"
        active = get_active_skills(state_path)
        assert active == []

    def test_malformed_state_file(self, tmp_path):
        """Malformed state file treated as empty."""
        state_path = tmp_path / "state.json"
        state_path.write_text("{bad json")

        active = get_active_skills(state_path)
        assert active == []

    def test_get_context_no_active_skills(self, tmp_path):
        """get_skill_context returns empty when nothing active."""
        state_path = tmp_path / "state.json"
        result = get_skill_context(
            repo_dir=tmp_path, state_path=state_path,
        )
        assert result == ""

    def test_missing_behavior_and_context_dirs(self, tmp_path):
        """Skills without behavior/ or context/ dirs produce empty content."""
        skill_dir = tmp_path / "lobster-shop" / "bare-skill"
        _create_skill_toml(skill_dir, "bare-skill")

        state_path = tmp_path / "state.json"
        activate_skill("bare-skill", state_path=state_path, repo_dir=tmp_path)

        result = get_skill_context(
            repo_dir=tmp_path, state_path=state_path,
        )
        # Still returns empty since no content to assemble
        assert result == ""
