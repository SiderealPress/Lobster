"""
User Model Subsystem — public API surface.

Only these names are imported by external code (inbox_server.py).
Everything else stays internal to the package.

Usage in inbox_server.py:
    from user_model import create_user_model
    user_model = create_user_model()
    # Per-message: user_model.observe(message_text, message_id)
    # MCP tools: user_model.dispatch(tool_name, args)
"""

import sqlite3
from pathlib import Path
from typing import Any

from .db import open_db, set_metadata_value
from .owner import get_owner_id, ensure_owner_toml, get_owner_timezone
from .tools import USER_MODEL_TOOL_DEFINITIONS, dispatch
from .markdown_sync import sync_all
from .observation import observe_message


class UserModel:
    """
    Facade class for the user model subsystem.

    Injected into inbox_server.py at startup. All interaction
    with the user model goes through this interface.
    """

    def __init__(
        self,
        db_path: Path,
        workspace_path: Path | None = None,
    ) -> None:
        self._db_path = db_path
        self._workspace_path = str(workspace_path) if workspace_path else None
        self._conn: sqlite3.Connection | None = None

    def _get_conn(self) -> sqlite3.Connection:
        """Lazy-initialize and return DB connection."""
        if self._conn is None:
            self._conn = open_db(self._db_path)
            # Ensure owner_id is in metadata
            owner_id = get_owner_id()
            if owner_id:
                set_metadata_value(self._conn, "owner_id", owner_id)
            # Ensure created_at is set
            row = self._conn.execute(
                "SELECT value FROM um_metadata WHERE key = 'created_at'"
            ).fetchone()
            if not row:
                from datetime import datetime, timezone
                set_metadata_value(self._conn, "created_at", datetime.now(timezone.utc).isoformat())
            # Seed bootstrap from owner.toml (non-critical)
            try:
                from .seed import reseed_if_needed
                reseed_if_needed(self._conn)
            except Exception:
                pass
        return self._conn

    def observe(
        self,
        message_text: str,
        message_id: str,
        context: str = "",
        message_ts: Any | None = None,
    ) -> list[str]:
        """
        Extract signals from a message and persist them.
        Returns list of observation IDs.
        Fast, synchronous, <50ms.
        """
        try:
            return observe_message(
                self._get_conn(),
                message_text=message_text,
                message_id=message_id,
                context=context,
                message_ts=message_ts,
            )
        except Exception:
            return []

    def get_context(self, contexts: list[str] | None = None) -> str:
        """
        Return a brief markdown context snippet.
        First tries the pre-computed cache file (fast path, <1ms).
        Falls back to live DB query if cache doesn't exist.
        Returns empty string on failure (graceful degradation).
        """
        # Fast path: read pre-computed cache
        try:
            cache_path = Path(self._workspace_path) / "user-model" / "_context.md" if self._workspace_path else None
            if cache_path and cache_path.exists():
                content = cache_path.read_text(encoding="utf-8")
                if content.strip():
                    return content
        except Exception:
            pass

        # Slow path: live query
        try:
            from .introspection import get_resolved_preferences
            prefs = get_resolved_preferences(
                self._get_conn(), contexts or [], min_confidence=0.6
            )
            if not prefs.get("preferences"):
                return ""
            lines = ["**User preferences (active):**"]
            for p in prefs["preferences"][:5]:
                lines.append(f"- {p['name']}: {p['description'][:80]}")
            return "\n".join(lines)
        except Exception:
            return ""

    def get_user_context(self) -> str:
        """
        Return a compact user profile context string for system prompt injection.
        Graceful degradation — returns empty string on failure.
        """
        try:
            from .profile import get_compact_context
            return get_compact_context()
        except Exception:
            return ""

    def dispatch(self, tool_name: str, args: dict) -> str:
        """
        Dispatch an MCP tool call. Returns JSON string.
        Used by inbox_server.py's _dispatch_tool.
        """
        return dispatch(
            tool_name,
            args,
            self._get_conn(),
            workspace_path=self._workspace_path,
        )

    def health(self) -> dict[str, Any]:
        """
        Health check for observability integration.
        Returns basic stats dict.
        """
        try:
            from .db import get_model_metadata
            meta = get_model_metadata(self._get_conn())
            return {
                "status": "ok",
                "schema_version": meta.schema_version,
                "observation_count": meta.observation_count,
                "preference_node_count": meta.preference_node_count,
                "last_observation_at": meta.last_observation_at.isoformat() if meta.last_observation_at else None,
            }
        except Exception as e:
            return {"status": "error", "error": str(e)}

    def run_consolidation(self, days_since_last_run: int = 1) -> dict[str, Any]:
        """
        Run the full nightly consolidation pipeline.
        Called by scheduled jobs.
        """
        from .inference import run_consolidation
        return run_consolidation(
            self._get_conn(),
            workspace_path=self._workspace_path,
            days_since_last_run=days_since_last_run,
        )

    def sync_files(self) -> dict[str, Any]:
        """Sync the markdown file layer from the DB."""
        return sync_all(self._get_conn(), self._workspace_path)

    @property
    def tool_definitions(self) -> list[dict]:
        """Return MCP tool definitions for registration in list_tools."""
        return USER_MODEL_TOOL_DEFINITIONS

    @property
    def tool_names(self) -> set[str]:
        """Return set of tool names handled by this subsystem."""
        return {t["name"] for t in USER_MODEL_TOOL_DEFINITIONS}


def create_user_model(
    db_path: Path | None = None,
    workspace_path: Path | None = None,
) -> UserModel:
    """
    Factory function: create and initialize the user model.

    Args:
        db_path: Path to memory.db. Defaults to ~/lobster-workspace/data/memory.db.
        workspace_path: Path to lobster-workspace. Defaults to ~/lobster-workspace.

    Returns:
        Initialized UserModel instance.
    """
    if db_path is None:
        db_path = Path.home() / "lobster-workspace" / "data" / "memory.db"

    if workspace_path is None:
        workspace_path = Path.home() / "lobster-workspace"

    # Ensure owner.toml exists (creates default if missing)
    ensure_owner_toml()

    return UserModel(db_path=db_path, workspace_path=workspace_path)


__all__ = [
    "UserModel",
    "create_user_model",
    "USER_MODEL_TOOL_DEFINITIONS",
]
