"""
Granola API Client — Slice 1

Thin, typed wrapper around the Granola public REST API.
No LLM calls. No heavy dependencies — only stdlib + urllib.

API base: https://public-api.granola.ai/v1
Auth:     Authorization: Bearer <GRANOLA_API_KEY>
Rate:     5 req/sec (300/min), burst 25 in 5s; 429 → retry with backoff
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Optional


GRANOLA_API_BASE = "https://public-api.granola.ai/v1"
_DEFAULT_MAX_RETRIES = 3
_DEFAULT_RETRY_BACKOFF_BASE = 1.0  # seconds, doubled each attempt


@dataclass
class GranolaNote:
    """Minimal typed wrapper around a raw Granola note dict."""

    id: str
    title: str
    created_at: str  # ISO 8601 string
    raw: dict = field(repr=False)  # full original JSON for storage

    @classmethod
    def from_dict(cls, d: dict) -> "GranolaNote":
        return cls(
            id=d["id"],
            title=d.get("title", ""),
            created_at=d.get("created_at", ""),
            raw=d,
        )


@dataclass
class ListNotesResult:
    notes: list[GranolaNote]
    has_more: bool
    cursor: Optional[str]


class GranolaAPIError(Exception):
    """Raised on non-2xx responses that are not retried."""

    def __init__(self, status: int, body: str):
        self.status = status
        self.body = body
        super().__init__(f"Granola API error {status}: {body}")


class GranolaRateLimitError(GranolaAPIError):
    """Raised when 429 is returned and retries are exhausted."""


class GranolaClient:
    """
    Thin HTTP client for the Granola public API.

    Usage::

        client = GranolaClient()               # reads GRANOLA_API_KEY from env
        result = client.list_notes()
        for note in result.notes:
            print(note.id, note.title)

        note = client.get_note("abc123", include_transcript=True)
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: str = GRANOLA_API_BASE,
        max_retries: int = _DEFAULT_MAX_RETRIES,
        retry_backoff_base: float = _DEFAULT_RETRY_BACKOFF_BASE,
    ):
        self._api_key = api_key or os.environ.get("GRANOLA_API_KEY")
        if not self._api_key:
            raise ValueError(
                "GRANOLA_API_KEY not set. Export it or pass api_key= to GranolaClient()."
            )
        self._base_url = base_url.rstrip("/")
        self._max_retries = max_retries
        self._retry_backoff_base = retry_backoff_base

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def list_notes(
        self,
        created_after: Optional[str] = None,
        cursor: Optional[str] = None,
    ) -> ListNotesResult:
        """
        Fetch one page of notes.

        Args:
            created_after: ISO 8601 timestamp; only return notes created after this.
            cursor:         Opaque pagination cursor from a previous response.

        Returns:
            ListNotesResult with notes list, has_more flag, and next cursor.
        """
        params: dict[str, str] = {}
        if created_after:
            params["created_after"] = created_after
        if cursor:
            params["cursor"] = cursor

        data = self._get("/notes", params=params)

        notes = [GranolaNote.from_dict(n) for n in data.get("notes", [])]
        return ListNotesResult(
            notes=notes,
            has_more=data.get("hasMore", False),
            cursor=data.get("cursor"),
        )

    def get_note(
        self,
        note_id: str,
        include_transcript: bool = False,
    ) -> dict:
        """
        Fetch a single note by ID.

        Args:
            note_id:             Granola note UUID.
            include_transcript:  If True, include the transcript array in the response.

        Returns:
            Raw dict as returned by the API.
        """
        params: dict[str, str] = {}
        if include_transcript:
            params["include"] = "transcript"
        return self._get(f"/notes/{note_id}", params=params)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get(self, path: str, params: Optional[dict] = None) -> Any:
        url = self._base_url + path
        if params:
            query = "&".join(
                f"{k}={urllib.request.quote(str(v))}" for k, v in params.items()
            )
            url = f"{url}?{query}"

        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Accept": "application/json",
            },
        )

        last_error: Optional[Exception] = None
        for attempt in range(self._max_retries):
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    body = resp.read().decode("utf-8")
                    return json.loads(body)
            except urllib.error.HTTPError as exc:
                if exc.code == 429:
                    if attempt < self._max_retries - 1:
                        wait = self._retry_backoff_base * (2 ** attempt)
                        time.sleep(wait)
                        last_error = exc
                        continue
                    else:
                        body = exc.read().decode("utf-8") if exc.fp else ""
                        raise GranolaRateLimitError(exc.code, body) from exc
                else:
                    body = exc.read().decode("utf-8") if exc.fp else ""
                    raise GranolaAPIError(exc.code, body) from exc

        # Should not reach here but satisfy type checker
        raise GranolaRateLimitError(429, "Max retries exceeded") from last_error
