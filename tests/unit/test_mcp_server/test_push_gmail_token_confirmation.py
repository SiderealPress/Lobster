"""
Tests for post-push confirmation on POST /api/push-gmail-token.

BIS-743 added a basic static-text confirmation (mirroring the pre-existing
Workspace pattern). BIS-744 upgrades it via the shared
``_queue_push_confirmation`` helper: a live-data preview (latest email
subject), graceful degradation when the preview fetch fails, and a de-dupe
guard. This file covers the Gmail endpoint's wiring into that shared helper;
the helper's own logic (failure visibility, system-alert fallback) is
covered exhaustively in ``test_push_token_confirmation_shared.py``.

Covers:
- push_gmail_token: queues confirmation reply to outbox on success
- push_gmail_token: confirmation includes a live latest-email preview
- push_gmail_token: confirmation degrades gracefully (never silent) when the
  live-data fetch fails
- push_gmail_token: returns 200 ok even if the confirmation write fails
- push_gmail_token: a second push for the same chat_id within the de-dupe
  window does not queue a second confirmation

All file I/O uses a tmp_path fixture — no real disk writes outside the test
sandbox. This fixture patches ``_MESSAGES_DIR`` directly (not
``os.path.expanduser``) since the endpoint now resolves its outbox path via
that module global (BIS-744) rather than re-resolving "~/messages" itself.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

# inbox_server_http calls sys.exit(1) at import time when MCP_HTTP_TOKEN is
# not set.  Pre-seed the env var before the first import.
os.environ.setdefault("MCP_HTTP_TOKEN", "test-mcp-token-placeholder")

_MODULE = "src.mcp.inbox_server_http"
_VALID_SECRET = "test-gmail-confirm-secret-xyz"

_VALID_BODY = {
    "chat_id": "6645894374",
    "access_token": "ya29.fake-gmail-token",
    "refresh_token": "1//refresh-fake",
    "expires_at": (datetime.now(tz=timezone.utc) + timedelta(hours=1)).isoformat(),
    "scope": "https://www.googleapis.com/auth/gmail.readonly",
}


@pytest.fixture()
def client_and_dirs(tmp_path):
    """Yield (TestClient, token_dir, outbox_dir), fully isolated from prod."""
    messages_base = tmp_path / "confirm_messages"
    gmail_token_dir = messages_base / "config" / "gmail-tokens"
    outbox_dir = messages_base / "outbox"
    gmail_token_dir.mkdir(parents=True)
    outbox_dir.mkdir(parents=True)

    with (
        patch(f"{_MODULE}._MESSAGES_DIR", messages_base),
        patch(f"{_MODULE}._GMAIL_TOKEN_DIR", gmail_token_dir),
        patch(f"{_MODULE}._INTERNAL_SECRET", _VALID_SECRET),
        patch(f"{_MODULE}._SESSION_HMAC_SECRET", ""),
        patch(f"{_MODULE}._ENFORCE_GMAIL_SIGNED_SESSION", False),
        patch(f"{_MODULE}._seen_gmail_session_nonces", {}),
        patch(f"{_MODULE}._seen_push_confirmations", {}),
        patch(f"{_MODULE}._fetch_authenticated_email", return_value="test-user@example.com"),
    ):
        from src.mcp.inbox_server_http import app

        client = TestClient(app, raise_server_exceptions=True)
        yield client, gmail_token_dir, outbox_dir


def _read_only_outbox_file(outbox_dir: Path) -> dict:
    files = list(outbox_dir.glob("*.json"))
    assert len(files) == 1, f"Expected exactly one outbox file, found {len(files)}"
    return json.loads(files[0].read_text())


class TestPostAuthConfirmation:
    def test_returns_ok_on_success(self, client_and_dirs):
        client, _, _ = client_and_dirs
        with patch(f"{_MODULE}._fetch_gmail_preview", return_value='Latest email: "Hi" from a@b.com'):
            resp = client.post(
                "/api/push-gmail-token",
                json=_VALID_BODY,
                headers={"Authorization": f"Bearer {_VALID_SECRET}"},
            )
        assert resp.status_code == 200
        assert resp.json().get("ok") is True

    def test_confirmation_queued_to_outbox(self, client_and_dirs):
        client, _, outbox_dir = client_and_dirs
        with patch(f"{_MODULE}._fetch_gmail_preview", return_value='Latest email: "Hi" from a@b.com'):
            client.post(
                "/api/push-gmail-token",
                json=_VALID_BODY,
                headers={"Authorization": f"Bearer {_VALID_SECRET}"},
            )
        reply = _read_only_outbox_file(outbox_dir)
        text = reply.get("text", "")
        assert "Gmail" in text
        assert reply.get("chat_id") == _VALID_BODY["chat_id"]
        assert reply.get("source") == "telegram"

    def test_confirmation_includes_live_preview(self, client_and_dirs):
        """BIS-744: the confirmation text includes the real live-data preview."""
        client, _, outbox_dir = client_and_dirs
        with patch(
            f"{_MODULE}._fetch_gmail_preview",
            return_value='Latest email: "Q3 numbers" from cfo@example.com',
        ):
            client.post(
                "/api/push-gmail-token",
                json=_VALID_BODY,
                headers={"Authorization": f"Bearer {_VALID_SECRET}"},
            )
        reply = _read_only_outbox_file(outbox_dir)
        assert "Q3 numbers" in reply["text"]

    def test_confirmation_degrades_gracefully_when_preview_fetch_fails(self, client_and_dirs, caplog):
        """BIS-744: a live-data fetch failure must never silence the confirmation."""
        import logging

        client, _, outbox_dir = client_and_dirs
        with patch(
            f"{_MODULE}._fetch_gmail_preview", side_effect=RuntimeError("Gmail API down")
        ), caplog.at_level(logging.ERROR):
            resp = client.post(
                "/api/push-gmail-token",
                json=_VALID_BODY,
                headers={"Authorization": f"Bearer {_VALID_SECRET}"},
            )

        assert resp.status_code == 200
        reply = _read_only_outbox_file(outbox_dir)
        assert "connected" in reply["text"].lower()
        assert "couldn't fetch a live preview" in reply["text"]
        assert any(
            "preview fetch failed" in r.getMessage().lower() for r in caplog.records
        ), "Preview fetch failure must be logged loudly, not silently swallowed"

    def test_returns_ok_even_when_outbox_write_fails(self, tmp_path):
        """Token save succeeds and 200 is returned even if outbox write throws."""
        messages_base = tmp_path / "confirm_messages"
        gmail_token_dir = messages_base / "config" / "gmail-tokens"
        gmail_token_dir.mkdir(parents=True)
        blocked_base = tmp_path / "blocked"
        blocked_base.mkdir()
        (blocked_base / "outbox").write_text("not a directory")

        with (
            patch(f"{_MODULE}._MESSAGES_DIR", blocked_base),
            patch(f"{_MODULE}._GMAIL_TOKEN_DIR", gmail_token_dir),
            patch(f"{_MODULE}._INTERNAL_SECRET", _VALID_SECRET),
            patch(f"{_MODULE}._SESSION_HMAC_SECRET", ""),
            patch(f"{_MODULE}._ENFORCE_GMAIL_SIGNED_SESSION", False),
            patch(f"{_MODULE}._seen_gmail_session_nonces", {}),
            patch(f"{_MODULE}._seen_push_confirmations", {}),
            patch(f"{_MODULE}._fetch_authenticated_email", return_value="test-user@example.com"),
            patch(f"{_MODULE}._fetch_gmail_preview", return_value="preview"),
        ):
            from src.mcp.inbox_server_http import app

            client = TestClient(app, raise_server_exceptions=True)
            resp = client.post(
                "/api/push-gmail-token",
                json=_VALID_BODY,
                headers={"Authorization": f"Bearer {_VALID_SECRET}"},
            )

        assert resp.status_code == 200
        assert resp.json().get("ok") is True

    def test_token_still_saved_even_if_confirmation_would_fail(self, tmp_path):
        """The token write must not depend on / be blocked by the confirmation step."""
        messages_base = tmp_path / "confirm_messages"
        gmail_token_dir = messages_base / "config" / "gmail-tokens"
        gmail_token_dir.mkdir(parents=True)
        blocked_base = tmp_path / "blocked"
        blocked_base.mkdir()
        (blocked_base / "outbox").write_text("not a directory")

        with (
            patch(f"{_MODULE}._MESSAGES_DIR", blocked_base),
            patch(f"{_MODULE}._GMAIL_TOKEN_DIR", gmail_token_dir),
            patch(f"{_MODULE}._INTERNAL_SECRET", _VALID_SECRET),
            patch(f"{_MODULE}._SESSION_HMAC_SECRET", ""),
            patch(f"{_MODULE}._ENFORCE_GMAIL_SIGNED_SESSION", False),
            patch(f"{_MODULE}._seen_gmail_session_nonces", {}),
            patch(f"{_MODULE}._seen_push_confirmations", {}),
            patch(f"{_MODULE}._fetch_authenticated_email", return_value="test-user@example.com"),
            patch(f"{_MODULE}._fetch_gmail_preview", return_value="preview"),
        ):
            from src.mcp.inbox_server_http import app

            client = TestClient(app, raise_server_exceptions=True)
            client.post(
                "/api/push-gmail-token",
                json=_VALID_BODY,
                headers={"Authorization": f"Bearer {_VALID_SECRET}"},
            )

        token_path = gmail_token_dir / f"{_VALID_BODY['chat_id']}.json"
        assert token_path.exists(), "Token file must be written regardless of confirmation outcome"

    def test_duplicate_push_does_not_send_second_confirmation(self, client_and_dirs):
        """BIS-744: de-dupe guard — two pushes for the same chat_id in quick
        succession must only produce ONE queued confirmation."""
        client, _, outbox_dir = client_and_dirs
        with patch(f"{_MODULE}._fetch_gmail_preview", return_value="preview line"):
            first = client.post(
                "/api/push-gmail-token",
                json=_VALID_BODY,
                headers={"Authorization": f"Bearer {_VALID_SECRET}"},
            )
            second = client.post(
                "/api/push-gmail-token",
                json=_VALID_BODY,
                headers={"Authorization": f"Bearer {_VALID_SECRET}"},
            )

        assert first.status_code == 200
        assert second.status_code == 200
        outbox_files = list(outbox_dir.glob("*.json"))
        assert len(outbox_files) == 1, (
            f"Expected exactly one confirmation across two pushes, found {len(outbox_files)}"
        )
