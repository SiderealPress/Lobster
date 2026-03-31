"""
Systemd timer-based scheduling backend for Lobster scheduled jobs.

Replaces the cron + jobs.json backend. Each job is backed by a pair of
systemd unit files:
  /etc/systemd/system/lobster-<name>.timer
  /etc/systemd/system/lobster-<name>.service

All managed units carry a "# LOBSTER-MANAGED" comment in the [Unit] section.
Only units with this marker are touched by this module.

All systemctl calls use sudo. The lobster user is expected to have NOPASSWD
for the commands used here.
"""

from __future__ import annotations

import asyncio
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SYSTEMD_DIR = Path("/etc/systemd/system")
UNIT_PREFIX = "lobster-"
LOBSTER_MARKER = "# LOBSTER-MANAGED"
LOBSTER_USER = "lobster"

# Maximum name length (prefix + name must fit comfortably in a unit filename)
MAX_NAME_LEN = 50

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class JobInfo:
    name: str
    schedule: str          # OnCalendar= value
    command: str           # ExecStart= value
    description: str
    active: bool
    last_run: Optional[str]
    next_run: Optional[str]


@dataclass(frozen=True)
class CreateResult:
    name: str
    status: str            # "created" | "already_exists"


@dataclass(frozen=True)
class UpdateResult:
    name: str
    updated_fields: list[str]


@dataclass(frozen=True)
class DeleteResult:
    name: str
    status: str            # "deleted" | "not_found"


# ---------------------------------------------------------------------------
# Cron-to-systemd calendar converter (pure function — no I/O)
# ---------------------------------------------------------------------------

# Regex to detect a 5-field cron expression: min hour dom month dow
# Each field is: * OR */N OR digit-based expression (numbers, commas, hyphens, slashes)
_CRON_FIELD = r'(?:\*(?:/\d+)?|\d[\d,\-/]*)'
_CRON_RE = re.compile(
    r'^'
    r'(' + _CRON_FIELD + r')\s+'   # minute (group 1)
    r'(' + _CRON_FIELD + r')\s+'   # hour (group 2)
    r'(' + _CRON_FIELD + r')\s+'   # day-of-month (group 3)
    r'(' + _CRON_FIELD + r')\s+'   # month (group 4)
    r'(' + _CRON_FIELD + r')'      # day-of-week (group 5)
    r'$'
)

# Day-of-week names used in cron (0=Sun or 7=Sun, 1=Mon … 6=Sat)
_DOW_NAMES = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]


def _cron_field_to_systemd(value: str, kind: str) -> Optional[str]:
    """Convert a single cron field to its systemd calendar equivalent.

    Returns None if the field is too complex to convert (step expressions on
    non-wildcard bases, comma-separated lists of ranges, etc.).

    kind is one of: 'minute', 'hour', 'dom', 'month', 'dow'
    """
    if value == "*":
        return "*"

    # Simple integer
    if re.match(r'^\d+$', value):
        return value.zfill(2) if kind in ("minute", "hour") else value

    # */N  — every N units
    m = re.match(r'^\*/(\d+)$', value)
    if m:
        step = int(m.group(1))
        if kind == "minute":
            return f"*:0/{step:02d}"  # handled specially by caller
        if kind == "hour":
            return f"0/{step}"
        return None  # dom/month/dow step — too complex

    # Comma-separated simple integers
    if re.match(r'^\d+(,\d+)+$', value):
        parts = value.split(",")
        return ",".join(p.zfill(2) if kind in ("minute", "hour") else p for p in parts)

    return None  # anything else (ranges with steps, etc.) — too complex


