"""
Tests for POST /api/push-gmail-token endpoint.

Covers:
- Happy path: valid authenticated request returns {"ok": true} and writes token file
- Token file written to gmail-tokens/<chat_id>.json with mode 0o600
- Atomic write: .tmp file is renamed to final path
- Auth: missing Authorization header -> 401
- Auth: wrong secret -> 401
- Validation: missing chat_id -> 400
- Validation: missing access_token -> 400
- Validation: missing expires_at -> 400
- Validation: invalid expires_at format -> 400
- Path traversal: chat_id with "../" components is sanitised, not written outside token dir
- Token values never appear in log output

All file I/O uses a tmp_path fixture — no real disk writes outside the test sandbox.
"""

from __future__ import annotations

import json
import logging
import os
import stat
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

# inbox_server_http calls sys.exit(1) at import time when MCP_HTTP_TOKEN is
# not set.  Pre-seed the env var before the first import so the module loads.
os.environ.setdefault("MCP_HTTP_TOKEN", "test-mcp-token-placeholder")

# The conftest.py at tests/ inserts the repo root into sys.path so that
# ``src.mcp.inbox_server_http`` is the canonical module path used for patches.
# No extra sys.path manipulation is needed here.

_MODULE = "src.mcp.inbox_server_http"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_VALID_SECRET = "test-secret-abc123"

_VALID_BODY = {
    "chat_id": "123456",
    "access_token": "ya29.test-access-token",
    "refresh_token": "1//test-refresh-token",
    "expires_at": "2026-04-01T02:00:00Z",
    "scope": "https://www.googleapis.com/auth/gmail.readonly",
}


