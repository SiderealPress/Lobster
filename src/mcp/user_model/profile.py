"""
PRD File Layer: structured profile files at ~/lobster-user-profile/.

Provides LLM-readable user context through YAML front matter markdown files:
  - profile.md   — identity, communication style, timezone
  - goals.md     — active goals with confidence and evidence
  - preferences.md — domain-grouped preference rules

Functions:
  get_profile_dir()      → Path, creates if needed
  read_profile_file()    → {meta: dict, body: str}
  write_profile_file()   → atomic write with YAML front matter
  read_all_profiles()    → {filename: {meta, body}}
  get_compact_context()  → ~150 token header string
  apply_goal_decay()     → decay unconfirmed goal confidence

No external dependencies — stdlib only. YAML parsing is manual (no PyYAML).
"""

import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any


_PROFILE_DIR = Path.home() / "lobster-user-profile"
_PROFILE_FILES = ["profile.md", "goals.md", "preferences.md"]


# ---------------------------------------------------------------------------
# Directory and file management
# ---------------------------------------------------------------------------

def get_profile_dir() -> Path:
    """Return the profile directory, creating it if needed."""
    _PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    return _PROFILE_DIR


def read_profile_file(path: Path) -> dict[str, Any]:
    """
    Read a profile file with YAML front matter.

    Returns: {"meta": dict, "body": str}
    If file doesn't exist, returns {"meta": {}, "body": ""}.
    """
    if not path.exists():
        return {"meta": {}, "body": ""}

    try:
        content = path.read_text(encoding="utf-8")
    except Exception:
        return {"meta": {}, "body": ""}

    return _parse_front_matter(content)


def write_profile_file(path: Path, meta: dict[str, Any], body: str) -> None:
    """
    Write a profile file with YAML front matter. Atomic write via temp + rename.
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    lines = ["---"]
    for key, value in meta.items():
        lines.append(f"{key}: {_format_yaml_value(value)}")
    lines.append("---")
    lines.append("")
    lines.append(body.rstrip())
    lines.append("")  # Trailing newline

    content = "\n".join(lines)

    tmp = path.parent / f".{path.name}.{os.getpid()}.tmp"
    try:
        tmp.write_text(content, encoding="utf-8")
        tmp.rename(path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except Exception:
                pass


def read_all_profiles() -> dict[str, dict[str, Any]]:
    """
    Read all profile files.

    Returns: {filename: {"meta": dict, "body": str}} for profile.md, goals.md, preferences.md.
    """
    profile_dir = get_profile_dir()
    result = {}
    for filename in _PROFILE_FILES:
        path = profile_dir / filename
        result[filename] = read_profile_file(path)
    return result


# ---------------------------------------------------------------------------
# Compact context for system prompt injection
# ---------------------------------------------------------------------------

def get_compact_context() -> str:
    """
    Return a compact (~150 token) header summarizing the user profile.
    Suitable for injection into system prompts.
    Returns empty string if no profile files exist.
    """
    profile_dir = get_profile_dir()
    profile_path = profile_dir / "profile.md"
    goals_path = profile_dir / "goals.md"
    prefs_path = profile_dir / "preferences.md"

    parts = []

    # Profile identity
    profile = read_profile_file(profile_path)
    if profile["meta"]:
        name = profile["meta"].get("name", "")
        tz = profile["meta"].get("timezone", "")
        style = profile["meta"].get("communication_style", "")
        if name:
            identity = f"User: {name}"
            if tz:
                identity += f" ({tz})"
            parts.append(identity)
        if style:
            parts.append(f"Style: {style}")

    # Active goals (top 3 by confidence)
    goals = read_profile_file(goals_path)
    if goals["body"]:
        goal_lines = _extract_goals_compact(goals["body"])
        if goal_lines:
            parts.append("Goals: " + "; ".join(goal_lines[:3]))

    # Key preferences (top 3)
    prefs = read_profile_file(prefs_path)
    if prefs["body"]:
        pref_lines = _extract_prefs_compact(prefs["body"])
        if pref_lines:
            parts.append("Prefs: " + "; ".join(pref_lines[:3]))

    return " | ".join(parts) if parts else ""


# ---------------------------------------------------------------------------
# Goal decay
# ---------------------------------------------------------------------------

def apply_goal_decay(decay_per_week: float = 0.05) -> int:
    """
    Read goals.md, decay confidence on unconfirmed goals, write back.
    Returns number of goals decayed.
    """
    profile_dir = get_profile_dir()
    goals_path = profile_dir / "goals.md"
    goals = read_profile_file(goals_path)

    if not goals["body"]:
        return 0

    body = goals["body"]
    decayed = 0

    # Find confidence values in the body and decay them
    def _decay_confidence(match: re.Match) -> str:
        nonlocal decayed
        conf = float(match.group(1))
        new_conf = max(0.1, conf - decay_per_week)
        if new_conf < conf:
            decayed += 1
        return f"confidence: {new_conf:.2f}"

    updated_body = re.sub(
        r"confidence:\s*([\d.]+)",
        _decay_confidence,
        body,
    )

    if decayed > 0:
        write_profile_file(goals_path, goals["meta"], updated_body)

    return decayed


# ---------------------------------------------------------------------------
# YAML front matter parsing (no PyYAML dependency)
# ---------------------------------------------------------------------------

def _parse_front_matter(content: str) -> dict[str, Any]:
    """
    Parse YAML front matter delimited by --- lines.
    Returns {"meta": dict, "body": str}.
    """
    content = content.strip()
    if not content.startswith("---"):
        return {"meta": {}, "body": content}

    # Find closing ---
    end_idx = content.find("---", 3)
    if end_idx == -1:
        return {"meta": {}, "body": content}

    front_matter = content[3:end_idx].strip()
    body = content[end_idx + 3:].strip()

    meta = {}
    for line in front_matter.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        colon_idx = line.find(":")
        if colon_idx == -1:
            continue
        key = line[:colon_idx].strip()
        value = line[colon_idx + 1:].strip()
        # Strip quotes
        if (value.startswith('"') and value.endswith('"')) or \
           (value.startswith("'") and value.endswith("'")):
            value = value[1:-1]
        meta[key] = value

    return {"meta": meta, "body": body}


def _format_yaml_value(value: Any) -> str:
    """Format a value for YAML front matter output."""
    if isinstance(value, str):
        # Quote strings that contain special chars
        if any(c in value for c in ":#{}[]|>&*!%@`"):
            return f'"{value}"'
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return str(value)


def _extract_goals_compact(body: str) -> list[str]:
    """Extract goal titles from goals.md body for compact context."""
    goals = []
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("## "):
            goals.append(line[3:].strip())
    return goals


def _extract_prefs_compact(body: str) -> list[str]:
    """Extract top preference rules from preferences.md body."""
    prefs = []
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("- ") and len(line) > 5:
            prefs.append(line[2:].strip())
    return prefs
