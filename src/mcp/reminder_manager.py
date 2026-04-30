"""
Reminder primitives for Lobster — create_reminder / list_reminders / cancel_reminder.

Reminders are one-shot inbox messages delivered at a specific UTC time.
Under the hood each reminder is backed by a systemd one-shot timer that invokes
post-reminder.sh, exactly as if the user had called create_scheduled_job manually.
The difference is that this module:

  1. Generates the job name and post-reminder.sh invocation automatically
  2. Persists reminder metadata (reminder_type, fire_time_utc, metadata) in a
     registry so callers can list and cancel reminders without knowing about
     the underlying systemd units.

The registry file lives at: ~/lobster-workspace/reminders/reminders.json
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from systemd_jobs import (
    create_job,
    delete_job,
    LOBSTER_USER,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

LOBSTER_HOME = f"/home/{LOBSTER_USER}"
LOBSTER_WORKSPACE = Path(LOBSTER_HOME) / "lobster-workspace"
REMINDERS_DIR = LOBSTER_WORKSPACE / "reminders"
REGISTRY_FILE = REMINDERS_DIR / "reminders.json"

# post-reminder.sh lives in the lobster repo at ~/lobster/scripts/
POST_REMINDER_SCRIPT = Path(LOBSTER_HOME) / "lobster" / "scripts" / "post-reminder.sh"

# Reminder job name prefix — must stay within systemd_jobs MAX_NAME_LEN (50)
REMINDER_NAME_PREFIX = "rem-"

# Maximum length for reminder_type (used in job name)
MAX_REMINDER_TYPE_LEN = 30

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class ReminderInfo:
    reminder_id: str
    reminder_type: str
    fire_time_utc: str       # ISO 8601 UTC string
    metadata: dict
    created_at: str          # ISO 8601 UTC string
    cancelled: bool = False  # True if cancel_reminder was called


@dataclass
class CreateReminderResult:
    reminder_id: str
    fire_time_utc: str
    job_name: str            # underlying systemd job name


@dataclass
class CancelReminderResult:
    reminder_id: str
    cancelled: bool


# ---------------------------------------------------------------------------
# Reminder ID and job name generation (pure functions)
# ---------------------------------------------------------------------------


def _validate_reminder_type(reminder_type: str) -> Optional[str]:
    """Return error string or None if valid."""
    if not reminder_type:
        return "reminder_type cannot be empty"
    if len(reminder_type) > MAX_REMINDER_TYPE_LEN:
        return f"reminder_type must be {MAX_REMINDER_TYPE_LEN} characters or less"
    if not re.match(r'^[a-zA-Z0-9_-]+$', reminder_type):
        return "reminder_type must be alphanumeric with underscores or hyphens"
    return None


def _validate_fire_time(fire_time_utc: str) -> Optional[str]:
    """Return error string or None if the fire_time_utc is a valid future UTC ISO 8601 string."""
    if not fire_time_utc:
        return "fire_time_utc cannot be empty"
    try:
        dt = _parse_fire_time(fire_time_utc)
    except ValueError as exc:
        return f"fire_time_utc is not a valid ISO 8601 UTC datetime: {exc}"
    now = datetime.now(tz=timezone.utc)
    if dt <= now:
        return f"fire_time_utc must be in the future (got {fire_time_utc!r})"
    return None


def _parse_fire_time(fire_time_utc: str) -> datetime:
    """Parse an ISO 8601 UTC string. Raises ValueError on failure."""
    # Accept both 'Z' suffix and '+00:00' offset
    normalized = fire_time_utc.replace("Z", "+00:00")
    dt = datetime.fromisoformat(normalized)
    if dt.tzinfo is None:
        raise ValueError("datetime must include timezone info — use UTC (Z or +00:00)")
    return dt.astimezone(timezone.utc)


def _make_reminder_id(reminder_type: str, now_utc: datetime) -> str:
    """Generate a stable, unique reminder ID."""
    epoch_ms = int(now_utc.timestamp() * 1000)
    return f"{reminder_type}-{epoch_ms}"


def _make_job_name(reminder_id: str) -> str:
    """Derive a systemd-safe job name from a reminder ID.

    Job names are prefixed with 'rem-' and use a short hash of the reminder_id
    to stay within MAX_NAME_LEN (50 chars) while remaining unique.
    """
    short_hash = hashlib.sha256(reminder_id.encode()).hexdigest()[:12]
    return f"{REMINDER_NAME_PREFIX}{short_hash}"


def _format_systemd_calendar(dt: datetime) -> str:
    """Format a UTC datetime as a systemd OnCalendar one-shot timestamp.

    Example: '2026-05-01 09:00:00 UTC'
    systemd interprets this as a one-shot calendar event at that exact instant.
    """
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC")


def _build_command(reminder_type: str, reminder_id: str) -> str:
    """Build the ExecStart command for a reminder service unit.

    Passes reminder_id as the second argument so post-reminder.sh can include it
    in the inbox message, allowing the dispatcher to look up metadata from the
    registry when the reminder fires.
    """
    script = str(POST_REMINDER_SCRIPT)
    return f"{script} {reminder_type} {reminder_id}"


# ---------------------------------------------------------------------------
# Registry I/O (pure read/write — side effects isolated here)
# ---------------------------------------------------------------------------


def _load_registry() -> list[dict]:
    """Load the reminders registry. Returns empty list if file does not exist."""
    if not REGISTRY_FILE.exists():
        return []
    try:
        data = json.loads(REGISTRY_FILE.read_text())
        if isinstance(data, list):
            return data
        return []
    except (json.JSONDecodeError, OSError):
        return []


def _save_registry(entries: list[dict]) -> None:
    """Atomically write the reminders registry."""
    REMINDERS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = REGISTRY_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(entries, indent=2))
    tmp.replace(REGISTRY_FILE)


def _registry_add(entry: dict) -> None:
    """Append a reminder entry to the registry (atomic)."""
    entries = _load_registry()
    entries.append(entry)
    _save_registry(entries)


def _registry_mark_cancelled(reminder_id: str) -> bool:
    """Mark a reminder as cancelled in the registry. Returns True if found."""
    entries = _load_registry()
    found = False
    for e in entries:
        if e.get("reminder_id") == reminder_id:
            e["cancelled"] = True
            found = True
            break
    if found:
        _save_registry(entries)
    return found


def _registry_get(reminder_id: str) -> Optional[dict]:
    """Return the registry entry for the given reminder_id, or None."""
    for e in _load_registry():
        if e.get("reminder_id") == reminder_id:
            return e
    return None


# ---------------------------------------------------------------------------
# High-level operations (async — call systemd_jobs under the hood)
# ---------------------------------------------------------------------------


async def create_reminder(
    reminder_type: str,
    fire_time_utc: str,
    metadata: Optional[dict] = None,
    *,
    now_utc: Optional[datetime] = None,
) -> CreateReminderResult:
    """Create a one-shot reminder that fires at fire_time_utc.

    Under the hood:
      1. Generate a unique reminder_id and derive a systemd job name
      2. Create a one-shot systemd timer (OnCalendar= at the exact datetime)
         whose service unit runs post-reminder.sh <reminder_type>
      3. Persist metadata to the reminders registry

    Returns CreateReminderResult with reminder_id, fire_time_utc, and job_name.
    Raises ValueError if validation fails.
    Raises RuntimeError (from systemd_jobs) if systemd operations fail.
    """
    err = _validate_reminder_type(reminder_type)
    if err:
        raise ValueError(err)
    err = _validate_fire_time(fire_time_utc)
    if err:
        raise ValueError(err)

    if now_utc is None:
        now_utc = datetime.now(tz=timezone.utc)

    reminder_id = _make_reminder_id(reminder_type, now_utc)
    job_name = _make_job_name(reminder_id)
    dt = _parse_fire_time(fire_time_utc)
    schedule = _format_systemd_calendar(dt)
    command = _build_command(reminder_type, reminder_id)
    description = f"Lobster reminder: {reminder_type}"

    await create_job(job_name, schedule, command, description)

    entry: dict = {
        "reminder_id": reminder_id,
        "reminder_type": reminder_type,
        "fire_time_utc": dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "metadata": metadata or {},
        "created_at": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "cancelled": False,
        "job_name": job_name,
    }
    _registry_add(entry)

    return CreateReminderResult(
        reminder_id=reminder_id,
        fire_time_utc=entry["fire_time_utc"],
        job_name=job_name,
    )


def list_reminders(
    pending_only: bool = True,
    *,
    now_utc: Optional[datetime] = None,
) -> list[ReminderInfo]:
    """Return reminders from the registry.

    When pending_only=True (default), only reminders whose fire_time_utc is in
    the future AND that have not been cancelled are returned. Already-fired
    reminders (fire_time_utc in the past) are excluded.

    Returns a list of ReminderInfo sorted by fire_time_utc ascending.
    """
    if now_utc is None:
        now_utc = datetime.now(tz=timezone.utc)

    entries = _load_registry()
    results: list[ReminderInfo] = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        cancelled = bool(e.get("cancelled", False))
        if pending_only and cancelled:
            continue

        fire_time_str = e.get("fire_time_utc", "")
        if pending_only and fire_time_str:
            try:
                fire_dt = _parse_fire_time(fire_time_str)
                if fire_dt <= now_utc:
                    continue  # already fired
            except ValueError:
                continue

        results.append(ReminderInfo(
            reminder_id=e.get("reminder_id", ""),
            reminder_type=e.get("reminder_type", ""),
            fire_time_utc=fire_time_str,
            metadata=e.get("metadata", {}),
            created_at=e.get("created_at", ""),
            cancelled=cancelled,
        ))

    results.sort(key=lambda r: r.fire_time_utc)
    return results


async def cancel_reminder(reminder_id: str) -> CancelReminderResult:
    """Cancel a pending reminder by stopping and removing its systemd timer.

    If the reminder is not found in the registry, returns cancelled=False.
    If the reminder was already cancelled, returns cancelled=False.
    Otherwise stops the systemd timer, removes unit files, and marks the
    registry entry as cancelled.
    """
    entry = _registry_get(reminder_id)
    if not entry:
        return CancelReminderResult(reminder_id=reminder_id, cancelled=False)

    if entry.get("cancelled"):
        return CancelReminderResult(reminder_id=reminder_id, cancelled=False)

    job_name = entry.get("job_name", _make_job_name(reminder_id))
    await delete_job(job_name)
    _registry_mark_cancelled(reminder_id)

    return CancelReminderResult(reminder_id=reminder_id, cancelled=True)
