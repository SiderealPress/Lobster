"""
Owner Identity: read/write the owner.toml identity file.

This is the seed layer for the user model — it provides the anchor identity
that the rest of the model hangs off of.

File location: ~/lobster-config/owner.toml
Format: TOML (parsed manually without external deps for compatibility)

Depends on: nothing (stdlib only).
"""

import os
import re
from pathlib import Path
from typing import Any


# Default location
_DEFAULT_OWNER_FILE = Path.home() / "lobster-config" / "owner.toml"


def _parse_toml_simple(text: str) -> dict[str, Any]:
    """
    Minimal TOML parser for owner.toml — handles simple key = "value" and sections.
    Does not support arrays, nested tables, or multi-line values.
    Falls back gracefully on parse errors.
    """
    result: dict[str, Any] = {}
    current_section: dict[str, Any] = result
    current_section_name: str | None = None

    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        # Section header
        section_match = re.match(r"^\[(\w+)\]$", line)
        if section_match:
            section_name = section_match.group(1)
            result[section_name] = {}
            current_section = result[section_name]
            current_section_name = section_name
            continue

        # Key-value pair
        kv_match = re.match(r'^(\w+)\s*=\s*"?([^"#]*)"?\s*(?:#.*)?$', line)
        if kv_match:
            key = kv_match.group(1)
            value = kv_match.group(2).strip()
            current_section[key] = value

    return result


def _format_toml_simple(data: dict[str, Any]) -> str:
    """Format a dict to minimal TOML for owner.toml."""
    lines = ["# Lobster instance owner identity", "# This file contains NO secrets.\n"]

    for section_name, section_data in data.items():
        if not isinstance(section_data, dict):
            continue
        lines.append(f"[{section_name}]")
        for key, value in section_data.items():
            lines.append(f'{key} = "{value}"')
        lines.append("")

    return "\n".join(lines)


def read_owner(owner_file: Path | None = None) -> dict[str, Any]:
    """
    Read owner.toml and return parsed data.

    Returns an empty dict if the file doesn't exist (graceful degradation).
    Structure:
    {
      "owner": {"email": ..., "telegram_username": ..., "telegram_chat_id": ..., "name": ...},
      "instance": {"id": ..., "hostname": ..., "plan": ..., ...}
    }
    """
    path = owner_file or _DEFAULT_OWNER_FILE
    if not path.exists():
        return {}
    try:
        return _parse_toml_simple(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_owner(data: dict[str, Any], owner_file: Path | None = None) -> None:
    """Write owner.toml atomically."""
    path = owner_file or _DEFAULT_OWNER_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    content = _format_toml_simple(data)
    tmp = path.parent / f".owner-{os.getpid()}.toml.tmp"
    try:
        tmp.write_text(content, encoding="utf-8")
        tmp.rename(path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except Exception:
                pass


def get_owner_name(owner_file: Path | None = None) -> str | None:
    """Return the owner's display name, or None if not set."""
    data = read_owner(owner_file)
    return data.get("owner", {}).get("name") or None


def get_owner_telegram_chat_id(owner_file: Path | None = None) -> str | None:
    """Return the owner's Telegram chat_id, or None if not set."""
    data = read_owner(owner_file)
    return data.get("owner", {}).get("telegram_chat_id") or None


def get_owner_id(owner_file: Path | None = None) -> str | None:
    """
    Return a stable owner identifier for use as model anchor.
    Prefers telegram_chat_id, falls back to email, then instance id.
    """
    data = read_owner(owner_file)
    owner = data.get("owner", {})
    instance = data.get("instance", {})

    return (
        owner.get("telegram_chat_id")
        or owner.get("email")
        or instance.get("id")
        or None
    )


def ensure_owner_toml(
    name: str | None = None,
    telegram_chat_id: str | None = None,
    owner_file: Path | None = None,
) -> dict[str, Any]:
    """
    Ensure owner.toml exists. Creates it with provided info if absent.
    Returns the parsed data.
    """
    path = owner_file or _DEFAULT_OWNER_FILE
    if path.exists():
        return read_owner(path)

    # Try to infer from environment
    env_chat_id = os.environ.get("TELEGRAM_ALLOWED_USERS", "").split(",")[0].strip()

    data = {
        "owner": {
            "name": name or "unknown",
            "telegram_chat_id": telegram_chat_id or env_chat_id or "",
        }
    }
    write_owner(data, path)
    return data
