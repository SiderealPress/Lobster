"""Tests for bisque auth -- bootstrap exchange, session lifecycle, TTL."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from bisque.auth import TokenStore, handle_auth_exchange


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
