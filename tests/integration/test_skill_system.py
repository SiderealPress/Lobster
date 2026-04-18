"""
Integration test for the composable skill layering system.

Tests the full lifecycle: parse manifest → install → activate →
get context → compose multiple skills → deactivate → verify removal.
"""

import json
import sys
from pathlib import Path

import pytest

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src" / "mcp"))

from skill_manager import (
    _parse_manifest,
    list_available_skills,
    get_active_skills,
    get_skill_context,
    activate_skill,
    deactivate_skill,
    mark_installed,
    get_skill_preferences,
    set_skill_preference,
)


def _setup_skill(shop_dir, name, behavior_text=None, context_text=None,
                  priority=50, category="workflow", prefs_defaults=None):
    """Create a complete mock skill with all four layers."""
    skill_dir = shop_dir / name
    skill_dir.mkdir(parents=True, exist_ok=True)

    # Manifest
    toml_content = f"""[skill]
name = "{name}"
version = "1.0.0"
description = "Test skill: {name}"
category = "{category}"

[activation]
mode = "always"

[layering]
priority = {priority}
"""
    (skill_dir / "skill.toml").write_text(toml_content)

    # Behavior
    if behavior_text:
        (skill_dir / "behavior").mkdir(exist_ok=True)
        (skill_dir / "behavior" / "system.md").write_text(behavior_text)

    # Context
    if context_text:
        (skill_dir / "context").mkdir(exist_ok=True)
        (skill_dir / "context" / "domain.md").write_text(context_text)

    # Preferences
    if prefs_defaults:
        (skill_dir / "preferences").mkdir(exist_ok=True)
        lines = [f'{k} = {json.dumps(v)}' for k, v in prefs_defaults.items()]
        (skill_dir / "preferences" / "defaults.toml").write_text("\n".join(lines))

    return skill_dir


