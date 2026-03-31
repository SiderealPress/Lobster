"""
src/utils/ifttt_rules.py — IFTTT-style behavioral rules store for Lobster.

Provides a bounded flat list of "if X then Y" behavioral rules that the
dispatcher loads at startup. Rules are stored as minimal YAML — the file is
an index only; behavioral content and access metadata are held in the memory
DB, looked up via `action_ref` (a memory DB entry ID).

Design principles:
  - Pure functions for all queries and transformations; side effects isolated to
    load_rules() and save_rules()
  - Immutability: all mutation functions return new rule lists rather than
    modifying in place
  - Cap: MAX_RULES (default 100) — prevents unbounded growth and keeps the file
    scannable at startup. LRU enforcement is handled by the memory DB (DB access
    increments counters there; the DB prunes old entries when needed).
  - Graceful degradation: missing or malformed rules file returns an empty list
    (Lobster continues without rules; no crash)

File location: ~/lobster-user-config/memory/canonical/ifttt-rules.yaml

Schema (version 1):
  id:         Stable unique slug (e.g. "check-calendar-on-meeting")
  condition:  Natural-language "IF" condition (one sentence in English)
  action_ref: Memory DB entry ID to look up — the DB holds the behavioral
              content and tracks access metadata (access_count, last_accessed_at)
  enabled:    true = active, false = soft-disabled (kept in file, never applied)
"""

import logging
import os
import tempfile
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_RULES: int = 100

_DEFAULT_RULES_PATH = (
    Path.home()
    / "lobster-user-config"
    / "memory"
    / "canonical"
    / "ifttt-rules.yaml"
)

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

# A Rule is a plain dict with exactly these keys:
#   id: str           — stable unique slug
#   condition: str    — natural-language "if" condition
#   action_ref: str   — memory DB entry ID (DB holds behavioral content + metadata)
#   enabled: bool     — whether the rule is active

Rule = dict[str, Any]
RuleStore = list[Rule]

# ---------------------------------------------------------------------------
# Pure transformation functions
# ---------------------------------------------------------------------------


def add_rule(
    rules: RuleStore,
    *,
    rule_id: str,
    condition: str,
    action_ref: str,
    cap: int = MAX_RULES,
) -> RuleStore:
    """Return a new rule list with the given rule appended (or replaced).

    If a rule with the same `rule_id` already exists, it is replaced in place
    (update semantics). Otherwise, the new rule is appended. If the list
    exceeds `cap` after appending, the last rule(s) beyond the cap are
    dropped (FIFO overflow — the memory DB owns LRU logic).

    Args:
        rules: Current rule list (not mutated).
        rule_id: Stable unique slug for the rule.
        condition: Natural-language "IF" condition (one sentence).
        action_ref: Memory DB entry ID for the behavioral content.
        cap: Maximum rule count; excess rules at the tail are dropped.

    Returns:
        New rule list with the rule added (and capped to `cap` entries).
    """
    new_rule: Rule = {
        "id": rule_id,
        "condition": condition,
        "action_ref": action_ref,
        "enabled": True,
    }

    existing_ids = {r["id"] for r in rules}
    if rule_id in existing_ids:
        updated = [new_rule if r["id"] == rule_id else r for r in rules]
    else:
        updated = list(rules) + [new_rule]

    # Cap: drop oldest entries (from head) to keep the newest rules
    if len(updated) > cap:
        n_dropped = len(updated) - cap
        log.info("ifttt_rules: dropped %d oldest rule(s) to enforce cap=%d", n_dropped, cap)
        updated = updated[n_dropped:]

    return updated


def remove_rule(rules: RuleStore, rule_id: str) -> RuleStore:
    """Return a new rule list with the rule identified by `rule_id` removed.

    No-op if the rule does not exist.

    Args:
        rules: Current rule list (not mutated).
        rule_id: ID of the rule to remove.

    Returns:
        New rule list without the specified rule.
    """
    return [r for r in rules if r["id"] != rule_id]


def get_enabled_rules(rules: RuleStore) -> RuleStore:
    """Return only the enabled rules (enabled=True or key absent).

    Args:
        rules: Full rule list.

    Returns:
        Filtered list containing only enabled rules.
    """
    return [r for r in rules if r.get("enabled", True)]


