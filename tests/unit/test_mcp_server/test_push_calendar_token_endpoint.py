"""
Characterization tests for POST /api/push-calendar-token (BIS-728 / Slice 0).

This endpoint (``push_calendar_token_endpoint`` in ``src/mcp/inbox_server_http.py``)
had no direct test coverage before this file. These tests pin its CURRENT
behavior exactly as read from source, so later slices (BIS-727 Slice 1+) have
a regression net when the auth model is hardened.

Covers:
- Happy path: valid authenticated request returns {"ok": true} and writes token file
- Token file written to gcal-tokens/<chat_id>.json with mode 0o600
- Atomic write: .tmp file is renamed to final path, not left behind
- Auth: missing Authorization header -> 401
- Auth: wrong secret -> 401
- Auth: missing "Bearer " prefix -> 401
- Validation: missing chat_id / access_token / expires_at -> 400
- Validation: invalid expires_at format -> 400
- Validation: invalid JSON body -> 400
- Path traversal: chat_id with "../" components is sanitised, not written outside token dir
- Token values never appear in log output
- KNOWN-BAD (pinned intentionally): an unsigned request bearing only the
  static LOBSTER_INTERNAL_SECRET bearer token, with an arbitrary/attacker-
  suppliable chat_id, IS ACCEPTED today and writes a real token file for that
  chat_id. There is no per-transaction binding (no nonce, no consent-token
  check) — any caller who knows the static secret can push a token for ANY
  chat_id. See connectors-oauth-plan-v2.md Slice 1, which closes this for the
  calendar path specifically. When Slice 1 lands, the test named
  ``test_forged_push_with_arbitrary_chat_id_is_currently_accepted_KNOWN_VULNERABLE``
  below must flip to assert rejection (401/403) — that is the intended,
  expected diff.

All file I/O uses a tmp_path fixture — no real disk writes outside the test sandbox.
"""

from __future__ import annotations

import json
import logging
import os
import stat
from pathlib import Path
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

# inbox_server_http calls sys.exit(1) at import time when MCP_HTTP_TOKEN is
# not set. Pre-seed the env var before the first import so the module loads.
os.environ.setdefault("MCP_HTTP_TOKEN", "test-mcp-token-placeholder")

_MODULE = "src.mcp.inbox_server_http"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_VALID_SECRET = "test-calendar-secret-abc123"

_VALID_BODY = {
    "chat_id": "123456",
    "access_token": "ya29.test-calendar-access-token",
    "refresh_token": "1//test-calendar-refresh-token",
    "expires_at": "2026-04-01T02:00:00Z",
    "scope": "https://www.googleapis.com/auth/calendar.events",
}


