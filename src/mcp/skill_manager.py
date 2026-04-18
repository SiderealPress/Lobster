"""
Skill Manager — Composable Skill Layering System

Manages skills as four-dimensional units (behavior + context + preferences + tooling)
that layer and compose at runtime.

State file: ~/messages/config/skills-state.json
Format:
    {
      "skills": {
        "camofox-browser": {
          "installed": true, "active": true,
          "activation_mode": "always",
          "version": "1.0.0", "priority": 50,
          "installed_at": "...", "preferences": {}
        }
      }
    }

Design principles (following tracker.py patterns):
- Pure functions where possible; side effects only in state mutation
- Atomic writes via write-to-temp-then-rename
- File locking for concurrent access
- Graceful degradation: missing files treated as empty state
"""

import fcntl
import json
import os
import tempfile
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# Default paths
_MESSAGES_DIR = Path(os.environ.get("LOBSTER_MESSAGES", Path.home() / "messages"))
_DEFAULT_STATE_PATH = _MESSAGES_DIR / "config" / "skills-state.json"
_REPO_DIR = Path(os.environ.get("LOBSTER_INSTALL_DIR", Path.home() / "lobster"))
_CONFIG_DIR = os.environ.get("LOBSTER_CONFIG_DIR", "")


# =============================================================================
# Pure helpers — file I/O
# =============================================================================

def _empty_store() -> dict:
    """Return a fresh empty skills store."""
    return {"skills": {}}


def _read_store(path: Path) -> dict:
    """Read and parse the skills-state JSON file.

    Returns an empty store if the file is missing or malformed. Never raises.
    """
    try:
        content = path.read_text(encoding="utf-8")
        data = json.loads(content)
        if not isinstance(data, dict) or not isinstance(data.get("skills"), dict):
            return _empty_store()
        return data
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return _empty_store()