class TestSkillSystemIntegration:
    """End-to-end skill system tests."""

    def test_full_lifecycle(self, tmp_path):
        """Complete lifecycle: discover → install → activate → context → deactivate."""
        shop = tmp_path / "lobster-shop"
        state_path = tmp_path / "config" / "skills-state.json"

        # 1. Create a mock skill
        _setup_skill(shop, "alpha",
                      behavior_text="Alpha behavior instructions.",
                      context_text="Alpha domain context.")

        # 2. Parse manifest
        manifest = _parse_manifest(shop / "alpha")
        assert manifest is not None
        assert manifest["skill"]["name"] == "alpha"

        # 3. List available — should show as not installed.
        # Pass config_dir="" to isolate from any user config overlay skills.
        available = list_available_skills(repo_dir=tmp_path, state_path=state_path, config_dir="")
        assert len(available) == 1
        assert available[0]["name"] == "alpha"
        assert available[0]["installed"] is False

        # 4. Mark installed
        mark_installed("alpha", "1.0.0", state_path=state_path)
        available = list_available_skills(repo_dir=tmp_path, state_path=state_path, config_dir="")
        assert available[0]["installed"] is True
        assert available[0]["active"] is False

        # 5. Activate
        result = activate_skill("alpha", mode="always",
                                state_path=state_path, repo_dir=tmp_path)
        assert "activated" in result

        active = get_active_skills(state_path)
        assert "alpha" in active

        # 6. Get context — should contain behavior + context
        context = get_skill_context(repo_dir=tmp_path, state_path=state_path, config_dir="")
        assert "Alpha behavior instructions." in context
        assert "Alpha domain context." in context
        assert "## Skill: alpha" in context

        # 7. Deactivate
        deactivate_skill("alpha", state_path=state_path)
        active = get_active_skills(state_path)
        assert "alpha" not in active

        # 8. Context no longer includes it
        context = get_skill_context(repo_dir=tmp_path, state_path=state_path, config_dir="")
        assert "Alpha behavior" not in context

    def test_multi_skill_composition(self, tmp_path):
        """Two active skills compose in priority order."""
        shop = tmp_path / "lobster-shop"
        state_path = tmp_path / "config" / "skills-state.json"

        _setup_skill(shop, "low-pri", behavior_text="LOW_PRI_BEHAVIOR", priority=10)
        _setup_skill(shop, "high-pri", behavior_text="HIGH_PRI_BEHAVIOR", priority=90)

        activate_skill("low-pri", state_path=state_path, repo_dir=tmp_path)
        activate_skill("high-pri", state_path=state_path, repo_dir=tmp_path)

        context = get_skill_context(repo_dir=tmp_path, state_path=state_path)

        assert "LOW_PRI_BEHAVIOR" in context
        assert "HIGH_PRI_BEHAVIOR" in context

        # Lower priority appears first
        low_pos = context.index("LOW_PRI_BEHAVIOR")
        high_pos = context.index("HIGH_PRI_BEHAVIOR")
        assert low_pos < high_pos

    def test_conditional_composition(self, tmp_path):
        """with-<other>.md files only included when co-active."""
        shop = tmp_path / "lobster-shop"
        state_path = tmp_path / "config" / "skills-state.json"

        # Skill A has conditional behavior for when "bravo" is active
        skill_a = _setup_skill(shop, "alpha", behavior_text="Alpha base.")
        (skill_a / "behavior" / "with-bravo.md").write_text("Alpha+Bravo synergy.")

        _setup_skill(shop, "bravo", behavior_text="Bravo base.")

        # Activate only alpha — with-bravo.md should NOT be included
        activate_skill("alpha", state_path=state_path, repo_dir=tmp_path)
        context = get_skill_context(repo_dir=tmp_path, state_path=state_path)
        assert "Alpha base." in context
        assert "Alpha+Bravo synergy." not in context

        # Now activate bravo too — with-bravo.md should be included
        activate_skill("bravo", state_path=state_path, repo_dir=tmp_path)
        context = get_skill_context(repo_dir=tmp_path, state_path=state_path)
        assert "Alpha base." in context
        assert "Alpha+Bravo synergy." in context
        assert "Bravo base." in context

    def test_preferences_with_defaults_and_overrides(self, tmp_path):
        """Preferences merge defaults with user overrides."""
        shop = tmp_path / "lobster-shop"
        state_path = tmp_path / "config" / "skills-state.json"

        _setup_skill(shop, "configurable",
                      prefs_defaults={"port": 9377, "auto_close": False})

        # Get defaults
        prefs = get_skill_preferences("configurable",
                                       state_path=state_path, repo_dir=tmp_path)
        assert prefs["port"] == 9377
        assert prefs["auto_close"] is False

        # Set override
        set_skill_preference("configurable", "port", 8080,
                              state_path=state_path, repo_dir=tmp_path)

        # Verify merged
        prefs = get_skill_preferences("configurable",
                                       state_path=state_path, repo_dir=tmp_path)
        assert prefs["port"] == 8080
        assert prefs["auto_close"] is False  # Still default

    def test_camofox_toml_and_json_compat(self, tmp_path):
        """Both skill.toml and skill.json are parseable for camofox."""
        # Test with the real camofox-browser directory
        camofox_dir = Path(__file__).parent.parent.parent / "lobster-shop" / "camofox-browser"
        if not camofox_dir.exists():
            pytest.skip("camofox-browser directory not found in repo")

        manifest = _parse_manifest(camofox_dir)
        assert manifest is not None
        assert manifest["skill"]["name"] == "camofox-browser"

    def test_deactivate_removes_from_context(self, tmp_path):
        """Deactivated skill is excluded from assembled context."""
        shop = tmp_path / "lobster-shop"
        state_path = tmp_path / "config" / "skills-state.json"

        _setup_skill(shop, "removable", behavior_text="REMOVABLE_CONTENT")
        _setup_skill(shop, "keeper", behavior_text="KEEPER_CONTENT")

        activate_skill("removable", state_path=state_path, repo_dir=tmp_path)
        activate_skill("keeper", state_path=state_path, repo_dir=tmp_path)

        context = get_skill_context(repo_dir=tmp_path, state_path=state_path)
        assert "REMOVABLE_CONTENT" in context
        assert "KEEPER_CONTENT" in context

        deactivate_skill("removable", state_path=state_path)

        context = get_skill_context(repo_dir=tmp_path, state_path=state_path)
        assert "REMOVABLE_CONTENT" not in context
        assert "KEEPER_CONTENT" in context

    def test_empty_state_returns_empty_context(self, tmp_path):
        """No active skills returns empty context string."""
        state_path = tmp_path / "config" / "skills-state.json"
        context = get_skill_context(repo_dir=tmp_path, state_path=state_path)
        assert context == ""
