"""
Guardrail test: every "source" value written to the inbox by any script in
scripts/*.sh must be in INBOX_MESSAGE_SOURCES (src/mcp/message_types.py).

Motivation: PR #1736 added a quarantine guard for unrecognized message sources.
That PR updated daily-health-check.sh but missed nightly-consolidation.sh,
causing three nightly consolidations to be silently quarantined (issue #1797).
This test catches that class of regression at review time.

What we check:
- Parse every  "source": "<value>"  literal in scripts/*.sh files
- Assert each value is in INBOX_MESSAGE_SOURCES
- Python scripts under scripts/ are explicitly excluded — they receive source
  values via CLI arguments (not hardcoded) and are tested separately.

Intentional exclusion:
- lobster-observe.py writes "source": "cron-direct" only to observations.log
  (not to the inbox JSON bus), so that value is not checked here.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Helpers (pure)
# ---------------------------------------------------------------------------

REPO_DIR = Path(__file__).parent.parent.parent
SCRIPTS_DIR = REPO_DIR / "scripts"

# Regex: matches  "source": "some-value"  inside shell heredoc JSON payloads.
# We deliberately match only double-quoted string literals so we skip variable
# expansions like  "source": "$SOURCE_VAR"  (those are dynamic, not hardcoded).
_SOURCE_LITERAL_RE = re.compile(r'"source"\s*:\s*"([^"]+)"')

# Shell scripts only — Python scripts under scripts/ use dynamic CLI args.
_SHELL_SCRIPTS = list(SCRIPTS_DIR.glob("*.sh"))

# Known constant name for clarity in assertions.
VALID_INBOX_SOURCES_CONSTANT = "INBOX_MESSAGE_SOURCES"


def _load_inbox_message_sources() -> frozenset[str]:
    """Import INBOX_MESSAGE_SOURCES from message_types.py (dependency-free module)."""
    mcp_dir = str(REPO_DIR / "src" / "mcp")
    if mcp_dir not in sys.path:
        sys.path.insert(0, mcp_dir)
    from message_types import INBOX_MESSAGE_SOURCES  # type: ignore[import]
    return INBOX_MESSAGE_SOURCES


def _extract_hardcoded_sources_from_shell(script: Path) -> list[tuple[str, int]]:
    """Return list of (source_value, line_number) for all hardcoded source literals.

    Only matches double-quoted string literals — variable expansions are skipped.
    """
    results = []
    text = script.read_text(errors="replace")
    for lineno, line in enumerate(text.splitlines(), start=1):
        for match in _SOURCE_LITERAL_RE.finditer(line):
            results.append((match.group(1), lineno))
    return results


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_all_shell_script_inbox_sources_are_known() -> None:
    """Every hardcoded source value in scripts/*.sh must be in INBOX_MESSAGE_SOURCES.

    Catches the class of regression introduced in issue #1797: a script writes
    source="internal" which is not in INBOX_MESSAGE_SOURCES, causing the inbox
    server's quarantine guard to silently move the message to failed/.
    """
    valid_sources = _load_inbox_message_sources()

    violations: list[str] = []
    for script in sorted(_SHELL_SCRIPTS):
        for source_value, lineno in _extract_hardcoded_sources_from_shell(script):
            if source_value not in valid_sources:
                violations.append(
                    f"{script.name}:{lineno}  \"source\": \"{source_value}\""
                    f"  (valid: {sorted(valid_sources)})"
                )

    assert not violations, (
        f"\n\nFound {len(violations)} script(s) writing unrecognized inbox source value(s).\n"
        "These messages will be quarantined by the inbox server's source guard.\n"
        f"Fix by changing the source to one of {sorted(valid_sources)}.\n\n"
        + "\n".join(violations)
    )


def test_nightly_consolidation_writes_system_source() -> None:
    """nightly-consolidation.sh must write source='system', not 'internal'.

    Regression test for issue #1797: source='internal' caused every nightly
    consolidation to be quarantined to failed/ since PR #1736.
    """
    script = SCRIPTS_DIR / "nightly-consolidation.sh"
    assert script.exists(), f"Script not found: {script}"

    sources = _extract_hardcoded_sources_from_shell(script)
    assert sources, "nightly-consolidation.sh wrote no hardcoded 'source' field — expected exactly one"

    source_values = [v for v, _ in sources]
    assert "internal" not in source_values, (
        "nightly-consolidation.sh still writes source='internal' — "
        "this will be quarantined by the inbox server. Use source='system'."
    )
    assert "system" in source_values, (
        f"nightly-consolidation.sh does not write source='system'. Got: {source_values}"
    )


def test_daily_update_check_writes_system_source() -> None:
    """daily-update-check.sh must write source='system', not 'internal'.

    Companion regression test: daily-update-check.sh had the same source='internal'
    bug as nightly-consolidation.sh (issue #1797 audit).
    """
    script = SCRIPTS_DIR / "daily-update-check.sh"
    assert script.exists(), f"Script not found: {script}"

    sources = _extract_hardcoded_sources_from_shell(script)
    assert sources, "daily-update-check.sh wrote no hardcoded 'source' field — expected at least one"

    source_values = [v for v, _ in sources]
    assert "internal" not in source_values, (
        "daily-update-check.sh still writes source='internal' — "
        "this will be quarantined by the inbox server. Use source='system'."
    )
    for val in source_values:
        assert val == "system", (
            f"daily-update-check.sh writes unexpected source='{val}'. Expected 'system'."
        )