@pytest.fixture()
def client_and_token_dir(tmp_path):
    """Yield (TestClient, gcal_token_dir) with patched dirs and secret."""
    gcal_token_dir = tmp_path / "config" / "gcal-tokens"

    with (
        patch(f"{_MODULE}._GCAL_TOKEN_DIR", gcal_token_dir),
        patch(f"{_MODULE}._INTERNAL_SECRET", _VALID_SECRET),
    ):
        from src.mcp.inbox_server_http import app

        client = TestClient(app, raise_server_exceptions=True)
        yield client, gcal_token_dir


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_valid_request_returns_ok(client_and_token_dir):
    client, _ = client_and_token_dir
    resp = client.post(
        "/api/push-calendar-token",
        json=_VALID_BODY,
        headers={"Authorization": f"Bearer {_VALID_SECRET}"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_token_file_written_with_correct_content(client_and_token_dir):
    client, token_dir = client_and_token_dir
    client.post(
        "/api/push-calendar-token",
        json=_VALID_BODY,
        headers={"Authorization": f"Bearer {_VALID_SECRET}"},
    )
    token_path = token_dir / "123456.json"
    assert token_path.exists(), "Token file was not created"

    data = json.loads(token_path.read_text())
    assert data["access_token"] == _VALID_BODY["access_token"]
    assert data["refresh_token"] == _VALID_BODY["refresh_token"]
    assert data["scope"] == _VALID_BODY["scope"]
    assert "expires_at" in data
    assert "2026-04-01" in data["expires_at"]


def test_token_file_mode_is_0o600(client_and_token_dir):
    client, token_dir = client_and_token_dir
    client.post(
        "/api/push-calendar-token",
        json=_VALID_BODY,
        headers={"Authorization": f"Bearer {_VALID_SECRET}"},
    )
    token_path = token_dir / "123456.json"
    file_mode = stat.S_IMODE(os.stat(str(token_path)).st_mode)
    assert file_mode == 0o600, f"Expected 0o600, got {oct(file_mode)}"


def test_no_tmp_file_left_behind(client_and_token_dir):
    client, token_dir = client_and_token_dir
    client.post(
        "/api/push-calendar-token",
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
    resp = client.post("/api/push-calendar-token", json=_VALID_BODY)
    assert resp.status_code == 401


def test_wrong_secret_returns_401(client_and_token_dir):
    client, _ = client_and_token_dir
    resp = client.post(
        "/api/push-calendar-token",
        json=_VALID_BODY,
        headers={"Authorization": "Bearer wrong-secret"},
    )
    assert resp.status_code == 401


def test_bearer_prefix_required(client_and_token_dir):
    client, _ = client_and_token_dir
    resp = client.post(
        "/api/push-calendar-token",
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
        "/api/push-calendar-token",
        json=body,
        headers={"Authorization": f"Bearer {_VALID_SECRET}"},
    )
    assert resp.status_code == 400


def test_invalid_expires_at_returns_400(client_and_token_dir):
    client, _ = client_and_token_dir
    body = {**_VALID_BODY, "expires_at": "not-a-date"}
    resp = client.post(
        "/api/push-calendar-token",
        json=body,
        headers={"Authorization": f"Bearer {_VALID_SECRET}"},
    )
    assert resp.status_code == 400
    assert "expires_at" in resp.json().get("error", "").lower()


def test_invalid_json_body_returns_400(client_and_token_dir):
    client, _ = client_and_token_dir
    resp = client.post(
        "/api/push-calendar-token",
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
        "/api/push-calendar-token",
        json=body,
        headers={"Authorization": f"Bearer {_VALID_SECRET}"},
    )
    if resp.status_code == 200:
        evil_path = token_dir.parent.parent / "evil.json"
        assert not evil_path.exists(), "Path traversal wrote outside token directory"
        sanitised_id = "".join(c for c in malicious_chat_id if c.isalnum() or c in ("-", "_"))
        if sanitised_id:
            expected = token_dir / f"{sanitised_id}.json"
            assert expected.exists(), f"Expected sanitised file at {expected}"
    else:
        assert resp.status_code == 400


def test_empty_chat_id_returns_400(client_and_token_dir):
    client, _ = client_and_token_dir
    body = {**_VALID_BODY, "chat_id": ""}
    resp = client.post(
        "/api/push-calendar-token",
        json=body,
        headers={"Authorization": f"Bearer {_VALID_SECRET}"},
    )
    assert resp.status_code == 400


def test_chat_id_with_only_special_chars_returns_400(client_and_token_dir):
    """chat_id like '/../' becomes empty after sanitisation -> 400."""
    client, _ = client_and_token_dir
    body = {**_VALID_BODY, "chat_id": "/../"}
    resp = client.post(
        "/api/push-calendar-token",
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
            "/api/push-calendar-token",
            json=_VALID_BODY,
            headers={"Authorization": f"Bearer {_VALID_SECRET}"},
        )

    combined = " ".join(r.getMessage() for r in caplog.records)
    assert "ya29.test-calendar-access-token" not in combined, "access_token appeared in logs"
    assert "1//test-calendar-refresh-token" not in combined, "refresh_token appeared in logs"


# ---------------------------------------------------------------------------
# KNOWN-BAD characterization (BIS-727 Slice 0 -> Slice 1 regression net)
#
# Today, `_is_authorized_internal` (inbox_server_http.py) only checks the
# static LOBSTER_INTERNAL_SECRET bearer token. It does NOT verify that the
# request corresponds to a specific consent transaction the user actually
# initiated (no nonce, no per-transaction signature, no expiry binding).
# Anyone holding the static secret can push a token for ANY chat_id.
#
# Slice 1 (per-transaction HMAC signing, calendar-scope-only) is expected to
# flip this exact test to assert rejection. Do not "fix" this test in Slice 0
# — it must describe today's real, vulnerable behavior.
# ---------------------------------------------------------------------------


def test_forged_push_with_arbitrary_chat_id_is_currently_accepted_KNOWN_VULNERABLE(
    client_and_token_dir,
):
    """KNOWN VULNERABLE (pinned intentionally, see module docstring).

    A request carrying only the static-secret bearer token, with an
    attacker-chosen chat_id that never went through any consent flow, is
    accepted (200) and a real token file is written for that chat_id.
    There is no nonce / per-transaction binding today.

    Slice 1 must flip this assertion to expect rejection (401/403) once
    per-transaction signing is enforced for the calendar push path.
    """
    client, token_dir = client_and_token_dir

    forged_chat_id = "999999999"  # attacker-supplied; never initiated any OAuth consent
    forged_body = {
        **_VALID_BODY,
        "chat_id": forged_chat_id,
    }

    resp = client.post(
        "/api/push-calendar-token",
        json=forged_body,
        headers={"Authorization": f"Bearer {_VALID_SECRET}"},  # only the static secret, no nonce
    )

    # --- Pinning today's (vulnerable) behavior ---
    assert resp.status_code == 200, (
        "Expected today's known-vulnerable behavior: static-secret-only request "
        "is accepted regardless of chat_id. If this now fails, Slice 1's fix "
        "has landed — replace this test with an equivalent 'KNOWN_GOOD' "
        "rejection test rather than deleting the coverage."
    )
    assert resp.json() == {"ok": True}

    forged_token_path = token_dir / f"{forged_chat_id}.json"
    assert forged_token_path.exists(), (
        "Expected today's known-vulnerable behavior: a real token file is "
        "written for the attacker-supplied chat_id with no additional binding."
    )
    written = json.loads(forged_token_path.read_text())
    assert written["access_token"] == forged_body["access_token"]
