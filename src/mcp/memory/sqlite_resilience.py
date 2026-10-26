"""
SQLite write resilience helpers for shared, multi-connection WAL databases.

Context: memory.db is shared by two independent subsystems, each holding
its own long-lived connection:
  - VectorMemory (src/mcp/memory/vector_memory.py) - the `events` table
  - UserModel    (src/mcp/user_model/db.py)        - the `um_*` tables

Independent connections to the same WAL-mode SQLite file can collide on
writes ("database is locked" / sqlite3.OperationalError) even though WAL
mode allows concurrent readers, because only one writer may hold the
write lock at a time. Python's sqlite3 default busy timeout is a flat
5 seconds with no backoff and no retry - a write that loses the race
once simply raises and is lost unless the caller handles it.

This module provides pure, composable helpers so every write site can
treat "the database was briefly busy" as a *retryable* condition rather
than a terminal failure:

    - `busy_timeout_ms(conn, ms)`      -> set PRAGMA busy_timeout
    - `is_retryable_lock_error(exc)`   -> predicate, no side effects
    - `with_retry(fn, ...)`            -> higher-order retry/backoff wrapper
    - `WriteResult`                    -> explicit success/failure value,
                                           so callers can't accidentally
                                           swallow a failure by forgetting
                                           to check a return value's shape

Nothing here swallows exceptions silently: `with_retry` either returns the
wrapped function's result, or raises the last exception after retries are
exhausted. There is no bare `except: pass` anywhere in this module.
"""

from __future__ import annotations

import logging
import random
import sqlite3
import time
from dataclasses import dataclass
from typing import Callable, Generic, TypeVar

log = logging.getLogger("lobster-memory")

T = TypeVar("T")

# Matches SQLite's two lock-contention error strings across versions:
# "database is locked" (SQLITE_BUSY) and "database table is locked"
# (SQLITE_LOCKED, e.g. contending on a shared cache or FTS trigger write).
_RETRYABLE_MESSAGES = ("database is locked", "database table is locked")

DEFAULT_BUSY_TIMEOUT_MS = 5_000
DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_BASE_DELAY_S = 0.05
DEFAULT_MAX_DELAY_S = 1.0


def busy_timeout_ms(conn: sqlite3.Connection, ms: int = DEFAULT_BUSY_TIMEOUT_MS) -> None:
    """Set SQLite's internal busy-wait timeout on a connection.

    This makes SQLite itself retry internally (via its busy handler) for up
    to `ms` milliseconds before raising "database is locked" - a first line
    of defense that resolves the vast majority of short write collisions
    without ever reaching application-level retry logic.
    """
    conn.execute(f"PRAGMA busy_timeout = {int(ms)}")


def is_retryable_lock_error(exc: BaseException) -> bool:
    """Pure predicate: does this exception represent transient SQLite lock
    contention that is safe to retry, as opposed to a real error (bad SQL,
    constraint violation, corruption, etc.)?
    """
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    message = str(exc).lower()
    return any(marker in message for marker in _RETRYABLE_MESSAGES)


@dataclass(frozen=True)
class WriteResult(Generic[T]):
    """Explicit success/failure result for a resilient write.

    Using a typed result (rather than returning `None` or `-1` on failure)
    forces callers to make a decision about failure instead of silently
    treating a sentinel value as a normal ID.
    """

    ok: bool
    value: T | None = None
    error: BaseException | None = None
    attempts: int = 1

    @staticmethod
    def success(value: T, attempts: int = 1) -> "WriteResult[T]":
        return WriteResult(ok=True, value=value, attempts=attempts)

    @staticmethod
    def failure(error: BaseException, attempts: int) -> "WriteResult[T]":
        return WriteResult(ok=False, error=error, attempts=attempts)


def with_retry(
    fn: Callable[[], T],
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    base_delay_s: float = DEFAULT_BASE_DELAY_S,
    max_delay_s: float = DEFAULT_MAX_DELAY_S,
    on_retry: Callable[[int, BaseException], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Call `fn()`, retrying with exponential backoff + jitter on transient
    SQLite lock contention.

    Raises the final exception if all attempts are exhausted -- this never
    silently swallows a failure. Non-retryable exceptions propagate
    immediately on the first attempt.

    Args:
        fn: Zero-arg callable performing the write (and any necessary commit).
        max_attempts: Total attempts including the first (not just retries).
        base_delay_s: Initial backoff delay.
        max_delay_s: Backoff delay ceiling.
        on_retry: Optional callback invoked as (attempt_number, exception)
            before each retry sleep - used for logging/metrics by callers.
        sleep: Injectable sleep function, so tests can run without real delay.
    """
    last_exc: BaseException | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return fn()
        except sqlite3.OperationalError as exc:
            if not is_retryable_lock_error(exc):
                raise
            last_exc = exc
            if attempt >= max_attempts:
                break
            if on_retry is not None:
                on_retry(attempt, exc)
            delay = min(max_delay_s, base_delay_s * (2 ** (attempt - 1)))
            delay += random.uniform(0, delay * 0.25)
            sleep(delay)

    assert last_exc is not None  # loop always sets this before breaking
    raise last_exc
