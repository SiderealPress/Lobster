#!/usr/bin/env python3
"""PreToolUse hook: warn when outgoing text or GitHub writes contain secrets.

Loads ~/lobster-config/config.env (or ~/lobster/config/config.env as fallback),
extracts all values >= 20 characters, then scans the tool input for those values.

If a match is found: prints a WARNING to stderr naming the key whose value was
found (never the value itself), then exits 0 (warn mode — does not block).

If no match: exits 0 silently.

**Mode:** WARN only. This hook never blocks (exit 0 always).
A future hook version will add block mode (exit 2) once we are confident in
the pattern coverage. See GitHub issue #582.

**Covered tools:**
  - mcp__lobster-inbox__send_reply   (body / text)
  - mcp__github__*                   (body, title, comment body, etc.)
  - Bash                             (command string, when it contains
                                      "gh issue" or "gh pr")

**Config file search order:**
  1. $LOBSTER_CONFIG_DIR/config.env  (env var override)
  2. ~/lobster-config/config.env     (standard install location)
  3. ~/lobster/config/config.env     (repo fallback for dev installs)
"""
import json
import os
import re
import sys
from pathlib import Path

# Minimum character length for a value to be treated as a candidate secret.
# Short values like "true", "1", chat IDs, etc. are excluded.
_MIN_SECRET_LEN = 20

# Pattern that extracts KEY=VALUE pairs from a shell env file.
# - Skips comment lines (leading #)
# - Handles optional quotes around values
# - Captures the raw value (without surrounding quotes)
_ENV_LINE = re.compile(
    r"^(?P<key>[A-Za-z_][A-Za-z0-9_]*)="
    r"(?P<value>\"[^\"]*\"|'[^']*'|[^\s#]*)"
)

# Tools that pass a Bash command string and need substring matching on the
# command text when the command looks like a gh CLI write operation.
_BASH_GH_WRITE_RE = re.compile(r"\bgh\b.*\b(issue|pr)\b")


def _find_config_file() -> Path | None:
    """Return the first config.env file that exists."""
    candidates = []

    # Honour explicit override first
    config_dir_env = os.environ.get("LOBSTER_CONFIG_DIR", "")
    if config_dir_env:
        candidates.append(Path(config_dir_env) / "config.env")

    home = Path.home()
    candidates += [
        home / "lobster-config" / "config.env",
        home / "lobster" / "config" / "config.env",
    ]

    return next((p for p in candidates if p.is_file()), None)


def _load_secrets(config_path: Path) -> dict[str, str]:
    """Parse KEY=VALUE pairs from a shell env file.

    Returns a dict of {key: value} for values whose length is >= _MIN_SECRET_LEN.
    Surrounding quotes are stripped from the stored value.
    """
    secrets: dict[str, str] = {}
    try:
        text = config_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return secrets

    for line in text.splitlines():
        line = line.strip()
        m = _ENV_LINE.match(line)
        if not m:
            continue
        key = m.group("key")
        raw_value = m.group("value")
        # Strip surrounding quotes
        if (raw_value.startswith('"') and raw_value.endswith('"')) or (
            raw_value.startswith("'") and raw_value.endswith("'")
        ):
            raw_value = raw_value[1:-1]
        if len(raw_value) >= _MIN_SECRET_LEN:
            secrets[key] = raw_value

    return secrets


def _extract_strings_to_scan(tool_name: str, tool_input: dict) -> list[str]:
    """Return the list of strings from tool_input that should be scanned.

    Returns [] if the tool is not in scope or the input has no interesting fields.
    """
    if tool_name == "mcp__lobster-inbox__send_reply":
        # The user-facing message body
        return [str(tool_input.get("text", ""))]

    if tool_name.startswith("mcp__github__"):
        # Collect common write fields that may carry user-supplied content
        fields = ["body", "title", "comment", "message", "content", "description"]
        return [str(tool_input.get(f, "")) for f in fields if tool_input.get(f)]

    if tool_name == "Bash":
        command = str(tool_input.get("command", ""))
        # Only scan Bash calls that look like gh CLI write operations
        if _BASH_GH_WRITE_RE.search(command):
            return [command]
        return []

    return []


def _scan_for_secrets(
    texts: list[str], secrets: dict[str, str]
) -> list[str]:
    """Return a list of key names whose values appear in any of the texts.

    Only key names are returned — never values — so callers can log which
    secrets were detected without revealing the secret values themselves.
    """
    combined = "\n".join(texts)
    # Separate key/value iteration so static analysis can verify that only
    # keys (not values) are collected into the return list.
    matched_keys: list[str] = []
    for key, value in secrets.items():
        if value in combined:
            matched_keys.append(key)  # key only — value is never stored here
    return matched_keys


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    tool_name = data.get("tool_name", "")
    tool_input = data.get("tool_input", {})

    texts = _extract_strings_to_scan(tool_name, tool_input)
    if not texts:
        sys.exit(0)

    config_path = _find_config_file()
    if config_path is None:
        # No config file found — nothing to scan against, pass silently.
        sys.exit(0)

    secrets = _load_secrets(config_path)
    if not secrets:
        sys.exit(0)

    matched_keys = _scan_for_secrets(texts, secrets)
    if matched_keys:
        keys_str = ", ".join(matched_keys)
        print(
            f"WARNING [secret-scanner]: outgoing tool call ({tool_name}) "
            f"contains secret value(s): {keys_str}. "
            f"Review before sending.",
            file=sys.stderr,
        )
        # WARN mode: do not block. Exit 0 to allow the tool call to proceed.
        # To switch to block mode, change this to sys.exit(2) and update the
        # error message to instruct Claude to redact the secret.

    sys.exit(0)


if __name__ == "__main__":
    main()
