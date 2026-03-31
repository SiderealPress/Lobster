"""
session.py — GroupSession state model and JSON persistence.

Tracks per-chat session state for the group chat UX policy:
- Sebastian becomes active only when directly invoked (@mention, /command, reply)
- A session stays open for SESSION_TTL_SECONDS after Sebastian's last reply
- Sessions close early on explicit closure signals ("thanks", "got it", etc.)

All I/O functions are safe to call from multiple threads/processes (atomic write).
"""

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SESSION_TTL_SECONDS = 600  # 10 minutes

# Default session file path — respects LOBSTER_MESSAGES env var
def _default_session_file() -> Path:
    messages_dir = Path(os.environ.get("LOBSTER_MESSAGES", Path.home() / "messages"))
    return messages_dir / "config" / "group-sessions.json"


SESSION_FILE = _default_session_file()

# Phrases that signal the end of a session
_CLOSURE_SIGNALS = frozenset({
    "thanks",
    "thank you",
    "thx",
    "ty",
    "got it",
    "gotcha",
    "👍",
    "perfect",
    "done",
    "all set",
    "that's all",
    "never mind",
    "nevermind",
    "ok thanks",
    "ok thank you",
    "ok ty",
    "cheers",
    "great thanks",
    "great, thanks",
})


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class GroupSession:
    """Tracks an active conversation session in a group chat.

    Attributes:
        chat_id: Telegram group chat ID (negative integer)
        invoker_user_id: Telegram user ID who started this session
        expires_at: When this session expires (UTC)
        active: Whether this session is active (False = explicitly closed)
    """
    chat_id: int
    invoker_user_id: int
    expires_at: datetime
    active: bool = True

    def is_expired(self) -> bool:
        """Return True if the session TTL has passed."""
        return datetime.now(timezone.utc) >= self.expires_at

    def to_dict(self) -> dict:
        """Serialize to JSON-compatible dict."""
        return {
            "chat_id": self.chat_id,
            "invoker_user_id": self.invoker_user_id,
            "expires_at": self.expires_at.isoformat(),
            "active": self.active,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "GroupSession":
        """Deserialize from dict (e.g. loaded from JSON)."""
        expires_at = datetime.fromisoformat(d["expires_at"])
        # Ensure timezone-aware
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        return cls(
            chat_id=int(d["chat_id"]),
            invoker_user_id=int(d["invoker_user_id"]),
            expires_at=expires_at,
            active=bool(d.get("active", True)),
        )


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------

def load_sessions(path: Path = SESSION_FILE) -> dict[int, GroupSession]:
    """Load sessions from JSON file.

    Returns:
        Dict keyed by chat_id (int). Returns empty dict if file doesn't exist.
    """
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        sessions: dict[int, GroupSession] = {}
        for item in raw:
            try:
                session = GroupSession.from_dict(item)
                sessions[session.chat_id] = session
            except (KeyError, ValueError):
                pass  # skip malformed entries
        return sessions
    except (json.JSONDecodeError, OSError):
        return {}


def save_sessions(sessions: dict[int, GroupSession], path: Path = SESSION_FILE) -> None:
    """Persist sessions to JSON atomically (write temp + rename).

    Args:
        sessions: Dict keyed by chat_id
        path: Target file path
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    data = [s.to_dict() for s in sessions.values()]
    # Atomic write: temp file in same dir + rename
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Session state functions
# ---------------------------------------------------------------------------

def get_active_session(
    chat_id: int,
    path: Path = SESSION_FILE,
) -> Optional[GroupSession]:
    """Return the active, non-expired session for chat_id, or None.

    A session is active if:
      - active == True
      - is_expired() == False
    """
    sessions = load_sessions(path)
    session = sessions.get(chat_id)
    if session is None:
        return None
    if not session.active or session.is_expired():
        return None
    return session


def open_session(
    chat_id: int,
    invoker_user_id: int,
    path: Path = SESSION_FILE,
) -> GroupSession:
    """Create or refresh a session for this chat.

    Idempotent: if a session already exists, resets the TTL and updates the
    invoker. Creates a new session if none exists.

    Args:
        chat_id: Telegram group chat ID
        invoker_user_id: User who invoked Sebastian
        path: Session file path

    Returns:
        The created or refreshed GroupSession
    """
    sessions = load_sessions(path)
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=SESSION_TTL_SECONDS)
    session = GroupSession(
        chat_id=chat_id,
        invoker_user_id=invoker_user_id,
        expires_at=expires_at,
        active=True,
    )
    sessions[chat_id] = session
    save_sessions(sessions, path)
    return session


def close_session(chat_id: int, path: Path = SESSION_FILE) -> None:
    """Mark the session for chat_id as inactive and persist.

    No-op if no session exists for this chat_id.
    """
    sessions = load_sessions(path)
    if chat_id in sessions:
        sessions[chat_id].active = False
        save_sessions(sessions, path)


def refresh_session(
    chat_id: int,
    path: Path = SESSION_FILE,
) -> Optional[GroupSession]:
    """Extend the TTL of an active session.

    Resets expires_at to now + SESSION_TTL_SECONDS.

    Returns:
        Updated GroupSession, or None if no active session exists.
    """
    sessions = load_sessions(path)
    session = sessions.get(chat_id)
    if session is None or not session.active:
        return None
    # Refresh even if expired — bot is replying, so extend
    session.expires_at = datetime.now(timezone.utc) + timedelta(seconds=SESSION_TTL_SECONDS)
    sessions[chat_id] = session
    save_sessions(sessions, path)
    return session


def purge_expired_sessions(
    sessions: dict[int, GroupSession],
) -> dict[int, GroupSession]:
    """Return a new dict with expired and inactive sessions removed.

    Pure function — does not modify the input dict, does not touch the file.
    """
    return {
        chat_id: session
        for chat_id, session in sessions.items()
        if session.active and not session.is_expired()
    }


# ---------------------------------------------------------------------------
# Closure signal detection
# ---------------------------------------------------------------------------

def is_closure_signal(text: str | None) -> bool:
    """Return True if the message text signals the end of a session.

    Case-insensitive match against known closure phrases.

    Args:
        text: Message text to check (may be None)

    Returns:
        True if text matches a closure signal
    """
    if not text:
        return False
    normalized = text.strip().lower()
    return normalized in _CLOSURE_SIGNALS
