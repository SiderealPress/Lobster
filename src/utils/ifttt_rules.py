"""
src/utils/ifttt_rules.py — IFTTT-style behavioral rules store for Lobster.

Provides a bounded, LRU-pruned flat list of "if X then Y" behavioral rules that
the dispatcher loads at startup. Rules are stored as structured YAML — machine-
readable, diff-able, and version-controllable in lobster-user-config.

Design principles:
  - Pure functions for all queries and transformations; side effects isolated to
    load_rules() and save_rules()
  - Immutability: all mutation functions return new rule lists rather than
    modifying in place
  - LRU pruning: when the cap is exceeded, the least-recently-used rules (by
    last_accessed_at, breaking ties by access_count) are pruned silently
  - Cap: MAX_RULES (default 100) — prevents unbounded growth and keeps the file
    scannable at startup
  - Graceful degradation: missing or malformed rules file returns an empty list
    (Lobster continues without rules; no crash)

File location: ~/lobster-user-config/memory/canonical/ifttt-rules.yaml
"""

import logging
import os
import tempfile
from datetime import datetime, timezone
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

# A Rule is a plain dict with at least these keys:
#   id: str                  — stable unique identifier (slug, e.g. "check-calendar-on-meeting")
#   trigger: str             — natural-language "if" condition
#   action: str              — natural-language "then" action
#   created_at: str          — ISO 8601 UTC timestamp
#   last_accessed_at: str    — ISO 8601 UTC timestamp (updated on every lookup hit)
#   access_count: int        — total number of times this rule was accessed
#   source: str              — how the rule was created ("lobster" | "user" | "system")
#   enabled: bool            — whether the rule is active (False = soft-disabled, not pruned)
#   notes: str | None        — optional human-readable annotation

Rule = dict[str, Any]
RuleStore = list[Rule]

