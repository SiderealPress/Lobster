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
# Validation (pure functions — no I/O)
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
    """Return an error message string, or None if command is valid."""
    if not command:
        return "command cannot be empty"
    if not command.startswith("/"):
        return "command must be an absolute path (must start with /)"
    return None


def validate_schedule(schedule: str) -> Optional[str]:
    """Return an error message string, or None if the schedule looks plausible.

    We accept any non-empty string and let systemd validate it at daemon-reload
    time. This keeps the validation loose while still catching blanks.
    """
    if not schedule:
        return "schedule cannot be empty"
    return None


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

        jobs.append(JobInfo(
            name=bare_name,
            schedule=schedule,
            command=command,
            description=f"Lobster job: {bare_name}",
            active=active,
            last_run=str(last_trigger) if last_trigger else None,
            next_run=str(next_trigger) if next_trigger else None,
        ))

    return jobs


async def update_job(
    name: str,
    schedule: Optional[str] = None,
    command: Optional[str] = None,
) -> UpdateResult:
    """Update schedule and/or command for an existing lobster job.

    Rewrites the affected unit files, then reloads and restarts the timer.
    Returns the list of fields that were changed.
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

    if not updated:
        return UpdateResult(name=name, updated_fields=[])

    _write_units(name, new_schedule, new_command, current_description)
    await _daemon_reload()
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
