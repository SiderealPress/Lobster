"""
Reliability utilities for Lobster MCP server.

Addresses common agent failure patterns:
- Atomic file writes (prevents corruption on crash)
- Input validation (prevents silent bad data)
- Structured audit logging (observability)
- Idempotency helpers (prevents duplicate processing)

Design principles:
- Each function is pure or has a single side effect
- No global mutable state
- Failures are explicit, never swallowed
"""

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Re-export atomic filesystem helpers from the canonical location.
# ---------------------------------------------------------------------------
# The canonical implementations live in utils/fs.py.  We re-export them here
# so that existing callers of `from reliability import atomic_write_json`
# continue to work without change.

_SRC_DIR = str(Path(__file__).resolve().parent.parent)
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from utils.fs import atomic_write_json, safe_move  # noqa: E402, F401


# =============================================================================
# Input Validation
# =============================================================================
# Problem: Tool arguments from LLMs can be subtly wrong - wrong types,
# out-of-range values, missing fields. Without validation, these propagate
# as silent corruption (Composio issues #3, #7).
#
# Solution: Validate at the boundary. Return clear error messages.
# =============================================================================

class ValidationError(Exception):
    """Raised when input validation fails. Contains a user-facing message."""
    pass


def validate_send_reply_args(args: dict) -> dict:
    """Validate and normalize send_reply arguments.

    Returns normalized args dict.
    Raises ValidationError with descriptive message on invalid input.
    """
    chat_id = args.get("chat_id")
    text = args.get("text", "")
    source = args.get("source", "telegram").lower()

    # chat_id: required, must be int or non-empty string
    if chat_id is None:
        raise ValidationError("chat_id is required")
    if isinstance(chat_id, str) and not chat_id.strip():
        raise ValidationError("chat_id cannot be empty string")
    if isinstance(chat_id, float):
        chat_id = int(chat_id)  # LLMs sometimes send floats

    # text: required, non-empty, reasonable length
    if not text or not text.strip():
        raise ValidationError("text is required and cannot be empty")
    # Sanity cap only — do NOT truncate at Telegram's per-message limit here.
    # The bot's _prepare_send_items() pipeline handles splitting long messages
    # into multiple chunks before they reach the Telegram API. Truncating here
    # silently drops content for messages between 4096 and ~12000 chars.
    if len(text) > 100_000:
        text = text[:99_997] + "..."

    # source: must be a known source
    valid_sources = {"telegram", "slack", "sms", "signal", "whatsapp", "bisque"}
    if source not in valid_sources:
        raise ValidationError(
            f"Invalid source '{source}'. Must be one of: {', '.join(sorted(valid_sources))}"
        )

    return {
        **args,
        "chat_id": chat_id,
        "text": text,
        "source": source,
    }


def validate_message_id(message_id: Any) -> str:
    """Validate a message_id is a non-empty string.

    Raises ValidationError if invalid.
    """
    if not message_id:
        raise ValidationError("message_id is required")
    if not isinstance(message_id, str):
        message_id = str(message_id)
    if not message_id.strip():
        raise ValidationError("message_id cannot be empty")
    # Guard against path traversal
    if ".." in message_id or "/" in message_id:
        raise ValidationError("message_id contains invalid characters")
    return message_id


# =============================================================================
# Audit Logging
# =============================================================================
# Problem: No structured record of what the agent did. When things go wrong,
# there's no way to reconstruct what happened (Composio issues #6, #10).
#
# Solution: Append-only audit log with structured entries. Each entry records
# what tool was called, what arguments were passed, and the outcome.
# The log is separate from the application log to avoid noise.
# =============================================================================

_AUDIT_LOG_PATH = None  # Set during init


def init_audit_log(log_dir: Path) -> None:
    """Initialize the audit log file path."""
    global _AUDIT_LOG_PATH
    log_dir.mkdir(parents=True, exist_ok=True)
    _AUDIT_LOG_PATH = log_dir / "audit.jsonl"


