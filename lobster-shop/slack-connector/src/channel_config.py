"""
Slack Connector — Per-Channel Configuration.

Loads channels.yaml and provides pure routing decisions based on channel
mode, event type, and mention status.

Design principles:
- Config parsing is isolated to load/reload; everything else is pure lookups.
- Immutable snapshots: reload() atomically replaces the config dict.
- All decision functions are pure: (config_snapshot, inputs) -> decision.
- Missing config file gracefully falls back to sensible defaults.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Optional

import yaml

log = logging.getLogger("slack-channel-config")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VALID_MODES = frozenset({"monitor", "respond", "full", "ignore"})

_DEFAULT_CONFIG_PATH = Path(
    os.environ.get("LOBSTER_WORKSPACE", Path.home() / "lobster-workspace")
) / "slack-connector" / "config" / "channels.yaml"

_BUILTIN_DEFAULTS: dict[str, Any] = {
    "mode": "monitor",
    "log_raw": True,
    "log_files": True,
    "log_reactions": True,
    "respond_to_mentions": True,
    "respond_to_dms": True,
    "llm_routing": False,
}


# ---------------------------------------------------------------------------
# Pure functions — config parsing and routing decisions
# ---------------------------------------------------------------------------


def parse_config(raw: dict[str, Any]) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Parse raw YAML dict into (defaults, channels_by_id).

    Pure function: takes parsed YAML, returns structured config.
    Returns a tuple of (merged defaults dict, {channel_id: channel_dict}).
    """
    file_defaults = raw.get("defaults", {})
    merged_defaults = {**_BUILTIN_DEFAULTS, **file_defaults}

    channels_list = raw.get("channels", []) or []
    channels_by_id: dict[str, dict[str, Any]] = {}

    for entry in channels_list:
        channel_id = entry.get("id", "")
        if not channel_id:
            continue
        # Merge: builtin defaults < file defaults < per-channel overrides
        merged = {**merged_defaults, **entry}
        # Validate mode
        if merged.get("mode") not in VALID_MODES:
            log.warning(
                "Channel %s has invalid mode %r, falling back to 'monitor'",
                channel_id,
                merged.get("mode"),
            )
            merged["mode"] = "monitor"
        channels_by_id[channel_id] = merged

    return merged_defaults, channels_by_id


def resolve_channel(
    channel_id: str,
    defaults: dict[str, Any],
    channels_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Look up effective config for a channel.

    Pure function: returns the channel-specific config if it exists,
    otherwise the merged defaults.
    """
    return channels_by_id.get(channel_id, defaults)


def should_route_to_llm(
    *,
    channel_cfg: dict[str, Any],
    event_type: str,
    is_mention: bool,
    is_dm: bool,
) -> bool:
    """Decide whether an event should be routed to the LLM inbox.

    Pure function: takes channel config + event metadata, returns bool.

    Routing rules by mode:
      monitor → never route
      ignore  → never route
      respond → route if @mention or DM
      full    → always route
    """
    mode = channel_cfg.get("mode", "monitor")

    if mode in ("monitor", "ignore"):
        return False

    if mode == "full":
        return True

    if mode == "respond":
        return is_mention or is_dm

    # Unknown mode — defensive fallback
    return False


def get_channel_triggers(
    channel_cfg: dict[str, Any],
) -> list[str]:
    """Extract trigger rule names from a channel config.

    Pure function. Returns empty list for 'ignore' mode or missing triggers.
    """
    mode = channel_cfg.get("mode", "monitor")
    if mode == "ignore":
        return []

    triggers = channel_cfg.get("triggers", []) or []
    return [
        t.get("rule", "") if isinstance(t, dict) else str(t)
        for t in triggers
        if (t.get("rule", "") if isinstance(t, dict) else str(t))
    ]


# ---------------------------------------------------------------------------
# ChannelConfig — stateful wrapper with hot-reload
# ---------------------------------------------------------------------------


class ChannelConfig:
    """Per-channel routing configuration with hot-reload from YAML.

    State is limited to the loaded config snapshot. All decision logic
    delegates to pure functions above.
    """

    def __init__(self, config_path: Optional[str] = None) -> None:
        self._config_path = Path(config_path) if config_path else _DEFAULT_CONFIG_PATH
        self._defaults: dict[str, Any] = {**_BUILTIN_DEFAULTS}
        self._channels_by_id: dict[str, dict[str, Any]] = {}
        self.reload()

    def reload(self) -> None:
        """Reload config from disk. Atomic replacement of internal state.

        If the file is missing or unparseable, falls back to builtin defaults.
        """
        if not self._config_path.exists():
            log.info(
                "Channel config not found at %s, using builtin defaults",
                self._config_path,
            )
            self._defaults = {**_BUILTIN_DEFAULTS}
            self._channels_by_id = {}
            return

        try:
            raw = yaml.safe_load(self._config_path.read_text()) or {}
            defaults, channels_by_id = parse_config(raw)
            # Atomic swap
            self._defaults = defaults
            self._channels_by_id = channels_by_id
            log.info(
                "Loaded channel config: %d channels from %s",
                len(channels_by_id),
                self._config_path,
            )
        except Exception:
            log.exception("Failed to parse channel config at %s", self._config_path)
            # Keep previous config on parse failure

    def get_channel_mode(self, channel_id: str) -> str:
        """Return the effective mode for a channel."""
        cfg = resolve_channel(channel_id, self._defaults, self._channels_by_id)
        return cfg.get("mode", "monitor")

    def should_route_to_llm(
        self,
        channel_id: str,
        event_type: str = "message",
        is_mention: bool = False,
        is_dm: bool = False,
    ) -> bool:
        """Decide whether to route this event to the LLM inbox."""
        cfg = resolve_channel(channel_id, self._defaults, self._channels_by_id)
        return should_route_to_llm(
            channel_cfg=cfg,
            event_type=event_type,
            is_mention=is_mention,
            is_dm=is_dm,
        )

    def get_channel_triggers(self, channel_id: str) -> list[str]:
        """Return trigger rule names for a channel."""
        cfg = resolve_channel(channel_id, self._defaults, self._channels_by_id)
        return get_channel_triggers(cfg)