def _atomic_write(path: Path, data: dict) -> None:
    """Atomically write data to path as pretty-printed JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(data, indent=2)
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.rename(tmp_path, str(path))
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _with_lock(path: Path, fn):
    """Execute fn(store) -> store under a file lock, then write result."""
    lock_path = path.with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            store = _read_store(path)
            updated = fn(store)
            _atomic_write(path, updated)
            return updated
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


# =============================================================================
# Manifest parsing
# =============================================================================

def _parse_manifest(skill_dir: Path) -> dict | None:
    """Parse skill.toml (preferred) or skill.json (compat) from a skill directory.

    Returns parsed manifest dict or None if neither exists or both are malformed.
    """
    # Prefer TOML
    toml_path = skill_dir / "skill.toml"
    if toml_path.exists():
        try:
            with open(toml_path, "rb") as f:
                return tomllib.load(f)
        except Exception:
            pass  # Fall through to JSON

    # Fallback to JSON
    json_path = skill_dir / "skill.json"
    if json_path.exists():
        try:
            with open(json_path, "r") as f:
                data = json.load(f)
            # Normalize JSON format to match TOML structure
            if "name" in data and "skill" not in data:
                # Flat JSON format — wrap in [skill] section for consistency
                return {"skill": data}
            return data
        except Exception:
            pass

    return None


# =============================================================================
# Skill directory resolution
# =============================================================================

def _resolve_skill_dirs(
    repo_dir: Path | None = None,
    config_dir: str | None = None,
) -> list[Path]:
    """Scan lobster-shop/ and optional private overlay for skill directories.

    Returns list of Paths to skill directories (each containing a manifest).
    Private overlay skills override repo skills with the same name.
    """
    repo_dir = repo_dir or _REPO_DIR
    config_dir = _CONFIG_DIR if config_dir is None else config_dir

    skills: dict[str, Path] = {}

    # Scan repo lobster-shop/
    shop_dir = repo_dir / "lobster-shop"
    if shop_dir.is_dir():
        for entry in sorted(shop_dir.iterdir()):
            if entry.is_dir() and not entry.name.startswith("."):
                manifest = _parse_manifest(entry)
                if manifest:
                    name = _extract_skill_name(manifest, entry.name)
                    skills[name] = entry

    # Overlay from private config
    if config_dir:
        overlay_dir = Path(config_dir) / "skills"
        if overlay_dir.is_dir():
            for entry in sorted(overlay_dir.iterdir()):
                if entry.is_dir() and not entry.name.startswith("."):
                    manifest = _parse_manifest(entry)
                    if manifest:
                        name = _extract_skill_name(manifest, entry.name)
                        skills[name] = entry  # Override repo version

    return list(skills.values())


def _extract_skill_name(manifest: dict, fallback: str) -> str:
    """Extract the skill name from a parsed manifest."""
    skill_section = manifest.get("skill", {})
    return skill_section.get("name", fallback)


# =============================================================================
# Behavior and context assembly
# =============================================================================

def _merge_behavior(skill_dir: Path, active_skills: list[str]) -> str:
    """Read behavior/*.md files from a skill, including conditional with-<other>.md.

    Conditional files (behavior/with-<other-skill>.md) are only included
    when the named skill is also active.
    """
    behavior_dir = skill_dir / "behavior"
    if not behavior_dir.is_dir():
        return ""

    parts: list[str] = []

    for md_file in sorted(behavior_dir.glob("*.md")):
        name = md_file.stem

        # Conditional composition: with-<other-skill>.md
        if name.startswith("with-"):
            other_skill = name[5:]  # Remove "with-" prefix
            if other_skill not in active_skills:
                continue

        try:
            content = md_file.read_text(encoding="utf-8").strip()
            if content:
                parts.append(content)
        except OSError:
            continue

    return "\n\n".join(parts)


def _read_context(skill_dir: Path) -> str:
    """Read context/*.md files from a skill."""
    context_dir = skill_dir / "context"
    if not context_dir.is_dir():
        return ""

    parts: list[str] = []
    for md_file in sorted(context_dir.glob("*.md")):
        try:
            content = md_file.read_text(encoding="utf-8").strip()
            if content:
                parts.append(content)
        except OSError:
            continue

    return "\n\n".join(parts)


def _read_preferences_defaults(skill_dir: Path) -> dict:
    """Read preferences/defaults.toml from a skill."""
    defaults_path = skill_dir / "preferences" / "defaults.toml"
    if not defaults_path.exists():
        return {}
    try:
        with open(defaults_path, "rb") as f:
            return tomllib.load(f)
    except Exception:
        return {}


def _read_preferences_schema(skill_dir: Path) -> dict:
    """Read preferences/schema.toml from a skill."""
    schema_path = skill_dir / "preferences" / "schema.toml"
    if not schema_path.exists():
        return {}
    try:
        with open(schema_path, "rb") as f:
            return tomllib.load(f)
    except Exception:
        return {}


def _assemble_context(
    skill_dirs: dict[str, Path],
    active_skills: list[str],
    state: dict,
) -> str:
    """Assemble composite context from all active skills, ordered by priority.

    Returns markdown string with section headers per skill.
    """
    if not active_skills:
        return ""

    # Build (priority, name, dir) tuples for ordering
    entries: list[tuple[int, str, Path]] = []
    for name in active_skills:
        if name not in skill_dirs:
            continue
        skill_state = state.get("skills", {}).get(name, {})
        priority = skill_state.get("priority", 50)
        entries.append((priority, name, skill_dirs[name]))

    # Sort by priority (lower first — higher priority applied later, wins conflicts)
    entries.sort(key=lambda e: e[0])

    sections: list[str] = []
    for priority, name, skill_dir in entries:
        parts: list[str] = []

        behavior = _merge_behavior(skill_dir, active_skills)
        if behavior:
            parts.append(behavior)

        context = _read_context(skill_dir)
        if context:
            parts.append(context)

        if parts:
            section = f"## Skill: {name}\n\n" + "\n\n".join(parts)
            sections.append(section)

    if not sections:
        return ""

    return "# Active Skills\n\n" + "\n\n---\n\n".join(sections)


# =============================================================================
# Public API
# =============================================================================

def list_available_skills(
    repo_dir: Path | None = None,
    config_dir: str | None = None,
    state_path: Path = _DEFAULT_STATE_PATH,
) -> list[dict]:
    """List all skills with install/active status.

    Returns list of dicts with: name, version, description, installed, active,
    activation_mode, priority, source_dir.
    """
    skill_dirs = _resolve_skill_dirs(repo_dir, config_dir)
    state = _read_store(state_path)

    result = []
    for skill_dir in skill_dirs:
        manifest = _parse_manifest(skill_dir)
        if not manifest:
            continue

        skill_info = manifest.get("skill", {})
        name = skill_info.get("name", skill_dir.name)
        skill_state = state.get("skills", {}).get(name, {})

        result.append({
            "name": name,
            "version": skill_info.get("version", "0.0.0"),
            "description": skill_info.get("description", ""),
            "author": skill_info.get("author", ""),
            "category": skill_info.get("category", "tool"),
            "installed": skill_state.get("installed", False),
            "active": skill_state.get("active", False),
            "activation_mode": skill_state.get("activation_mode", "always"),
            "priority": skill_state.get("priority", 50),
            "source_dir": str(skill_dir),
        })

    return result


def get_active_skills(state_path: Path = _DEFAULT_STATE_PATH) -> list[str]:
    """Return names of all active skills."""
    state = _read_store(state_path)
    return [
        name for name, info in state.get("skills", {}).items()
        if info.get("active", False)
    ]


def get_skill_context(
    repo_dir: Path | None = None,
    config_dir: str | None = None,
    state_path: Path = _DEFAULT_STATE_PATH,
) -> str:
    """THE KEY FUNCTION — assemble composite context from all active skills.

    Returns markdown string ready for injection into Claude's context.
    Empty string if no skills are active.
    """
    repo_dir = repo_dir or _REPO_DIR
    config_dir = _CONFIG_DIR if config_dir is None else config_dir

    state = _read_store(state_path)
    active = get_active_skills(state_path)

    if not active:
        return ""

    # Build name -> dir mapping
    skill_dirs: dict[str, Path] = {}
    for skill_dir in _resolve_skill_dirs(repo_dir, config_dir):
        manifest = _parse_manifest(skill_dir)
        if manifest:
            name = _extract_skill_name(manifest, skill_dir.name)
            skill_dirs[name] = skill_dir

    return _assemble_context(skill_dirs, active, state)


def activate_skill(
    skill_name: str,
    mode: str = "always",
    state_path: Path = _DEFAULT_STATE_PATH,
    repo_dir: Path | None = None,
    config_dir: str | None = None,
) -> str:
    """Mark a skill as active. Returns status message."""
    valid_modes = {"always", "triggered", "contextual"}
    if mode not in valid_modes:
        return f"Error: invalid mode '{mode}'. Must be one of: {', '.join(sorted(valid_modes))}"

    # Verify skill exists
    repo_dir = repo_dir or _REPO_DIR
    config_dir = _CONFIG_DIR if config_dir is None else config_dir
    available = {s["name"] for s in list_available_skills(repo_dir, config_dir, state_path)}
    if skill_name not in available:
        return f"Error: skill '{skill_name}' not found."

    def update(store: dict) -> dict:
        skills = store.get("skills", {})
        existing = skills.get(skill_name, {})
        existing["active"] = True
        existing["activation_mode"] = mode
        if not existing.get("installed"):
            existing["installed"] = True
            existing["installed_at"] = datetime.now(timezone.utc).isoformat()
        if "priority" not in existing:
            existing["priority"] = 50
        if "preferences" not in existing:
            existing["preferences"] = {}
        skills[skill_name] = existing
        return {"skills": skills}

    _with_lock(state_path, update)
    return f"Skill '{skill_name}' activated (mode: {mode})."


def deactivate_skill(
    skill_name: str,
    state_path: Path = _DEFAULT_STATE_PATH,
) -> str:
    """Mark a skill as inactive. Returns status message."""
    def update(store: dict) -> dict:
        skills = store.get("skills", {})
        if skill_name not in skills:
            return store  # No-op
        skills[skill_name]["active"] = False
        return {"skills": skills}

    _with_lock(state_path, update)
    return f"Skill '{skill_name}' deactivated."


def mark_installed(
    skill_name: str,
    version: str,
    state_path: Path = _DEFAULT_STATE_PATH,
) -> None:
    """Record that a skill has been installed."""
    def update(store: dict) -> dict:
        skills = store.get("skills", {})
        existing = skills.get(skill_name, {})
        existing["installed"] = True
        existing["version"] = version
        existing["installed_at"] = datetime.now(timezone.utc).isoformat()
        if "active" not in existing:
            existing["active"] = False
        if "activation_mode" not in existing:
            existing["activation_mode"] = "always"
        if "priority" not in existing:
            existing["priority"] = 50
        if "preferences" not in existing:
            existing["preferences"] = {}
        skills[skill_name] = existing
        return {"skills": skills}

    _with_lock(state_path, update)


def get_skill_preferences(
    skill_name: str,
    state_path: Path = _DEFAULT_STATE_PATH,
    repo_dir: Path | None = None,
    config_dir: str | None = None,
) -> dict:
    """Get merged preferences (defaults + user overrides) for a skill."""
    repo_dir = repo_dir or _REPO_DIR
    config_dir = _CONFIG_DIR if config_dir is None else config_dir

    # Find the skill directory
    skill_dir = None
    for d in _resolve_skill_dirs(repo_dir, config_dir):
        manifest = _parse_manifest(d)
        if manifest and _extract_skill_name(manifest, d.name) == skill_name:
            skill_dir = d
            break

    # Start with defaults from the skill
    defaults = _read_preferences_defaults(skill_dir) if skill_dir else {}

    # Overlay user overrides from state
    state = _read_store(state_path)
    user_prefs = state.get("skills", {}).get(skill_name, {}).get("preferences", {})

    merged = {**defaults, **user_prefs}
    return merged


def set_skill_preference(
    skill_name: str,
    key: str,
    value: Any,
    state_path: Path = _DEFAULT_STATE_PATH,
    repo_dir: Path | None = None,
    config_dir: str | None = None,
) -> str:
    """Set a preference value for a skill. Validates against schema if available."""
    repo_dir = repo_dir or _REPO_DIR
    config_dir = _CONFIG_DIR if config_dir is None else config_dir

    # Find skill directory for schema validation
    skill_dir = None
    for d in _resolve_skill_dirs(repo_dir, config_dir):
        manifest = _parse_manifest(d)
        if manifest and _extract_skill_name(manifest, d.name) == skill_name:
            skill_dir = d
            break

    # Validate against schema if available
    if skill_dir:
        schema = _read_preferences_schema(skill_dir)
        if schema and key not in schema:
            valid_keys = list(schema.keys())
            return f"Error: unknown preference '{key}'. Valid keys: {', '.join(valid_keys)}"

    def update(store: dict) -> dict:
        skills = store.get("skills", {})
        existing = skills.get(skill_name, {})
        prefs = existing.get("preferences", {})
        prefs[key] = value
        existing["preferences"] = prefs
        skills[skill_name] = existing
        return {"skills": skills}

    _with_lock(state_path, update)
    return f"Set {skill_name}.{key} = {value}"