def find_rule(rules: RuleStore, rule_id: str) -> Rule | None:
    """Return the rule with the given ID, or None if not found.

    Args:
        rules: Rule list to search.
        rule_id: ID to look up.

    Returns:
        Matching rule dict, or None.
    """
    for rule in rules:
        if rule.get("id") == rule_id:
            return rule
    return None


def format_rules_for_context(rules: RuleStore) -> str:
    """Render enabled rules as a compact plain-text block for context injection.

    Produces one line per rule in the format:
        [id] IF <condition> THEN lookup <action_ref> in memory DB

    Disabled rules are omitted. If no enabled rules exist, returns an empty string.

    The agent should batch all rule lookups that match a given turn — resolve all
    matching `action_ref` values in a single DB query rather than one at a time.

    Args:
        rules: Full rule list.

    Returns:
        Multi-line string of enabled rules, or "" if none.
    """
    enabled = get_enabled_rules(rules)
    if not enabled:
        return ""
    lines = [
        f"[{r['id']}] IF {r['condition']} THEN lookup {r['action_ref']} in memory DB"
        for r in enabled
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# I/O functions (side effects isolated here)
# ---------------------------------------------------------------------------


def load_rules(path: Path | None = None) -> RuleStore:
    """Load rules from YAML file.

    Gracefully returns an empty list if the file does not exist or is malformed.

    Args:
        path: Path to the rules YAML file. Defaults to the canonical location
              in lobster-user-config.

    Returns:
        List of rule dicts. Empty list on any error.
    """
    rules_path = path or _DEFAULT_RULES_PATH
    if not rules_path.exists():
        log.debug("ifttt_rules: rules file not found at %s", rules_path)
        return []

    try:
        raw = rules_path.read_text(encoding="utf-8")
        data = yaml.safe_load(raw)
    except Exception as exc:
        log.warning("ifttt_rules: failed to read %s: %s", rules_path, exc)
        return []

    if not isinstance(data, dict):
        log.warning(
            "ifttt_rules: unexpected top-level type %s in %s",
            type(data).__name__,
            rules_path,
        )
        return []

    raw_rules = data.get("rules", [])
    if not isinstance(raw_rules, list):
        log.warning("ifttt_rules: 'rules' key is not a list in %s", rules_path)
        return []

    # Validate minimum required keys; skip malformed entries with a warning
    valid_rules: RuleStore = []
    for i, entry in enumerate(raw_rules):
        if not isinstance(entry, dict):
            log.warning("ifttt_rules: rule[%d] is not a dict, skipping", i)
            continue
        if not all(k in entry for k in ("id", "condition", "action_ref")):
            log.warning(
                "ifttt_rules: rule[%d] missing required keys (id/condition/action_ref), skipping",
                i,
            )
            continue
        valid_rules.append(entry)

    log.debug("ifttt_rules: loaded %d rule(s) from %s", len(valid_rules), rules_path)
    return valid_rules


def save_rules(
    rules: RuleStore,
    path: Path | None = None,
    cap: int = MAX_RULES,
) -> None:
    """Persist rule list to YAML file atomically.

    Caps the rule list before writing (FIFO — drops head/oldest entries beyond cap).
    Uses write-to-temp-then-rename to ensure readers never see a partial file.

    Args:
        rules: Rule list to persist (not mutated).
        path: Target file path. Defaults to the canonical location.
        cap: Maximum number of rules to retain.

    Raises:
        OSError: If the write or rename fails.
    """
    rules_path = path or _DEFAULT_RULES_PATH
    rules_path.parent.mkdir(parents=True, exist_ok=True)

    # Drop oldest (head) entries to keep the newest rules at cap
    capped = rules[-cap:] if len(rules) > cap else list(rules)

    data = {
        "version": 1,
        "rules": capped,
    }

    serialized = yaml.dump(
        data,
        default_flow_style=False,
        allow_unicode=True,
        sort_keys=False,
    )

    # Atomic write: temp file in same directory, then rename
    fd, tmp_path = tempfile.mkstemp(dir=str(rules_path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(serialized)
            f.flush()
            os.fsync(f.fileno())
        os.rename(tmp_path, str(rules_path))
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    log.debug("ifttt_rules: saved %d rule(s) to %s", len(capped), rules_path)
