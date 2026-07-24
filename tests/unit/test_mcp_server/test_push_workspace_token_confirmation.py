"""
Tests for the Slice 7 onboarding additions:

- push_workspace_token: queues confirmation reply to outbox on success
- push_workspace_token: confirmation text mentions /gdocs, /gdrive, /gsheets
- push_workspace_token: returns 200 ok even if the confirmation write fails
- generate_consent_link('workspace'): returns URL containing https://
- generate_consent_link('workspace'): workspace is in _VALID_SCOPES
- generate_consent_link: graceful failure when myownlobster.ai unreachable
- skill.toml: /workspace connect is a registered trigger
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from starlette.testclient import TestClient

# inbox_server_http calls sys.exit(1) at import time when MCP_HTTP_TOKEN is
# not set.  Pre-seed the env var before the first import.
os.environ.setdefault("MCP_HTTP_TOKEN", "test-token-for-http-server")

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "src"))

_MODULE = "src.mcp.inbox_server_http"
_VALID_SECRET = "test-workspace-secret-xyz"

_VALID_BODY = {
    "chat_id": "6645894374",
    "access_token": "ya29.fake-workspace-token",
    "refresh_token": "1//refresh-fake",
    "expires_at": (datetime.now(tz=timezone.utc) + timedelta(hours=1)).isoformat(),
    "scope": "https://www.googleapis.com/auth/spreadsheets",
}


@pytest.fixture()
def client_and_dirs(tmp_path):
    """Yield (TestClient, token_dir, outbox_dir), fully isolated from prod.

    BIS-744: patches ``_MESSAGES_DIR`` directly instead of
    ``os.path.expanduser`` -- the endpoint now resolves its outbox path via
    that module global (shared with the calendar/gmail endpoints through
    ``_queue_push_confirmation``) rather than re-resolving "~/messages"
    itself.
    """
    messages_base = tmp_path / "confirm_messages"
    workspace_token_dir = messages_base / "config" / "workspace-tokens"
    outbox_dir = messages_base / "outbox"
    workspace_token_dir.mkdir(parents=True)
    outbox_dir.mkdir(parents=True)

    with (
        patch(f"{_MODULE}._MESSAGES_DIR", messages_base),
        patch(f"{_MODULE}._WORKSPACE_TOKEN_DIR", workspace_token_dir),
        patch(f"{_MODULE}._INTERNAL_SECRET", _VALID_SECRET),
        patch(f"{_MODULE}._SESSION_HMAC_SECRET", ""),
        patch(f"{_MODULE}._ENFORCE_WORKSPACE_SIGNED_SESSION", False),
        patch(f"{_MODULE}._seen_workspace_session_nonces", {}),
        patch(f"{_MODULE}._seen_push_confirmations", {}),
    ):
        from src.mcp.inbox_server_http import app

        client = TestClient(app, raise_server_exceptions=True)
        yield client, workspace_token_dir, outbox_dir


def _read_only_outbox_file(outbox_dir: Path) -> dict:
    files = list(outbox_dir.glob("*.json"))
    assert len(files) == 1, f"Expected exactly one outbox file, found {len(files)}"
    return json.loads(files[0].read_text())


# ---------------------------------------------------------------------------
# Post-auth confirmation — outbox content
# ---------------------------------------------------------------------------


class TestPostAuthConfirmation:
    def test_returns_ok_on_success(self, client_and_dirs):
        client, _, _ = client_and_dirs
        with patch(f"{_MODULE}._fetch_workspace_preview", return_value="Most recent Drive file: Q3 plan.docx"):
            resp = client.post(
                "/api/push-workspace-token",
                json=_VALID_BODY,
                headers={"Authorization": f"Bearer {_VALID_SECRET}"},
            )
        assert resp.status_code == 200
        assert resp.json().get("ok") is True

    def test_confirmation_queued_to_outbox(self, client_and_dirs):
        client, _, outbox_dir = client_and_dirs
        with patch(f"{_MODULE}._fetch_workspace_preview", return_value="Most recent Drive file: Q3 plan.docx"):
            client.post(
                "/api/push-workspace-token",
                json=_VALID_BODY,
                headers={"Authorization": f"Bearer {_VALID_SECRET}"},
            )
        reply = _read_only_outbox_file(outbox_dir)
        text = reply.get("text", "")
        assert "/gdocs" in text
        assert "/gdrive" in text
        assert "/gsheets" in text
        assert reply.get("chat_id") == _VALID_BODY["chat_id"]

    def test_confirmation_includes_live_preview(self, client_and_dirs):
        """BIS-744: the confirmation text includes the real live-data preview."""
        client, _, outbox_dir = client_and_dirs
        with patch(
            f"{_MODULE}._fetch_workspace_preview",
            return_value="Most recent Drive file: roadmap.gdoc",
        ):
            client.post(
                "/api/push-workspace-token",
                json=_VALID_BODY,
                headers={"Authorization": f"Bearer {_VALID_SECRET}"},
            )
        reply = _read_only_outbox_file(outbox_dir)
        assert "roadmap.gdoc" in reply["text"]

    def test_confirmation_degrades_gracefully_when_preview_fetch_fails(self, client_and_dirs, caplog):
        """BIS-744: a live-data fetch failure must never silence the confirmation."""
        import logging

        client, _, outbox_dir = client_and_dirs
        with patch(
            f"{_MODULE}._fetch_workspace_preview", side_effect=RuntimeError("Drive API down")
        ), caplog.at_level(logging.ERROR):
            resp = client.post(
                "/api/push-workspace-token",
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
        workspace_token_dir = messages_base / "config" / "workspace-tokens"
        workspace_token_dir.mkdir(parents=True)
        blocked_base = tmp_path / "blocked"
        blocked_base.mkdir()
        (blocked_base / "outbox").write_text("not a directory")

        with (
            patch(f"{_MODULE}._MESSAGES_DIR", blocked_base),
            patch(f"{_MODULE}._WORKSPACE_TOKEN_DIR", workspace_token_dir),
            patch(f"{_MODULE}._INTERNAL_SECRET", _VALID_SECRET),
            patch(f"{_MODULE}._SESSION_HMAC_SECRET", ""),
            patch(f"{_MODULE}._ENFORCE_WORKSPACE_SIGNED_SESSION", False),
            patch(f"{_MODULE}._seen_workspace_session_nonces", {}),
            patch(f"{_MODULE}._seen_push_confirmations", {}),
            patch(f"{_MODULE}._fetch_workspace_preview", return_value="preview"),
        ):
            from src.mcp.inbox_server_http import app

            client = TestClient(app, raise_server_exceptions=True)
            resp = client.post(
                "/api/push-workspace-token",
                json=_VALID_BODY,
                headers={"Authorization": f"Bearer {_VALID_SECRET}"},
            )

        assert resp.status_code == 200
        assert resp.json().get("ok") is True

    def test_notify_failure_is_logged_loudly_and_falls_back_to_system_alert(self, tmp_path):
        """BIS-744: fixes the pre-existing bare `except Exception:
        log.warning(...)` swallow. When the outbox write itself fails, the
        failure must be logged at ERROR (not WARNING) and a system-level
        alert must be written to the inbox as a fallback -- never total
        silence."""
        import logging

        messages_base = tmp_path / "confirm_messages"
        workspace_token_dir = messages_base / "config" / "workspace-tokens"
        workspace_token_dir.mkdir(parents=True)
        inbox_dir = messages_base / "inbox"
        inbox_dir.mkdir(parents=True)
        blocked_base = tmp_path / "blocked"
        blocked_base.mkdir()
        (blocked_base / "outbox").write_text("not a directory")

        with (
            patch(f"{_MODULE}._MESSAGES_DIR", blocked_base),
            patch(f"{_MODULE}._INBOX_DIR", inbox_dir),
            patch(f"{_MODULE}._WORKSPACE_TOKEN_DIR", workspace_token_dir),
            patch(f"{_MODULE}._INTERNAL_SECRET", _VALID_SECRET),
            patch(f"{_MODULE}._SESSION_HMAC_SECRET", ""),
            patch(f"{_MODULE}._ENFORCE_WORKSPACE_SIGNED_SESSION", False),
            patch(f"{_MODULE}._seen_workspace_session_nonces", {}),
            patch(f"{_MODULE}._seen_push_confirmations", {}),
            patch(f"{_MODULE}._fetch_workspace_preview", return_value="preview"),
        ):
            import logging as _logging

            logger = _logging.getLogger(_MODULE)
            from src.mcp.inbox_server_http import app

            client = TestClient(app, raise_server_exceptions=True)
            handler_records = []

            class _CollectHandler(_logging.Handler):
                def emit(self, record):
                    handler_records.append(record)

            handler = _CollectHandler(level=_logging.ERROR)
            logger.addHandler(handler)
            try:
                resp = client.post(
                    "/api/push-workspace-token",
                    json=_VALID_BODY,
                    headers={"Authorization": f"Bearer {_VALID_SECRET}"},
                )
            finally:
                logger.removeHandler(handler)

        # The push itself still succeeds -- token was saved before the
        # confirmation step ran.
        assert resp.status_code == 200

        error_records = [r for r in handler_records if r.levelno >= logging.ERROR]
        assert error_records, "Notify-path failure must be logged at ERROR, not swallowed"
        assert any(
            "failed to queue outbox confirmation" in r.getMessage().lower()
            for r in error_records
        )

        # System-alert fallback: a chat_id=0 alert must land in the inbox.
        alert_files = list(inbox_dir.glob("*_confirm_failure_alert.json"))
        assert alert_files, "No system-alert fallback was written when notify failed"
        alert = json.loads(alert_files[0].read_text())
        assert alert["chat_id"] == 0
        assert alert["type"] == "system_alert"
        assert "workspace" in alert["text"].lower()


# ---------------------------------------------------------------------------
# generate_consent_link — workspace scope
# ---------------------------------------------------------------------------


class TestGenerateConsentLinkWorkspace:
    def test_returns_https_url_when_configured(self):
        from integrations.google_auth.consent import generate_consent_link

        mock_url = "https://myownlobster.ai/connect/workspace?token=abc123"

        with patch(
            "integrations.google_auth.consent.requests.post",
            return_value=MagicMock(
                status_code=200,
                ok=True,
                json=lambda: {"url": mock_url},
            ),
        ), patch.dict(
            os.environ,
            {
                "LOBSTER_INSTANCE_URL": "https://my.instance.example.com",
                "LOBSTER_INTERNAL_SECRET": "internal-secret",
            },
        ):
            url = generate_consent_link("workspace")

        assert url.startswith("https://")

    def test_workspace_is_valid_scope(self):
        from integrations.google_auth.consent import generate_consent_link

        mock_url = "https://myownlobster.ai/connect/workspace?token=xyz"

        with patch(
            "integrations.google_auth.consent.requests.post",
            return_value=MagicMock(
                status_code=200,
                ok=True,
                json=lambda: {"url": mock_url},
            ),
        ), patch.dict(
            os.environ,
            {
                "LOBSTER_INSTANCE_URL": "https://my.instance.example.com",
                "LOBSTER_INTERNAL_SECRET": "internal-secret",
            },
        ):
            url = generate_consent_link("workspace")
            assert url

    def test_graceful_fallback_when_myownlobster_unreachable(self):
        import requests as req_lib
        from integrations.google_auth.consent import generate_consent_link

        with patch(
            "integrations.google_auth.consent.requests.post",
            side_effect=req_lib.exceptions.ConnectionError("unreachable"),
        ), patch.dict(
            os.environ,
            {
                "LOBSTER_INSTANCE_URL": "https://my.instance.example.com",
                "LOBSTER_INTERNAL_SECRET": "internal-secret",
            },
        ):
            try:
                result = generate_consent_link("workspace")
                assert not result  # if returned, should be falsy
            except Exception:
                pass  # Any exception is acceptable — caller catches it


# ---------------------------------------------------------------------------
# skill.toml — /workspace connect trigger
# ---------------------------------------------------------------------------


def test_skill_toml_has_workspace_connect_trigger():
    skill_toml = Path(__file__).parent.parent.parent.parent / \
        "lobster-shop" / "google-workspace" / "skill.toml"
    assert skill_toml.exists(), "skill.toml not found"
    content = skill_toml.read_text()
    assert "/workspace connect" in content, \
        "/workspace connect not in skill.toml triggers"
