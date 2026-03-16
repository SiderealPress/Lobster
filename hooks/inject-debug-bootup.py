#!/usr/bin/env python3
"""
SessionStart hook: inject sys.debug.bootup.md when LOBSTER_DEBUG=true.

Fires on every SessionStart event. If the LOBSTER_DEBUG environment variable
is set to 'true' (case-insensitive), reads
~/lobster/.claude/sys.debug.bootup.md and prints its contents to stdout.

Claude Code SessionStart hooks can inject content into the agent's initial
context by printing to stdout. This causes the content to appear as a system
message at the start of the session.

If LOBSTER_DEBUG is not set or is not 'true', the hook exits silently with
exit code 0.

Also checks ~/lobster-config/config.env as a fallback for the LOBSTER_DEBUG
setting, so developers can set it in config.env instead of the shell
environment.
"""

import os
import sys
from pathlib import Path


CONFIG_ENV = Path(os.path.expanduser("~/lobster-config/config.env"))
DEBUG_BOOTUP_FILE = Path(os.path.expanduser("~/lobster/.claude/sys.debug.bootup.md"))


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


def main() -> None:
    if not _is_debug_mode():
        sys.exit(0)

    if not DEBUG_BOOTUP_FILE.exists():
        # File missing — exit silently rather than crashing the session.
        print(
            f"[inject-debug-bootup] WARNING: LOBSTER_DEBUG=true but "
            f"{DEBUG_BOOTUP_FILE} not found; skipping injection.",
            file=sys.stderr,
        )
        sys.exit(0)

    try:
        content = DEBUG_BOOTUP_FILE.read_text()
    except OSError as exc:
        print(
            f"[inject-debug-bootup] WARNING: could not read {DEBUG_BOOTUP_FILE}: {exc}",
            file=sys.stderr,
        )
        sys.exit(0)

    # Print to stdout — Claude Code injects stdout from SessionStart hooks
    # into the agent's initial context as a system message.
    print(content)
    sys.exit(0)


if __name__ == "__main__":
    main()