def convert_cron_to_systemd(expr: str) -> Optional[str]:
    """Convert a 5-field cron expression to a systemd OnCalendar string.

    Returns the converted string on success, or None if the expression cannot
    be automatically converted (the caller should then reject with a clear error).

    Supports:
    - Wildcards: * * * * *  → *-*-* *:*:00
    - Specific times: 0 9 * * *  → *-*-* 09:00:00
    - Minute steps: */5 * * * *  → *-*-* *:0/5:00
    - Hour steps:   0 */2 * * *  → *-*-* 0/2:00:00
    - Day-of-week:  0 0 * * 1  → Mon *-*-* 00:00:00
    - Comma minute: 0,30 * * * *  → *-*-* *:00,30:00
    """
    m = _CRON_RE.match(expr.strip())
    if not m:
        return None

    cron_min, cron_hour, cron_dom, cron_month, cron_dow = m.groups()

    # --- day-of-week ---
    dow_prefix = ""
    if cron_dow != "*":
        # Only handle single simple dow values (0-7)
        if re.match(r'^\d$', cron_dow):
            idx = int(cron_dow) % 7  # map 7 → 0 (both = Sunday)
            dow_prefix = _DOW_NAMES[idx] + " "
        else:
            return None  # complex dow (ranges, lists) — skip

    # --- month / dom --- (only wildcards or simple values handled)
    if cron_month != "*" and not re.match(r'^\d{1,2}$', cron_month):
        return None
    if cron_dom != "*" and not re.match(r'^\d{1,2}$', cron_dom):
        return None

    date_part = (
        f"*-{cron_month.zfill(2)}-{cron_dom.zfill(2)}"
        if cron_month != "*" or cron_dom != "*"
        else "*-*-*"
    )
    if cron_month == "*" and cron_dom != "*":
        date_part = f"*-*-{cron_dom.zfill(2)}"
    elif cron_month != "*" and cron_dom == "*":
        date_part = f"*-{cron_month.zfill(2)}-*"

    # --- minute / hour ---
    # Special case: */N minute with wildcard hour → *:0/N:00
    m_min_step = re.match(r'^\*/(\d+)$', cron_min)
    if m_min_step and cron_hour == "*":
        step = int(m_min_step.group(1))
        time_part = f"*:0/{step:02d}:00"
        return f"{dow_prefix}{date_part} {time_part}"
    # */N minute with a specific hour (e.g. */15 9 * * *) cannot be cleanly
    # expressed in systemd OnCalendar format — return None so the caller
    # emits a helpful error rather than silently dropping the hour constraint.
    if m_min_step and cron_hour != "*":
        return None

    # Hour step: 0 */N * * * → *-*-* 0/N:00:00
    m_hour_step = re.match(r'^\*/(\d+)$', cron_hour)
    if m_hour_step and cron_min != "*":
        step = int(m_hour_step.group(1))
        min_val = cron_min.zfill(2) if re.match(r'^\d+$', cron_min) else None
        if min_val is None:
            return None
        time_part = f"0/{step}:{min_val}:00"
        return f"{dow_prefix}{date_part} {time_part}"

    # General: resolve minute and hour fields
    sys_min = _cron_field_to_systemd(cron_min, "minute")
    sys_hour = _cron_field_to_systemd(cron_hour, "hour")
    if sys_min is None or sys_hour is None:
        return None

    # If minute came back as a *:0/N style string (shouldn't happen here, but guard)
    if ":" in sys_min:
        return f"{dow_prefix}{date_part} {sys_min}:00"

    time_part = f"{sys_hour if sys_hour != '*' else '*'}:{sys_min}:00"
    return f"{dow_prefix}{date_part} {time_part}"


def is_cron_expression(expr: str) -> bool:
    """Return True if the string looks like a 5-field cron expression."""
    return bool(_CRON_RE.match(expr.strip()))


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_name(name: str) -> Optional[str]:
    """Return an error message string, or None if the name is valid."""
    if not name:
        return "name cannot be empty"
    if not re.match(r'^[a-z0-9][a-z0-9-]*[a-z0-9]$|^[a-z0-9]$', name):
        return "name must be lowercase alphanumeric with hyphens, cannot start/end with hyphen"
    if len(name) > MAX_NAME_LEN:
        return f"name must be {MAX_NAME_LEN} characters or less"
    return None


def validate_command(command: str) -> Optional[str]:
    """Return an error message string, or None if command is valid.

    Checks: non-empty, absolute path, and that the executable file exists.
    The executable is the first whitespace-separated token of the command.
    """
    if not command:
        return "command cannot be empty"
    if not command.startswith("/"):
        return "command must be an absolute path (must start with /)"
    # Extract the executable (first token, before any arguments)
    executable = command.split()[0]
    if not Path(executable).exists():
        return f"Command not found: {executable}"
    return None


def validate_schedule(schedule: str) -> Optional[str]:
    """Return an error message string, or None if the schedule is valid.

    If the input looks like a 5-field cron expression, it is automatically
    converted to systemd OnCalendar format. If conversion is not possible,
    an error is returned with guidance.

    For all other expressions, systemd-analyze calendar is used to validate
    the value — this catches typos and unsupported syntax before the unit
    file is written.
    """
    _, err = normalize_schedule(schedule)
    return err


