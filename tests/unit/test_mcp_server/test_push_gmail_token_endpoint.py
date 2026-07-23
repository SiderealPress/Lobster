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
- BIS-727 Slice 2 (per-transaction HMAC signing, gmail path — identical
  pattern to Slice 1's calendar path): a forged push carrying only the
  static bearer secret (no valid signed session) is rejected (401) once
  enforcement is on. Also covers: valid signed session accepted under
  enforcement; warn-mode (default) still accepts a missing/invalid session
  but logs a warning; tampered signature / wrong secret / expired /
  chat_id-scope mismatch / nonce replay are all rejected under enforcement.

All file I/O uses a tmp_path fixture — no real disk writes outside the test sandbox.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import stat
import sys
import time
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
_SESSION_HMAC_SECRET = "test-gmail-session-hmac-secret-xyz789"  # BIS-727 Slice 2

_VALID_BODY = {
    "chat_id": "123456",
    "access_token": "ya29.test-access-token",
    "refresh_token": "1//test-refresh-token",
    "expires_at": "2026-04-01T02:00:00Z",
    "scope": "https://www.googleapis.com/auth/gmail.readonly",
}


def _build_signed_session(
    chat_id: str,
    scope: str,
    *,
    instance_id: str = "http://test-vps:8741",
    nonce: str = "test-nonce-abc123",
    exp: float | None = None,
    secret: str = _SESSION_HMAC_SECRET,
    sig_override: str | None = None,
) -> dict:
    """Build a session_token dict matching the wire format signed by
    myownlobster.ai's callback route (BIS-727 Slice 1/2): HMAC-SHA256 over
    instance_id|chat_id|scope|nonce|exp.
    """
    if exp is None:
        exp = int(time.time()) + 1_800  # 30 min, matches SESSION_TOKEN_TTL
    exp = int(exp)
    message = f"{instance_id}|{chat_id}|{scope}|{nonce}|{exp}"
    sig = sig_override
    if sig is None:
        sig = hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()
    return {
        "instance_id": instance_id,
        "chat_id": chat_id,
        "scope": scope,
        "nonce": nonce,
        "exp": exp,
        "sig": sig,
    }


@pytest.fixture()
def client_and_token_dir(tmp_path):
    """Yield (TestClient, gmail_token_dir) with patched dirs and secrets.

    Enforcement defaults to False (warn mode) here, matching the module's
    real-world default — individual tests patch
    ``_ENFORCE_GMAIL_SIGNED_SESSION`` to True to exercise the enforced path.
    """
    gmail_token_dir = tmp_path / "config" / "gmail-tokens"

    with (
        patch(f"{_MODULE}._GMAIL_TOKEN_DIR", gmail_token_dir),
        patch(f"{_MODULE}._INTERNAL_SECRET", _VALID_SECRET),
        patch(f"{_MODULE}._SESSION_HMAC_SECRET", _SESSION_HMAC_SECRET),
        patch(f"{_MODULE}._ENFORCE_GMAIL_SIGNED_SESSION", False),
        patch(f"{_MODULE}._seen_gmail_session_nonces", {}),
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


# ---------------------------------------------------------------------------
# BIS-727 Slice 2 — forged-push-rejected (gmail path), mirroring Slice 1's
# test_forged_push_with_arbitrary_chat_id_is_now_rejected for calendar
# ---------------------------------------------------------------------------


def test_forged_push_with_arbitrary_chat_id_is_now_rejected(client_and_token_dir):
    """A request carrying only the static-secret bearer token, with an
    attacker-chosen chat_id that never went through any consent flow (no
    signed session attached at all), is rejected once enforcement is on."""
    client, token_dir = client_and_token_dir

    with patch(f"{_MODULE}._ENFORCE_GMAIL_SIGNED_SESSION", True):
        forged_chat_id = "999999999"  # attacker-supplied; never initiated any OAuth consent
        forged_body = {
            **_VALID_BODY,
            "chat_id": forged_chat_id,
        }

        resp = client.post(
            "/api/push-gmail-token",
            json=forged_body,
            headers={"Authorization": f"Bearer {_VALID_SECRET}"},  # only the static secret, no session
        )

        assert resp.status_code == 401, (
            "BIS-727 Slice 2: a static-secret-only push with no valid signed "
            "session must be rejected once enforcement is on."
        )
        forged_token_path = token_dir / f"{forged_chat_id}.json"
        assert not forged_token_path.exists(), "No token file should be written for a rejected push"


# ---------------------------------------------------------------------------
# BIS-727 Slice 2 — signed session verification (gmail path)
# ---------------------------------------------------------------------------


def test_valid_signed_session_is_accepted_under_enforcement(client_and_token_dir):
    """The legitimate-push counterpart to the forged-push-rejected test above:
    a valid signed session still succeeds even with enforcement on."""
    client, token_dir = client_and_token_dir

    with patch(f"{_MODULE}._ENFORCE_GMAIL_SIGNED_SESSION", True):
        session = _build_signed_session(_VALID_BODY["chat_id"], _VALID_BODY["scope"])
        body = {**_VALID_BODY, "session_token": session}

        resp = client.post(
            "/api/push-gmail-token",
            json=body,
            headers={"Authorization": f"Bearer {_VALID_SECRET}"},
        )

        assert resp.status_code == 200
        assert resp.json() == {"ok": True}
        assert (token_dir / f"{_VALID_BODY['chat_id']}.json").exists()


def test_missing_signed_session_accepted_but_warns_in_default_warn_mode(client_and_token_dir, caplog):
    """Default (enforce=False) behavior: a push with no signed session at all
    is still accepted, but a warning is logged — this is the 48h grace window."""
    client, token_dir = client_and_token_dir

    with caplog.at_level(logging.WARNING):
        resp = client.post(
            "/api/push-gmail-token",
            json=_VALID_BODY,
            headers={"Authorization": f"Bearer {_VALID_SECRET}"},
        )

    assert resp.status_code == 200
    assert (token_dir / f"{_VALID_BODY['chat_id']}.json").exists()
    assert any("signed session" in r.getMessage().lower() for r in caplog.records)


def test_tampered_signature_rejected_under_enforcement(client_and_token_dir):
    client, _ = client_and_token_dir

    with patch(f"{_MODULE}._ENFORCE_GMAIL_SIGNED_SESSION", True):
        session = _build_signed_session(_VALID_BODY["chat_id"], _VALID_BODY["scope"])
        session["sig"] = "0" * len(session["sig"])  # tamper: replace with a bogus signature
        body = {**_VALID_BODY, "session_token": session}

        resp = client.post(
            "/api/push-gmail-token",
            json=body,
            headers={"Authorization": f"Bearer {_VALID_SECRET}"},
        )

        assert resp.status_code == 401


def test_wrong_hmac_secret_rejected_under_enforcement(client_and_token_dir):
    client, _ = client_and_token_dir

    with patch(f"{_MODULE}._ENFORCE_GMAIL_SIGNED_SESSION", True):
        session = _build_signed_session(
            _VALID_BODY["chat_id"], _VALID_BODY["scope"], secret="a-completely-different-secret"
        )
        body = {**_VALID_BODY, "session_token": session}

        resp = client.post(
            "/api/push-gmail-token",
            json=body,
            headers={"Authorization": f"Bearer {_VALID_SECRET}"},
        )

        assert resp.status_code == 401


def test_expired_session_rejected_under_enforcement(client_and_token_dir):
    client, _ = client_and_token_dir

    with patch(f"{_MODULE}._ENFORCE_GMAIL_SIGNED_SESSION", True):
        expired_exp = int(time.time()) - 10  # already expired
        session = _build_signed_session(_VALID_BODY["chat_id"], _VALID_BODY["scope"], exp=expired_exp)
        body = {**_VALID_BODY, "session_token": session}

        resp = client.post(
            "/api/push-gmail-token",
            json=body,
            headers={"Authorization": f"Bearer {_VALID_SECRET}"},
        )

        assert resp.status_code == 401


def test_chat_id_mismatch_rejected_under_enforcement(client_and_token_dir):
    """A session signed for one chat_id cannot be replayed against another."""
    client, _ = client_and_token_dir

    with patch(f"{_MODULE}._ENFORCE_GMAIL_SIGNED_SESSION", True):
        session = _build_signed_session("111111111", _VALID_BODY["scope"])  # different chat_id
        body = {**_VALID_BODY, "session_token": session}  # body's chat_id is still "123456"

        resp = client.post(
            "/api/push-gmail-token",
            json=body,
            headers={"Authorization": f"Bearer {_VALID_SECRET}"},
        )

        assert resp.status_code == 401


def test_scope_mismatch_rejected_under_enforcement(client_and_token_dir):
    client, _ = client_and_token_dir

    with patch(f"{_MODULE}._ENFORCE_GMAIL_SIGNED_SESSION", True):
        session = _build_signed_session(_VALID_BODY["chat_id"], "some-other-scope")
        body = {**_VALID_BODY, "session_token": session}

        resp = client.post(
            "/api/push-gmail-token",
            json=body,
            headers={"Authorization": f"Bearer {_VALID_SECRET}"},
        )

        assert resp.status_code == 401


def test_nonce_reuse_rejected_under_enforcement(client_and_token_dir):
    """Single-use: replaying the exact same signed session twice succeeds
    once and is rejected the second time."""
    client, token_dir = client_and_token_dir

    with patch(f"{_MODULE}._ENFORCE_GMAIL_SIGNED_SESSION", True):
        session = _build_signed_session(_VALID_BODY["chat_id"], _VALID_BODY["scope"], nonce="reused-nonce-1")
        body = {**_VALID_BODY, "session_token": session}

        first = client.post(
            "/api/push-gmail-token",
            json=body,
            headers={"Authorization": f"Bearer {_VALID_SECRET}"},
        )
        second = client.post(
            "/api/push-gmail-token",
            json=body,
            headers={"Authorization": f"Bearer {_VALID_SECRET}"},
        )

        assert first.status_code == 200
        assert second.status_code == 401
        assert (token_dir / f"{_VALID_BODY['chat_id']}.json").exists()
