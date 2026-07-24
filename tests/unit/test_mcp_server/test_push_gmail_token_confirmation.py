"""
Tests for BIS-743: post-push confirmation on POST /api/push-gmail-token.

Before BIS-743, ``push_gmail_token_endpoint`` wrote the token file and
returned ``{"ok": true}`` with zero user-facing confirmation — unlike
``push_workspace_token_endpoint``, which already queues an outbox reply on
success. This file proves the Gmail endpoint now does the same thing,
mirroring ``test_push_workspace_token_confirmation.py``.

Covers:
- push_gmail_token: queues confirmation reply to outbox on success
- push_gmail_token: confirmation text mentions Gmail
- push_gmail_token: returns 200 ok even if the confirmation write fails
- push_gmail_token: confirmation delivered to the correct chat_id

All file I/O uses a tmp_path fixture — no real disk writes outside the test sandbox.
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
    """Yield (TestClient, token_dir, outbox_dir)."""
    gmail_token_dir = tmp_path / "config" / "gmail-tokens"
    outbox_dir = tmp_path / "outbox"
    gmail_token_dir.mkdir(parents=True)
    outbox_dir.mkdir(parents=True)

    with (
        patch(f"{_MODULE}._GMAIL_TOKEN_DIR", gmail_token_dir),
        patch(f"{_MODULE}._INTERNAL_SECRET", _VALID_SECRET),
        patch(f"{_MODULE}._SESSION_HMAC_SECRET", ""),
        patch(f"{_MODULE}._ENFORCE_GMAIL_SIGNED_SESSION", False),
        patch(f"{_MODULE}._seen_gmail_session_nonces", {}),
        patch(
            "os.path.expanduser",
            side_effect=lambda p: str(tmp_path) if p == "~/messages" else p,
        ),
    ):
        from src.mcp.inbox_server_http import app

        client = TestClient(app, raise_server_exceptions=True)
        yield client, gmail_token_dir, outbox_dir


class TestPostAuthConfirmation:
    def test_returns_ok_on_success(self, client_and_dirs):
        client, _, _ = client_and_dirs
        resp = client.post(
            "/api/push-gmail-token",
            json=_VALID_BODY,
            headers={"Authorization": f"Bearer {_VALID_SECRET}"},
        )
        assert resp.status_code == 200
        assert resp.json().get("ok") is True

    def test_confirmation_queued_to_outbox(self, client_and_dirs):
        client, _, outbox_dir = client_and_dirs
        client.post(
            "/api/push-gmail-token",
            json=_VALID_BODY,
            headers={"Authorization": f"Bearer {_VALID_SECRET}"},
        )
        outbox_files = list(outbox_dir.glob("*.json"))
        assert outbox_files, "No confirmation was queued to the outbox"
        reply = json.loads(outbox_files[0].read_text())
        text = reply.get("text", "")
        assert "Gmail" in text
        assert reply.get("chat_id") == _VALID_BODY["chat_id"]
        assert reply.get("source") == "telegram"

    def test_returns_ok_even_when_outbox_write_fails(self, tmp_path):
        """Token save succeeds and 200 is returned even if outbox write throws."""
        gmail_token_dir = tmp_path / "config" / "gmail-tokens"
        gmail_token_dir.mkdir(parents=True)

        with (
            patch(f"{_MODULE}._GMAIL_TOKEN_DIR", gmail_token_dir),
            patch(f"{_MODULE}._INTERNAL_SECRET", _VALID_SECRET),
            patch(f"{_MODULE}._SESSION_HMAC_SECRET", ""),
            patch(f"{_MODULE}._ENFORCE_GMAIL_SIGNED_SESSION", False),
            patch(f"{_MODULE}._seen_gmail_session_nonces", {}),
            patch(
                "os.path.expanduser",
                side_effect=lambda p: "/dev/null/nonexistent" if p == "~/messages" else p,
            ),
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
        gmail_token_dir = tmp_path / "config" / "gmail-tokens"
        gmail_token_dir.mkdir(parents=True)

        with (
            patch(f"{_MODULE}._GMAIL_TOKEN_DIR", gmail_token_dir),
            patch(f"{_MODULE}._INTERNAL_SECRET", _VALID_SECRET),
            patch(f"{_MODULE}._SESSION_HMAC_SECRET", ""),
            patch(f"{_MODULE}._ENFORCE_GMAIL_SIGNED_SESSION", False),
            patch(f"{_MODULE}._seen_gmail_session_nonces", {}),
            patch(
                "os.path.expanduser",
                side_effect=lambda p: "/dev/null/nonexistent" if p == "~/messages" else p,
            ),
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