def normalize_schedule(schedule: str) -> tuple[str, Optional[str]]:
    """Normalize a schedule string and return (normalized, error).

    If the schedule is a cron expression, converts it to systemd calendar
    format. Then validates the result using systemd-analyze.

    Returns (normalized_schedule, None) on success.
    Returns (schedule, error_message) on failure.
    """
    if not schedule:
        return schedule, "schedule cannot be empty"

    # Auto-convert cron expressions
    if is_cron_expression(schedule):
        converted = convert_cron_to_systemd(schedule)
        if converted is None:
            return schedule, (
                f"Cannot auto-convert cron expression '{schedule}' to systemd calendar format. "
                "Use systemd OnCalendar syntax instead (e.g., '*-*-* 09:00:00' for daily at 9am, "
                "'*:0/30:00' for every 30 minutes). "
                "See: https://www.freedesktop.org/software/systemd/man/systemd.time.html"
            )
        schedule = converted

    # Validate with systemd-analyze calendar
    try:
        result = subprocess.run(
            ["systemd-analyze", "calendar", schedule],
            capture_output=True,
            timeout=5,
        )
        if result.returncode != 0:
            err_text = result.stderr.decode(errors="replace").strip() or result.stdout.decode(errors="replace").strip()
            return schedule, (
                f"Invalid schedule '{schedule}': {err_text}. "
                "Use systemd OnCalendar syntax (e.g., '*-*-* 09:00:00', 'daily', 'hourly', '*:0/15:00')."
            )
    except (OSError, subprocess.TimeoutExpired):
        # systemd-analyze not available or timed out — fall back to permissive validation
        pass

    return schedule, None


# ---------------------------------------------------------------------------
# Unit file generation (pure functions — no I/O)
# ---------------------------------------------------------------------------

def _timer_unit(name: str, schedule: str, description: str) -> str:
    desc = description or f"Lobster scheduled job: {name}"
    return f"""[Unit]
Description={desc}
{LOBSTER_MARKER}

[Timer]
OnCalendar={schedule}
Persistent=true

[Install]
WantedBy=timers.target
"""


def _service_unit(name: str, command: str, description: str) -> str:
    desc = description or f"Lobster job: {name}"
    return f"""[Unit]
Description={desc}
{LOBSTER_MARKER}

[Service]
Type=oneshot
User={LOBSTER_USER}
ExecStart={command}
"""


def _unit_name(name: str) -> str:
    return f"{UNIT_PREFIX}{name}"


def _timer_path(name: str) -> Path:
    return SYSTEMD_DIR / f"{_unit_name(name)}.timer"


def _service_path(name: str) -> Path:
    return SYSTEMD_DIR / f"{_unit_name(name)}.service"


# ---------------------------------------------------------------------------
# Systemctl helpers (async, use sudo)
# ---------------------------------------------------------------------------

async def _run_systemctl(*args: str, check: bool = True) -> tuple[int, str, str]:
    """Run a sudo systemctl command. Returns (returncode, stdout, stderr)."""
    cmd = ["sudo", "systemctl"] + list(args)
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=15)
    rc = proc.returncode or 0
    stdout = stdout_bytes.decode(errors="replace").strip()
    stderr = stderr_bytes.decode(errors="replace").strip()
    if check and rc != 0:
        raise RuntimeError(f"systemctl {' '.join(args)} failed (rc={rc}): {stderr or stdout}")
    return rc, stdout, stderr


async def _daemon_reload() -> None:
    await _run_systemctl("daemon-reload")


async def _enable_now(unit_name: str) -> None:
    await _run_systemctl("enable", "--now", unit_name)


async def _stop_and_disable(unit_name: str) -> None:
    """Stop and disable a unit. Ignore errors if the unit doesn't exist."""
    await _run_systemctl("stop", unit_name, check=False)
    await _run_systemctl("disable", unit_name, check=False)


# ---------------------------------------------------------------------------
# Unit file I/O
# ---------------------------------------------------------------------------

def _is_lobster_unit(path: Path) -> bool:
    """Return True if the file exists and contains the LOBSTER-MANAGED marker."""
    try:
        return LOBSTER_MARKER in path.read_text()
    except OSError:
        return False


def _sudo_write(path: Path, content: str) -> None:
    """Write content to a path owned by root, using sudo tee."""
    result = subprocess.run(
        ["sudo", "tee", str(path)],
        input=content.encode(),
        capture_output=True,
    )
    if result.returncode != 0:
        raise PermissionError(
            f"sudo tee {path} failed: {result.stderr.decode().strip()}"
        )


