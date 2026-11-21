#!/usr/bin/env bash
# =============================================================================
# check_repeat_offender.sh — Spam Report Lookup
# =============================================================================
# Usage: bash check_repeat_offender.sh <phone_number> [--json]
#
# Reads ~/messages/config/spam_reports.yaml and reports whether a given
# phone number has been reported before. Read-only — never writes the log.
# =============================================================================

set -euo pipefail

if [[ $# -lt 1 ]]; then
    echo "Usage: $0 <phone_number> [--json]" >&2
    exit 1
fi

PHONE_NUMBER="$1"
JSON_OUTPUT=false
if [[ "${2:-}" == "--json" ]]; then
    JSON_OUTPUT=true
fi

MESSAGES_DIR="${LOBSTER_MESSAGES:-$HOME/messages}"
LOG_PATH="$MESSAGES_DIR/config/spam_reports.yaml"

python3 - "$PHONE_NUMBER" "$JSON_OUTPUT" "$LOG_PATH" <<'PYEOF'
import sys
import json
from pathlib import Path
from typing import Any

import yaml

phone_arg, json_output_arg, log_path_arg = sys.argv[1], sys.argv[2], sys.argv[3]
json_output = json_output_arg == "True"
log_path = Path(log_path_arg)


def normalize_phone(raw: str) -> str:
    """Pure: normalize a phone number to a +1XXXXXXXXXX-style string."""
    digits = "".join(c for c in raw if c.isdigit())
    if len(digits) == 10:
        digits = "1" + digits
    return f"+{digits}" if digits else raw


def load_reports(path: Path) -> list[dict[str, Any]]:
    """Read-only I/O boundary. Never raises; returns [] on missing/malformed file."""
    if not path.exists():
        return []
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError:
        return []
    return data.get("reports", []) or []


def find_prior_reports(reports: list[dict[str, Any]], phone_number: str) -> list[dict[str, Any]]:
    """Pure filter/sort — no I/O."""
    target = normalize_phone(phone_number)
    matches = [r for r in reports if normalize_phone(r.get("phone_number", "")) == target]
    return sorted(matches, key=lambda r: r.get("date", ""), reverse=True)


reports = load_reports(log_path)
prior = find_prior_reports(reports, phone_arg)

if json_output:
    print(json.dumps({
        "phone_number": normalize_phone(phone_arg),
        "times_reported": len(prior),
        "reports": prior,
    }, indent=2))
else:
    normalized = normalize_phone(phone_arg)
    if not prior:
        print(f"{normalized}: no prior reports found.")
    else:
        print(f"{normalized}: reported {len(prior)} time(s) before.")
        for r in prior:
            ftc_status = r.get("channels", {}).get("ftc", {}).get("status", "unknown")
            fcc_status = r.get("channels", {}).get("fcc", {}).get("status", "unknown")
            carrier_status = r.get("channels", {}).get("carrier_7726", {}).get("status", "unknown")
            print(f"  {r.get('date', '?')} — ftc={ftc_status} fcc={fcc_status} carrier_7726={carrier_status}")
PYEOF
