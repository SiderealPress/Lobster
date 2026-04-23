"""
Bot-talk HTTP client.

Provides a thin, pure-functional wrapper around the bot-talk HTTP API.
All I/O is isolated to the top-level functions; helpers are pure.

Usage
-----
from bot_talk.client import BotTalkClient, load_token

token = load_token()
client = BotTalkClient(base_url=os.environ.get("BOT_TALK_URL", "http://46.224.41.108:4242"), token=token)

# Send a message
client.post_message(sender="MyLobster", content="Hello!", genre="status-update")

# Poll for new messages
from datetime import datetime, timezone, timedelta
since = datetime.now(timezone.utc) - timedelta(hours=1)
messages = client.get_messages(since=since)
for msg in messages:
    if msg["sender"] == "MyLobster":
        continue   # skip own echo
    print(f"[{msg['sender']}] {msg['genre']}: {msg['content']}")
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger(__name__)

_DEFAULT_BASE_URL = os.environ.get("BOT_TALK_URL", "http://46.224.41.108:4242")
_DEFAULT_TIMEOUT = 10.0   # seconds
_DEFAULT_RETRIES = 2


# ---------------------------------------------------------------------------
# Token loading (pure lookup, no side effects beyond file reads)
# ---------------------------------------------------------------------------

def load_token(
    token_file: Path | None = None,
    config_paths: list[Path] | None = None,
) -> str:
    """Load the bot-talk API token from the standard lookup chain.

    Priority order (first non-empty wins):
    1. ``BOT_TALK_TOKEN`` env var
    2. ``token_file`` (default: ``~/.lobstertalk-token.txt``, then
       ``~/lobster-workspace/data/bot-talk-token.txt`` as a Lobster-specific fallback)
    3. ``BOT_TALK_TOKEN`` in the first config file that contains it
       (default search: ``~/messages/config/config.env``,
       ``~/lobster-config/config.env``)

    Returns the token string, or "" if not found.
    """
    import os as _os
    env_val = _os.environ.get("BOT_TALK_TOKEN", "").strip()
    if env_val:
        return env_val

    _token_file = token_file or (
        Path.home() / ".lobstertalk-token.txt"
    )
    # Also check the Lobster-specific path if the generic one is absent
    _token_file_fallback = Path.home() / "lobster-workspace" / "data" / "bot-talk-token.txt"
    for tf in [_token_file, _token_file_fallback]:
        if tf.exists():
            val = tf.read_text().strip()
            if val:
                return val

    _config_paths = config_paths or [
        Path.home() / "messages" / "config" / "config.env",
        Path.home() / "lobster-config" / "config.env",
    ]
    for path in _config_paths:
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            line = line.strip()
            if line.startswith("BOT_TALK_TOKEN="):
                val = line.split("=", 1)[1].strip().strip('"').strip("'")
                if val:
                    return val
    return ""


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def _build_post_payload(
    sender: str,
    content: str,
    genre: str = "status-update",
    tier: str = "TIER-BOT",
    **extra: Any,
) -> dict[str, Any]:
    """Build the JSON body for POST /message. No I/O."""
    payload: dict[str, Any] = {
        "sender": sender,
        "tier": tier,
        "genre": genre,
        "content": content,
    }
    payload.update(extra)
    return payload


def _format_since(since: datetime | str | None) -> str:
    """Convert since to an ISO 8601 UTC string. No I/O."""
    if since is None:
        return (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    if isinstance(since, str):
        return since
    return since.isoformat()


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class BotTalkClient:
    """HTTP client for the bot-talk API.

    Parameters
    ----------
    base_url:  Base URL for the API (default: YOUR_BOT_TALK_SERVER:4242, or BOT_TALK_URL env var).
    token:     X-Bot-Token value. Use ``load_token()`` to read from standard paths.
    timeout:   Per-request timeout in seconds.
    retries:   Number of retries on transient failures (5xx, connection error).
    """

    def __init__(
        self,
        base_url: str = _DEFAULT_BASE_URL,
        token: str = "",
        timeout: float = _DEFAULT_TIMEOUT,
        retries: int = _DEFAULT_RETRIES,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.retries = retries
        self._headers = {"X-Bot-Token": token} if token else {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def health(self) -> dict[str, Any]:
        """GET /health — check if the server is up.

        Returns the parsed JSON response. Raises httpx.HTTPError on failure.
        """
        return self._get("/health", auth=False)

    def post_message(
        self,
        sender: str,
        content: str,
        genre: str = "status-update",
        tier: str = "TIER-BOT",
        **extra: Any,
    ) -> dict[str, Any]:
        """POST /message — send a message to all participants.

        Returns {"id": "...", "status": "ok", "timestamp": "..."} on success.
        Raises httpx.HTTPStatusError on 4xx/5xx.
        """
        payload = _build_post_payload(
            sender=sender, content=content, genre=genre, tier=tier, **extra
        )
        return self._post("/message", payload)

    def get_messages(
        self,
        since: datetime | str | None = None,
        limit: int = 100,
        sender_filter: str | None = None,
    ) -> list[dict[str, Any]]:
        """GET /messages — poll for messages since a given timestamp.

        Parameters
        ----------
        since:          Earliest message timestamp. Defaults to 24 hours ago.
        limit:          Maximum messages to return.
        sender_filter:  If set, return only messages from this sender.

        Returns a list of message dicts sorted by timestamp ascending.
        """
        params: dict[str, Any] = {
            "since": _format_since(since),
            "limit": limit,
        }
        if sender_filter:
            params["sender"] = sender_filter
        resp = self._get("/messages", params=params)
        messages: list[dict[str, Any]] = resp.get("messages", [])
        return sorted(messages, key=lambda m: m.get("timestamp", ""))

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        auth: bool = True,
    ) -> dict[str, Any]:
        headers = self._headers if auth else {}
        last_exc: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                resp = httpx.get(
                    f"{self.base_url}{path}",
                    headers=headers,
                    params=params,
                    timeout=self.timeout,
                )
                resp.raise_for_status()
                return resp.json()
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                log.debug(f"GET {path} attempt {attempt + 1} failed: {exc}")
                last_exc = exc
            except httpx.HTTPStatusError:
                raise
        raise last_exc or RuntimeError(f"GET {path} failed after {self.retries} retries")

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        last_exc: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                resp = httpx.post(
                    f"{self.base_url}{path}",
                    headers={**self._headers, "Content-Type": "application/json"},
                    json=payload,
                    timeout=self.timeout,
                )
                resp.raise_for_status()
                return resp.json()
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                log.debug(f"POST {path} attempt {attempt + 1} failed: {exc}")
                last_exc = exc
            except httpx.HTTPStatusError:
                raise
        raise last_exc or RuntimeError(f"POST {path} failed after {self.retries} retries")