def _sudo_remove(path: Path) -> None:
    """Remove a file owned by root, using sudo rm -f."""
    subprocess.run(["sudo", "rm", "-f", str(path)], check=True, capture_output=True)


def _write_units(name: str, schedule: str, command: str, description: str) -> None:
    """Write timer and service unit files to /etc/systemd/system/ via sudo."""
    _sudo_write(_timer_path(name), _timer_unit(name, schedule, description))
    _sudo_write(_service_path(name), _service_unit(name, command, description))


def _remove_units(name: str) -> None:
    """Remove timer and service unit files (ignore if missing)."""
    for p in [_timer_path(name), _service_path(name)]:
        _sudo_remove(p)


def _read_unit_field(path: Path, field: str) -> Optional[str]:
    """Extract a single field value from a unit file, e.g. 'OnCalendar'."""
    try:
        for line in path.read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith(f"{field}="):
                return stripped[len(f"{field}="):].strip()
    except OSError:
        pass
    return None


# ---------------------------------------------------------------------------
# Core operations
# ---------------------------------------------------------------------------

async def create_job(
    name: str,
    schedule: str,
    command: str,
    description: str = "",
) -> CreateResult:
    """Create a systemd timer+service pair for a job.

    Idempotent: if the unit already exists with the same schedule and command,
    returns status="already_exists" without writing or reloading anything.
    """
    timer = _timer_path(name)
    service = _service_path(name)

    # Idempotency check
    if timer.exists() and service.exists() and _is_lobster_unit(timer):
        existing_schedule = _read_unit_field(timer, "OnCalendar")
        existing_command = _read_unit_field(service, "ExecStart")
        if existing_schedule == schedule and existing_command == command:
            return CreateResult(name=name, status="already_exists")

    _write_units(name, schedule, command, description)
    await _daemon_reload()
    await _enable_now(f"{_unit_name(name)}.timer")
    return CreateResult(name=name, status="created")


async def list_jobs() -> list[JobInfo]:
    """List all lobster-managed timer units with their status."""
    rc, stdout, _ = await _run_systemctl(
        "list-timers", "--all", "--no-pager",
        "--output=json",
        check=False,
    )

    jobs: list[JobInfo] = []

    if rc != 0 or not stdout:
        return jobs

    try:
        import json
        timers = json.loads(stdout)
    except (ValueError, TypeError):
        return jobs

    for entry in timers:
        unit = entry.get("unit", "") or entry.get("timers_activating_target", "")
        if not unit:
            # Try other keys
            for key in ("next", "left", "last", "passed", "unit", "activates"):
                if key == "unit":
                    unit = entry.get("unit", "")
                    break
            unit = entry.get("unit", "")

        if not unit.startswith(UNIT_PREFIX) or not unit.endswith(".timer"):
            continue

        bare_name = unit[len(UNIT_PREFIX):-len(".timer")]

        # Only include units we manage
        if not _is_lobster_unit(_timer_path(bare_name)):
            continue

        schedule = _read_unit_field(_timer_path(bare_name), "OnCalendar") or ""
        command = _read_unit_field(_service_path(bare_name), "ExecStart") or ""

        # Parse timing fields from the JSON entry
        last_trigger = entry.get("last") or entry.get("last_trigger")
        next_trigger = entry.get("next") or entry.get("next_trigger")

        # systemctl list-timers --output=json does not emit an "active" key.
        # Query the real active state per unit via "systemctl is-active".
        rc_active, _, _ = await _run_systemctl(
            "is-active", f"{_unit_name(bare_name)}.timer", check=False
        )
        active = (rc_active == 0)

        def _us_to_iso(us: object) -> Optional[str]:
            """Convert a microsecond-epoch integer to an ISO 8601 UTC string."""
            if not us:
                return None
            try:
                return datetime.fromtimestamp(int(us) / 1_000_000, tz=timezone.utc).isoformat()
            except (ValueError, TypeError, OSError):
                return None

        jobs.append(JobInfo(
            name=bare_name,
            schedule=schedule,
            command=command,
            description=f"Lobster job: {bare_name}",
            active=active,
            last_run=_us_to_iso(last_trigger),
            next_run=_us_to_iso(next_trigger),
        ))

    return jobs