@pytest.fixture()
def client_and_token_dir(tmp_path):
    """Yield (TestClient, gmail_token_dir) with patched dirs and secret."""
    gmail_token_dir = tmp_path / "config" / "gmail-tokens"

    with (
        patch(f"{_MODULE}._GMAIL_TOKEN_DIR", gmail_token_dir),
        patch(f"{_MODULE}._INTERNAL_SECRET", _VALID_SECRET),
    ):
        from src.mcp.inbox_server_http import app

        client = TestClient(app, raise_server_exceptions=True)
        yield client, gmail_token_dir


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_valid_request_returns_ok(client_and_token_dir):
    client, _ = client_and_token_dir
    resp = client.post(
        "/api/push-gmail-token",
        json=_VALID_BODY,
        headers={"Authorization": f"Bearer {_VALID_SECRET}"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_token_file_written_with_correct_content(client_and_token_dir):
    client, token_dir = client_and_token_dir
    client.post(
        "/api/push-gmail-token",
        json=_VALID_BODY,
        headers={"Authorization": f"Bearer {_VALID_SECRET}"},
    )
    token_path = token_dir / "123456.json"
    assert token_path.exists(), "Token file was not created"

    data = json.loads(token_path.read_text())
    assert data["access_token"] == _VALID_BODY["access_token"]
    assert data["refresh_token"] == _VALID_BODY["refresh_token"]
    assert data["scope"] == _VALID_BODY["scope"]
    # expires_at is re-serialised after parsing; check presence and content
    assert "expires_at" in data
    assert "2026-04-01" in data["expires_at"]


def test_token_file_mode_is_0o600(client_and_token_dir):
    client, token_dir = client_and_token_dir
    client.post(
        "/api/push-gmail-token",
        json=_VALID_BODY,
        headers={"Authorization": f"Bearer {_VALID_SECRET}"},
    )
    token_path = token_dir / "123456.json"
    file_mode = stat.S_IMODE(os.stat(str(token_path)).st_mode)
    assert file_mode == 0o600, f"Expected 0o600, got {oct(file_mode)}"


def test_no_tmp_file_left_behind(client_and_token_dir):
    client, token_dir = client_and_token_dir
    client.post(
        "/api/push-gmail-token",
        json=_VALID_BODY,
        headers={"Authorization": f"Bearer {_VALID_SECRET}"},
    )
    tmp_path = token_dir / "123456.json.tmp"
    assert not tmp_path.exists(), ".tmp file should have been renamed away"


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


def test_missing_auth_header_returns_401(client_and_token_dir):
    client, _ = client_and_token_dir
    resp = client.post("/api/push-gmail-token", json=_VALID_BODY)
    assert resp.status_code == 401


def test_wrong_secret_returns_401(client_and_token_dir):
    client, _ = client_and_token_dir
    resp = client.post(
        "/api/push-gmail-token",
        json=_VALID_BODY,
        headers={"Authorization": "Bearer wrong-secret"},
    )
    assert resp.status_code == 401


def test_bearer_prefix_required(client_and_token_dir):
    client, _ = client_and_token_dir
    resp = client.post(
        "/api/push-gmail-token",
        json=_VALID_BODY,
        headers={"Authorization": _VALID_SECRET},  # no "Bearer " prefix
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "missing_field",
    ["chat_id", "access_token", "expires_at"],
)
def test_missing_required_field_returns_400(client_and_token_dir, missing_field):
    client, _ = client_and_token_dir
    body = {k: v for k, v in _VALID_BODY.items() if k != missing_field}
    resp = client.post(
        "/api/push-gmail-token",
        json=body,
        headers={"Authorization": f"Bearer {_VALID_SECRET}"},
    )
    assert resp.status_code == 400


def test_invalid_expires_at_returns_400(client_and_token_dir):
    client, _ = client_and_token_dir
    body = {**_VALID_BODY, "expires_at": "not-a-date"}
    resp = client.post(
        "/api/push-gmail-token",
        json=body,
        headers={"Authorization": f"Bearer {_VALID_SECRET}"},
    )
    assert resp.status_code == 400
    assert "expires_at" in resp.json().get("error", "").lower()


def test_invalid_json_body_returns_400(client_and_token_dir):
    client, _ = client_and_token_dir
    resp = client.post(
        "/api/push-gmail-token",
        content=b"not-json",
        headers={
            "Authorization": f"Bearer {_VALID_SECRET}",
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Path traversal
# ---------------------------------------------------------------------------


def test_path_traversal_in_chat_id_is_sanitised(client_and_token_dir):
    client, token_dir = client_and_token_dir
    malicious_chat_id = "../evil"
    body = {**_VALID_BODY, "chat_id": malicious_chat_id}
    resp = client.post(
        "/api/push-gmail-token",
        json=body,
        headers={"Authorization": f"Bearer {_VALID_SECRET}"},
    )
    # The sanitised chat_id becomes "evil" (dots and slashes stripped)
    # The response must be OK (the sanitised value is valid), not an error
    # AND the file must not have been written outside the token directory
    if resp.status_code == 200:
        # Confirm no file outside the token dir
        evil_path = token_dir.parent.parent / "evil.json"
        assert not evil_path.exists(), "Path traversal wrote outside token directory"
        # Confirm the sanitised version landed inside the token dir
        sanitised_id = "".join(c for c in malicious_chat_id if c.isalnum() or c in ("-", "_"))
        if sanitised_id:
            expected = token_dir / f"{sanitised_id}.json"
            assert expected.exists(), f"Expected sanitised file at {expected}"
    else:
        # If the sanitised chat_id is empty (all chars stripped), a 400 is correct
        assert resp.status_code == 400


def test_empty_chat_id_returns_400(client_and_token_dir):
    client, _ = client_and_token_dir
    body = {**_VALID_BODY, "chat_id": ""}
    resp = client.post(
        "/api/push-gmail-token",
        json=body,
        headers={"Authorization": f"Bearer {_VALID_SECRET}"},
    )
    assert resp.status_code == 400


def test_chat_id_with_only_special_chars_returns_400(client_and_token_dir):
    """chat_id like '/../' becomes empty after sanitisation -> 400."""
    client, _ = client_and_token_dir
    body = {**_VALID_BODY, "chat_id": "/../"}
    resp = client.post(
        "/api/push-gmail-token",
        json=body,
        headers={"Authorization": f"Bearer {_VALID_SECRET}"},
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Token values must not appear in logs
# ---------------------------------------------------------------------------


def test_token_values_not_in_log_output(client_and_token_dir, caplog):
    """access_token and refresh_token must never appear in log records."""
    client, _ = client_and_token_dir
    with caplog.at_level(logging.DEBUG):
        client.post(
            "/api/push-gmail-token",
            json=_VALID_BODY,
            headers={"Authorization": f"Bearer {_VALID_SECRET}"},
        )

    combined = " ".join(r.getMessage() for r in caplog.records)
    assert "ya29.test-access-token" not in combined, "access_token appeared in logs"
    assert "1//test-refresh-token" not in combined, "refresh_token appeared in logs"