# ---------------------------------------------------------------------------
# Pure transformation functions
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    """Return current UTC time as ISO 8601 string."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _lru_key(rule: Rule) -> tuple[str, int]:
    """Sort key for LRU pruning: (last_accessed_at ASC, access_count ASC).

    Rules with the oldest last_accessed_at — or the lowest access_count on
    ties — are candidates for pruning first.
    """
    return (rule.get("last_accessed_at", ""), rule.get("access_count", 0))


def prune_lru(rules: RuleStore, cap: int = MAX_RULES) -> RuleStore:
    """Return a new rule list pruned to at most `cap` entries via LRU policy.

    Rules are sorted by (last_accessed_at ASC, access_count ASC). The least-
    recently-used entries beyond the cap are dropped. The returned list
    preserves the original order of the surviving rules.

    Args:
        rules: Current rule list (not mutated).
        cap: Maximum number of rules to retain.

    Returns:
        A new list containing at most `cap` rules. If len(rules) <= cap,
        returns a shallow copy unchanged.
    """
    if len(rules) <= cap:
        return list(rules)

    # Identify the set of IDs to keep (the `cap` most-recently-used)
    sorted_by_lru = sorted(rules, key=_lru_key)
    keep_ids = {r["id"] for r in sorted_by_lru[len(rules) - cap :]}

    # Preserve original order for the kept rules
    pruned = [r for r in rules if r["id"] in keep_ids]

    n_dropped = len(rules) - len(pruned)
    if n_dropped:
        log.info(
            "ifttt_rules: pruned %d rule(s) (LRU, cap=%d)", n_dropped, cap
        )

    return pruned


def add_rule(
    rules: RuleStore,
    *,
    rule_id: str,
    trigger: str,
    action: str,
    source: str = "lobster",
    notes: str | None = None,
    cap: int = MAX_RULES,
) -> RuleStore:
    """Return a new rule list with `rule` appended and LRU pruning applied.

    If a rule with the same `rule_id` already exists, it is replaced in place
    (update semantics). Otherwise, the new rule is appended and the list is
    pruned if over cap.

    Args:
        rules: Current rule list (not mutated).
        rule_id: Stable unique slug for the rule.
        trigger: Natural-language "if" condition.
        action: Natural-language "then" action.
        source: Origin label ("lobster" | "user" | "system").
        notes: Optional annotation.
        cap: Maximum rule count after pruning.

    Returns:
        New rule list with the rule added (and list pruned to cap if needed).
    """
    now = _now_iso()
    new_rule: Rule = {
        "id": rule_id,
        "trigger": trigger,
        "action": action,
        "created_at": now,
        "last_accessed_at": now,
        "access_count": 0,
        "source": source,
        "enabled": True,
        "notes": notes,
    }

    # Replace existing rule with same ID, or append
    existing_ids = {r["id"] for r in rules}
    if rule_id in existing_ids:
        updated = [new_rule if r["id"] == rule_id else r for r in rules]
    else:
        updated = list(rules) + [new_rule]

    return prune_lru(updated, cap=cap)


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


def touch_rule(rules: RuleStore, rule_id: str) -> RuleStore:
    """Return a new rule list with access metadata updated for `rule_id`.

    Increments access_count and sets last_accessed_at to now. Used when a
    rule is matched and applied during a session. No-op if the rule does not
    exist.

    Args:
        rules: Current rule list (not mutated).
        rule_id: ID of the accessed rule.

    Returns:
        New rule list with updated access metadata for the specified rule.
    """
    now = _now_iso()

    def _touch(rule: Rule) -> Rule:
        if rule["id"] != rule_id:
            return rule
        return {
            **rule,
            "last_accessed_at": now,
            "access_count": rule.get("access_count", 0) + 1,
        }

    return [_touch(r) for r in rules]


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
    """Render enabled rules as a compact plain-text block for injection into context.

    Produces one line per rule in the format:
        [id] IF <trigger> THEN <action>

    Disabled rules are omitted. If no enabled rules exist, returns an empty string.

    Args:
        rules: Full rule list.

    Returns:
        Multi-line string of enabled rules, or "" if none.
    """
    enabled = get_enabled_rules(rules)
    if not enabled:
        return ""
    lines = [
        f"[{r['id']}] IF {r['trigger']} THEN {r['action']}" for r in enabled
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
        log.warning(
            "ifttt_rules: 'rules' key is not a list in %s", rules_path
        )
        return []

    # Validate minimum required keys; skip malformed entries with a warning
    valid_rules: RuleStore = []
    for i, entry in enumerate(raw_rules):
        if not isinstance(entry, dict):
            log.warning(
                "ifttt_rules: rule[%d] is not a dict, skipping", i
            )
            continue
        if not all(k in entry for k in ("id", "trigger", "action")):
            log.warning(
                "ifttt_rules: rule[%d] missing required keys (id/trigger/action), skipping",
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

    Applies LRU pruning before writing so the on-disk file never exceeds `cap`
    entries. Uses write-to-temp-then-rename to ensure readers never see a
    partial file.

    Args:
        rules: Rule list to persist (not mutated).
        path: Target file path. Defaults to the canonical location.
        cap: Maximum number of rules to retain (LRU pruning applied).

    Raises:
        OSError: If the write or rename fails.
    """
    rules_path = path or _DEFAULT_RULES_PATH
    rules_path.parent.mkdir(parents=True, exist_ok=True)

    pruned = prune_lru(rules, cap=cap)

    data = {
        "version": 1,
        "rules": pruned,
    }

    serialized = yaml.dump(
        data,
        default_flow_style=False,
        allow_unicode=True,
        sort_keys=False,
    )

    # Atomic write: temp file in same directory, then rename
    fd, tmp_path = tempfile.mkstemp(
        dir=str(rules_path.parent), suffix=".tmp"
    )
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

    log.debug(
        "ifttt_rules: saved %d rule(s) to %s", len(pruned), rules_path
    )