async def update_job(
    name: str,
    schedule: Optional[str] = None,
    command: Optional[str] = None,
    enabled: Optional[bool] = None,
) -> UpdateResult:
    """Update schedule, command, and/or enabled state for an existing lobster job.

    Rewrites the affected unit files, then reloads and restarts the timer.
    Returns the list of fields that were changed.

    If enabled=False, the timer is stopped and disabled (paused).
    If enabled=True, the timer is re-enabled and started.
    """
    timer = _timer_path(name)
    service = _service_path(name)

    if not timer.exists() or not _is_lobster_unit(timer):
        raise FileNotFoundError(f"Job '{name}' not found or not a lobster-managed unit")

    updated: list[str] = []

    current_schedule = _read_unit_field(timer, "OnCalendar") or ""
    current_command = _read_unit_field(service, "ExecStart") or ""
    current_description = ""
    for line in timer.read_text().splitlines():
        if line.strip().startswith("Description="):
            current_description = line.strip()[len("Description="):]
            break

    new_schedule = schedule if schedule is not None else current_schedule
    new_command = command if command is not None else current_command

    if schedule is not None and schedule != current_schedule:
        updated.append("schedule")
    if command is not None and command != current_command:
        updated.append("command")
    if enabled is not None:
        updated.append("enabled")

    if not updated:
        return UpdateResult(name=name, updated_fields=[])

    # Handle enable/disable (no unit file rewrite needed for this)
    if enabled is not None and not (schedule is not None or command is not None):
        # Only toggling enabled — just enable/disable the timer
        if enabled:
            await _enable_now(f"{_unit_name(name)}.timer")
        else:
            await _stop_and_disable(f"{_unit_name(name)}.timer")
        return UpdateResult(name=name, updated_fields=updated)

    # Unit file update
    _write_units(name, new_schedule, new_command, current_description)
    await _daemon_reload()

    if enabled is False:
        await _stop_and_disable(f"{_unit_name(name)}.timer")
    elif enabled is True:
        await _enable_now(f"{_unit_name(name)}.timer")
    else:
        # Restart the timer so the new schedule takes effect
        await _run_systemctl("restart", f"{_unit_name(name)}.timer")

    return UpdateResult(name=name, updated_fields=updated)


async def delete_job(name: str) -> DeleteResult:
    """Stop, disable, and remove unit files for a lobster job.

    Idempotent: returns status="not_found" if the unit doesn't exist.
    """
    timer = _timer_path(name)
    if not timer.exists():
        return DeleteResult(name=name, status="not_found")

    if not _is_lobster_unit(timer):
        raise PermissionError(f"Unit '{_unit_name(name)}' exists but is not lobster-managed — refusing to delete")

    await _stop_and_disable(f"{_unit_name(name)}.timer")
    _remove_units(name)
    await _daemon_reload()
    return DeleteResult(name=name, status="deleted")


# ---------------------------------------------------------------------------
# Scaffold helper
# ---------------------------------------------------------------------------

# Minimal inline poller template returned when no file template exists
_INLINE_POLLER_TEMPLATE = """\
#!/usr/bin/env python3
\"\"\"
Lobster poller job — generated scaffold.

This script is called by a systemd timer unit. It should:
  1. Fetch or check the data source
  2. Write output via the lobster MCP write_task_output tool
  3. Exit 0 on success, non-zero on failure

Usage:
  ExecStart=/path/to/this/script.py
\"\"\"

import subprocess
import json
import sys
from datetime import datetime, timezone

JOB_NAME = "REPLACE_WITH_JOB_NAME"


def fetch_data() -> dict:
    \"\"\"Fetch data from the source. Replace with your logic.\"\"\"
    return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}


def write_output(job_name: str, output: str, status: str = "success") -> None:
    \"\"\"Write job output via lobster MCP (calls the mcp tool via CLI shim).\"\"\"
    # If running as a systemd service, write to stdout for journal capture.
    print(f"[{job_name}] {status}: {output}")


def main() -> int:
    data = fetch_data()
    output = json.dumps(data)
    write_output(JOB_NAME, output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
"""


def get_scaffold(kind: str = "poller") -> str:
    """Return the scaffold template for the given kind.

    Checks for a file at ~/lobster/scheduled-tasks/templates/<kind>.py.template
    first; falls back to the inline template if the file doesn't exist.
    """
    repo_dir = Path.home() / "lobster"
    template_path = repo_dir / "scheduled-tasks" / "templates" / f"{kind}.py.template"
    if template_path.exists():
        return template_path.read_text()
    return _INLINE_POLLER_TEMPLATE