def audit_log(
    tool: str,
    args: dict | None = None,
    result: str = "",
    error: str = "",
    duration_ms: int | None = None,
) -> None:
    """Append a structured audit entry (JSONL format).

    Each line is a self-contained JSON object, making the file:
    - Append-only (crash-safe, no corruption of existing entries)
    - Easy to grep/filter
    - Parseable line-by-line (no need to load entire file)

    Args:
        tool: Tool name that was called.
        args: Sanitized arguments (never log secrets).
        result: Brief result summary.
        error: Error message if the call failed.
        duration_ms: How long the call took.
    """
    if _AUDIT_LOG_PATH is None:
        return  # Not initialized yet

    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "tool": tool,
    }

    if args:
        # Sanitize: redact potential secrets, truncate large values
        sanitized = {}
        for k, v in args.items():
            if k in ("text", "output", "context", "body", "description"):
                s = str(v)
                sanitized[k] = s[:200] + "..." if len(s) > 200 else s
            elif k in ("token", "password", "secret", "api_key"):
                sanitized[k] = "[REDACTED]"
            else:
                sanitized[k] = v
        entry["args"] = sanitized

    if result:
        entry["result"] = result[:500]
    if error:
        entry["error"] = error[:500]
    if duration_ms is not None:
        entry["duration_ms"] = duration_ms

    try:
        line = json.dumps(entry, default=str) + "\n"
        with open(_AUDIT_LOG_PATH, "a") as f:
            f.write(line)
    except Exception:
        pass  # Audit logging must never crash the main process


# =============================================================================
# Idempotency
# =============================================================================
# Problem: If the agent crashes after sending a reply but before marking
# the message as processed, it will re-process and send a duplicate reply
# on restart (Composio issue #9).
#
# Solution: Track recently processed message IDs in a set with TTL.
# Check before processing; skip if already seen.
# =============================================================================

class IdempotencyTracker:
    """Tracks recently processed items to prevent duplicate processing.

    Uses an in-memory set with TTL-based expiry. Not persistent across
    restarts (by design - the file-based state directories handle that).
    This catches duplicates within a single session.
    """

    def __init__(self, ttl_seconds: int = 600):
        self._seen: dict[str, float] = {}  # id -> timestamp
        self._ttl = ttl_seconds

    def check_and_mark(self, item_id: str) -> bool:
        """Check if item was recently processed. If not, mark it.

        Returns True if this is a NEW item (not seen before).
        Returns False if this is a DUPLICATE (already processed).
        """
        self._evict_expired()
        if item_id in self._seen:
            return False
        self._seen[item_id] = time.time()
        return True

    def _evict_expired(self) -> None:
        """Remove entries older than TTL."""
        now = time.time()
        cutoff = now - self._ttl
        self._seen = {k: v for k, v in self._seen.items() if v > cutoff}


# =============================================================================
# Circuit Breaker
# =============================================================================
# Problem: When an external service (Telegram API, GitHub, etc.) is down,
# the agent keeps retrying and wasting resources (Composio issue #5).
#
# Solution: Circuit breaker pattern from distributed systems.
# After N consecutive failures, stop trying for a cooldown period.
# =============================================================================

class CircuitBreaker:
    """Simple circuit breaker for external service calls.

    States:
    - CLOSED: Normal operation, calls pass through
    - OPEN: Too many failures, calls are rejected immediately
    - HALF_OPEN: After cooldown, allow one test call

    This is the standard circuit breaker pattern from Michael Nygard's
    "Release It!" - a well-established reliability pattern.
    """

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        cooldown_seconds: int = 60,
    ):
        self.name = name
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self._state = self.CLOSED
        self._failure_count = 0
        self._last_failure_time = 0.0

    @property
    def state(self) -> str:
        if self._state == self.OPEN:
            # Check if cooldown has elapsed
            if time.time() - self._last_failure_time >= self.cooldown_seconds:
                self._state = self.HALF_OPEN
        return self._state

    def allow_request(self) -> bool:
        """Check if a request should be allowed through."""
        s = self.state
        if s == self.CLOSED:
            return True
        if s == self.HALF_OPEN:
            return True  # Allow one test request
        return False  # OPEN - reject

    def record_success(self) -> None:
        """Record a successful call. Resets the breaker."""
        self._failure_count = 0
        self._state = self.CLOSED

    def record_failure(self) -> None:
        """Record a failed call. May trip the breaker."""
        self._failure_count += 1
        self._last_failure_time = time.time()
        if self._failure_count >= self.failure_threshold:
            self._state = self.OPEN

    def status(self) -> dict:
        """Return current status for observability."""
        return {
            "name": self.name,
            "state": self.state,
            "failure_count": self._failure_count,
            "failure_threshold": self.failure_threshold,
            "cooldown_seconds": self.cooldown_seconds,
        }
