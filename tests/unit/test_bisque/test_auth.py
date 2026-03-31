"""Tests for bisque auth -- bootstrap exchange, session lifecycle, TTL."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from bisque.auth import TokenStore, create_bootstrap_token, handle_auth_exchange


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def tokens_file(tmp_path: Path) -> Path:
    """Create a tokens file with a known bootstrap token."""
    tf = tmp_path / "tokens.json"
    tf.write_text(json.dumps({
        "bootstrapTokens": {
            "boot-abc123": {"email": "test@example.com", "created": "2025-01-01T00:00:00Z"},
            "boot-xyz789": {"email": "other@example.com", "created": "2025-01-01T00:00:00Z"},
        },
        "sessionTokens": {},
    }))
    return tf


@pytest.fixture
def store(tokens_file: Path) -> TokenStore:
    return TokenStore(tokens_file, session_ttl=3600)


@pytest.fixture
def short_ttl_store(tokens_file: Path) -> TokenStore:
    """Store with 1-second TTL for expiry tests."""
    return TokenStore(tokens_file, session_ttl=0.1)


# =============================================================================
# Bootstrap token validation
# =============================================================================


class TestBootstrapTokens:
    def test_valid_bootstrap_token(self, store: TokenStore):
        valid, email = store.validate_bootstrap_token("boot-abc123")
        assert valid is True
        assert email == "test@example.com"

    def test_bootstrap_token_consumed(self, store: TokenStore):
        store.validate_bootstrap_token("boot-abc123")
        # Second use should fail — token consumed
        valid, email = store.validate_bootstrap_token("boot-abc123")
        assert valid is False
        assert email == ""

    def test_invalid_bootstrap_token(self, store: TokenStore):
        valid, email = store.validate_bootstrap_token("nonexistent")
        assert valid is False

    def test_empty_bootstrap_token(self, store: TokenStore):
        valid, email = store.validate_bootstrap_token("")
        assert valid is False

    def test_missing_tokens_file(self, tmp_path: Path):
        store = TokenStore(tmp_path / "nonexistent.json")
        valid, email = store.validate_bootstrap_token("anything")
        assert valid is False

    def test_corrupt_tokens_file(self, tmp_path: Path):
        tf = tmp_path / "tokens.json"
        tf.write_text("not valid json {{{")
        store = TokenStore(tf)
        valid, email = store.validate_bootstrap_token("anything")
        assert valid is False

    def test_bootstrap_no_email(self, tmp_path: Path):
        tf = tmp_path / "tokens.json"
        tf.write_text(json.dumps({
            "bootstrapTokens": {
                "no-email": {"created": "2025-01-01"},
            },
        }))
        store = TokenStore(tf)
        valid, email = store.validate_bootstrap_token("no-email")
        assert valid is False

    def test_second_bootstrap_token_still_works(self, store: TokenStore):
        store.validate_bootstrap_token("boot-abc123")
        valid, email = store.validate_bootstrap_token("boot-xyz789")
        assert valid is True
        assert email == "other@example.com"


# =============================================================================
# Session management
# =============================================================================


class TestSessionManagement:
    def test_create_session(self, store: TokenStore):
        token = store.create_session("test@example.com")
        assert len(token) > 20

    def test_validate_session(self, store: TokenStore):
        token = store.create_session("test@example.com")
        valid, email = store.validate_session(token)
        assert valid is True
        assert email == "test@example.com"

    def test_validate_invalid_session(self, store: TokenStore):
        valid, email = store.validate_session("fake-token")
        assert valid is False

    def test_validate_empty_session(self, store: TokenStore):
        valid, email = store.validate_session("")
        assert valid is False

    def test_session_expire(self, short_ttl_store: TokenStore):
        token = short_ttl_store.create_session("test@example.com")
        time.sleep(0.2)  # wait for TTL
        valid, email = short_ttl_store.validate_session(token)
        assert valid is False

    def test_touch_session(self, short_ttl_store: TokenStore):
        token = short_ttl_store.create_session("test@example.com")
        time.sleep(0.05)
        short_ttl_store.touch_session(token)
        valid, email = short_ttl_store.validate_session(token)
        assert valid is True

    def test_revoke_session(self, store: TokenStore):
        token = store.create_session("test@example.com")
        store.revoke_session(token)
        valid, email = store.validate_session(token)
        assert valid is False

    def test_revoke_nonexistent(self, store: TokenStore):
        store.revoke_session("fake")  # should not raise

    def test_cleanup_expired(self, short_ttl_store: TokenStore):
        short_ttl_store.create_session("a@test.com")
        short_ttl_store.create_session("b@test.com")
        time.sleep(0.2)
        removed = short_ttl_store.cleanup_expired()
        assert removed == 2
        assert short_ttl_store.active_session_count == 0

    def test_active_session_count(self, store: TokenStore):
        assert store.active_session_count == 0
        store.create_session("a@test.com")
        store.create_session("b@test.com")
        assert store.active_session_count == 2


# =============================================================================
# HTTP auth exchange
# =============================================================================


class TestAuthExchange:
    def test_exchange_success(self, store: TokenStore):
        status, body = handle_auth_exchange({"token": "boot-abc123"}, store)
        assert status == 200
        assert "sessionToken" in body
        assert body["email"] == "test@example.com"
        # Session token should be valid
        valid, email = store.validate_session(body["sessionToken"])
        assert valid is True

    def test_exchange_invalid_token(self, store: TokenStore):
        status, body = handle_auth_exchange({"token": "bad"}, store)
        assert status == 401
        assert "error" in body

    def test_exchange_missing_token(self, store: TokenStore):
        status, body = handle_auth_exchange({}, store)
        assert status == 400
        assert "error" in body

    def test_exchange_empty_token(self, store: TokenStore):
        status, body = handle_auth_exchange({"token": ""}, store)
        assert status == 400


# =============================================================================
# P1.4: Canonical bootstrap token schema (createdAt/expiresAt/used)
# =============================================================================


class TestCanonicalBootstrapSchema:
    """P1.4: Validate bootstrap token handling against the canonical schema.

    Canonical fields:
      - email: str
      - createdAt: ISO 8601 string
      - expiresAt: ISO 8601 string
      - used: bool
    """

    def _make_store(self, tmp_path: Path, token: str, record: dict) -> TokenStore:
        tf = tmp_path / "tokens.json"
        tf.write_text(json.dumps({"bootstrapTokens": {token: record}}))
        return TokenStore(tf)

    def test_canonical_token_valid(self, tmp_path: Path):
        """A canonical token that is not used and not expired should be accepted."""
        now = datetime.now(timezone.utc)
        store = self._make_store(tmp_path, "canonical-1", {
            "email": "user@example.com",
            "createdAt": now.isoformat(),
            "expiresAt": (now + timedelta(hours=24)).isoformat(),
            "used": False,
        })
        valid, email = store.validate_bootstrap_token("canonical-1")
        assert valid is True
        assert email == "user@example.com"

    def test_canonical_token_expired(self, tmp_path: Path):
        """An expired canonical token (expiresAt in the past) should be rejected."""
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        store = self._make_store(tmp_path, "expired-1", {
            "email": "user@example.com",
            "createdAt": (past - timedelta(hours=1)).isoformat(),
            "expiresAt": past.isoformat(),
            "used": False,
        })
        valid, _ = store.validate_bootstrap_token("expired-1")
        assert valid is False

    def test_canonical_token_used_flag(self, tmp_path: Path):
        """A canonical token with used=True should be rejected."""
        now = datetime.now(timezone.utc)
        store = self._make_store(tmp_path, "used-1", {
            "email": "user@example.com",
            "createdAt": now.isoformat(),
            "expiresAt": (now + timedelta(hours=24)).isoformat(),
            "used": True,
        })
        valid, _ = store.validate_bootstrap_token("used-1")
        assert valid is False

    def test_canonical_token_consumed_on_use(self, tmp_path: Path):
        """After a successful exchange the token must be removed from the store."""
        now = datetime.now(timezone.utc)
        store = self._make_store(tmp_path, "oneuse-1", {
            "email": "user@example.com",
            "createdAt": now.isoformat(),
            "expiresAt": (now + timedelta(hours=24)).isoformat(),
            "used": False,
        })
        valid1, _ = store.validate_bootstrap_token("oneuse-1")
        assert valid1 is True
        # Second call must fail — token was consumed
        valid2, _ = store.validate_bootstrap_token("oneuse-1")
        assert valid2 is False

    def test_legacy_token_no_expiry_accepted(self, tmp_path: Path):
        """Legacy tokens without expiresAt should still be accepted (backward compat)."""
        store = self._make_store(tmp_path, "legacy-1", {
            "email": "user@example.com",
            "created_at": time.time(),
        })
        valid, email = store.validate_bootstrap_token("legacy-1")
        assert valid is True
        assert email == "user@example.com"

    def test_unparseable_expires_at_rejected(self, tmp_path: Path):
        """A token with a malformed expiresAt should be rejected (security boundary)."""
        store = self._make_store(tmp_path, "badexp-1", {
            "email": "user@example.com",
            "expiresAt": "not-a-date",
            "used": False,
        })
        valid, _ = store.validate_bootstrap_token("badexp-1")
        assert valid is False


# =============================================================================
# P1.2: create_bootstrap_token writes canonical schema
# =============================================================================


class TestCreateBootstrapToken:
    def test_creates_token_with_canonical_fields(self, tmp_path: Path):
        """create_bootstrap_token must write createdAt/expiresAt/used: false."""
        tf = tmp_path / "tokens.json"
        tf.write_text("{}")
        store = TokenStore(tf)
        token = create_bootstrap_token("writer@example.com", store, ttl_seconds=3600)
        assert token

        data = json.loads(tf.read_text())
        record = data["bootstrapTokens"][token]
        assert record["email"] == "writer@example.com"
        assert record["used"] is False
        # Verify fields are parseable ISO strings
        datetime.fromisoformat(record["createdAt"].replace("Z", "+00:00"))
        datetime.fromisoformat(record["expiresAt"].replace("Z", "+00:00"))

    def test_created_token_can_be_exchanged(self, tmp_path: Path):
        """A freshly created bootstrap token should be exchangeable for a session."""
        tf = tmp_path / "tokens.json"
        tf.write_text("{}")
        store = TokenStore(tf)
        token = create_bootstrap_token("user@example.com", store, ttl_seconds=3600)
        valid, email = store.validate_bootstrap_token(token)
        assert valid is True
        assert email == "user@example.com"

    def test_expires_at_is_in_future(self, tmp_path: Path):
        """expiresAt must be in the future by approximately the requested TTL."""
        tf = tmp_path / "tokens.json"
        tf.write_text("{}")
        store = TokenStore(tf)
        before = datetime.now(timezone.utc)
        token = create_bootstrap_token("ttl@example.com", store, ttl_seconds=7200)
        after = datetime.now(timezone.utc)

        data = json.loads(tf.read_text())
        record = data["bootstrapTokens"][token]
        expires = datetime.fromisoformat(record["expiresAt"].replace("Z", "+00:00"))

        # Should expire between ~2h from before and ~2h+epsilon from after
        assert expires > before + timedelta(hours=1, minutes=59)
        assert expires < after + timedelta(hours=2, minutes=1)
