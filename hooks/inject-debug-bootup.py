#!/usr/bin/env python3
"""
SessionStart hook: inject sys.debug.bootup.md when LOBSTER_DEBUG=true.

Fires on every SessionStart event. If the LOBSTER_DEBUG environment variable
is set to 'true' (case-insensitive), reads
~/lobster/.claude/sys.debug.bootup.md and prints its contents to stdout.

When the session is the dispatcher, also injects
~/lobster/.claude/sys.debug.dispatcher.bootup.md — this adds dispatcher-specific
startup invariant checks (branch check, alerting policy, etc.).

Claude Code SessionStart hooks can inject content into the agent's initial
context by printing to stdout. This causes the content to appear as a system
message at the start of the session.

If LOBSTER_DEBUG is not set or is not 'true', the hook exits silently with
exit code 0.

Also checks ~/lobster-config/config.env as a fallback for the LOBSTER_DEBUG
setting, so developers can set it in config.env instead of the shell
environment.
"""

import json
import os
import sys
from pathlib import Path

# Allow imports from the hooks directory (session_role).
sys.path.insert(0, str(Path(__file__).parent))

import session_role  # noqa: E402 — path insert must precede this

CONFIG_ENV = Path(os.path.expanduser("~/lobster-config/config.env"))
DEBUG_BOOTUP_FILE = Path(os.path.expanduser("~/lobster/.claude/sys.debug.bootup.md"))
DEBUG_DISPATCHER_BOOTUP_FILE = Path(
    os.path.expanduser("~/lobster/.claude/sys.debug.dispatcher.bootup.md")
)


def _parse_config_env() -> dict:
    """Parse key=value pairs from config.env, ignoring comments and blank lines."""
    config: dict = {}
    if not CONFIG_ENV.exists():
        return config
    try:
        for line in CONFIG_ENV.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            value = value.strip().strip('"').strip("'")
            config[key.strip()] = value
    except OSError:
        pass
    return config


def _is_debug_mode() -> bool:
    """Return True if LOBSTER_DEBUG is 'true' in the environment or config.env.

    The environment variable takes priority over config.env: if LOBSTER_DEBUG
    is present in the environment (regardless of value), it overrides whatever
    is set in config.env.
    """
    raw_env = os.environ.get("LOBSTER_DEBUG")
    if raw_env is not None:
        # Env var is explicitly set — use it exclusively (don't fall back to config.env).
        return raw_env.strip().lower() == "true"
    # Env var absent — check config.env as fallback.
    config = _parse_config_env()
    config_val = config.get("LOBSTER_DEBUG", "").strip().lower()
    return config_val == "true"


def _read_file_safe(path: Path, label: str) -> str | None:
    """Return file contents or None on any error, logging to stderr."""
    if not path.exists():
        print(
            f"[inject-debug-bootup] WARNING: LOBSTER_DEBUG=true but "
            f"{path} not found; skipping {label} injection.",
            file=sys.stderr,
        )
        return None
    try:
        return path.read_text()
    except OSError as exc:
        print(
            f"[inject-debug-bootup] WARNING: could not read {path}: {exc}",
            file=sys.stderr,
        )
        return None


def main() -> None:
    if not _is_debug_mode():
        sys.exit(0)

    # Read hook input from stdin to detect dispatcher vs subagent role.
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        hook_input = {}

    # Always inject the base debug supplement.
    content = _read_file_safe(DEBUG_BOOTUP_FILE, "sys.debug.bootup.md")
    if content is None:
        sys.exit(0)

    # Print to stdout — Claude Code injects stdout from SessionStart hooks
    # into the agent's initial context as a system message.
    print(content)

    # Additionally inject the dispatcher-specific supplement when the session
    # is the Lobster dispatcher. This adds branch-invariant checks and
    # debug-mode startup sequencing. Silently skipped for subagent sessions.
    if session_role.is_dispatcher(hook_input):
        dispatcher_content = _read_file_safe(
            DEBUG_DISPATCHER_BOOTUP_FILE, "sys.debug.dispatcher.bootup.md"
        )
        if dispatcher_content is not None:
            print(dispatcher_content)

    sys.exit(0)


if __name__ == "__main__":
    main()
