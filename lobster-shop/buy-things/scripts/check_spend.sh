#!/usr/bin/env bash
# =============================================================================
# check_spend.sh — Buy-Things Spend Report
# =============================================================================
# Usage: bash check_spend.sh [--month YYYY-MM] [--json]
#
# Reads ~/messages/config/spend_log.yaml and payment.yaml and prints a
# human-readable spending report. With --json, outputs machine-readable JSON.
# =============================================================================

set -euo pipefail

MESSAGES_DIR="${LOBSTER_MESSAGES:-$HOME/messages}"
PAYMENT_YAML="$MESSAGES_DIR/config/payment.yaml"
SPEND_LOG="$MESSAGES_DIR/config/spend_log.yaml"

# Parse arguments
TARGET_MONTH=""
JSON_OUTPUT=false
while [[ $# -gt 0 ]]; do
    case "$1" in
        --month) TARGET_MONTH="$2"; shift 2 ;;
        --json) JSON_OUTPUT=true; shift ;;
        *) echo "Unknown argument: $1"; exit 1 ;;
    esac
done

python3 - "$TARGET_MONTH" "$JSON_OUTPUT" <<'PYEOF'
import sys
import yaml
import json
from pathlib import Path
from datetime import date, datetime

target_month = sys.argv[1]  # "" or "YYYY-MM"
json_output = sys.argv[2] == "True"

messages_dir = Path.home() / "messages"
payment_path = messages_dir / "config" / "payment.yaml"
spend_path = messages_dir / "config" / "spend_log.yaml"

# Read payment config
if not payment_path.exists():
    print("Error: payment.yaml not found. Run /buy setup to configure.")
    sys.exit(1)

payment = yaml.safe_load(payment_path.read_text()) or {}
limit = payment.get("spending", {}).get("monthly_limit_usd", 1000)
last4 = payment.get("card", {}).get("number", "????")[-4:]

# Read spend log
if not spend_path.exists():
    purchases = []
else:
    data = yaml.safe_load(spend_path.read_text()) or {}
    purchases = data.get("purchases", [])

# Determine target month
if not target_month:
    target_month = date.today().strftime("%Y-%m")

month_purchases = [
    p for p in purchases
    if p.get("date", "")[:7] == target_month
]

month_total = sum(p.get("amount_usd", 0) for p in month_purchases)
pct = (month_total / limit * 100) if limit > 0 else 0

# Format month name
try:
    month_display = datetime.strptime(target_month, "%Y-%m").strftime("%B %Y")
except ValueError:
    month_display = target_month

if json_output:
    output = {
        "month": target_month,
        "month_display": month_display,
        "total_usd": round(month_total, 2),
        "limit_usd": limit,
        "percent_used": round(pct, 1),
        "at_cap": month_total >= limit,
        "warning": month_total >= 0.8 * limit,
        "last4": last4,
        "purchases": [
            {
                "date": p.get("date"),
                "merchant": p.get("merchant"),
                "item": p.get("item"),
                "amount_usd": p.get("amount_usd"),
                "order_id": p.get("order_id"),
            }
            for p in sorted(month_purchases, key=lambda x: x.get("date", ""), reverse=True)
        ],
    }
    print(json.dumps(output, indent=2))
else:
    # Human-readable output
    if month_total >= limit:
        print(f"LIMIT REACHED: {month_display}: ${month_total:.2f} of ${limit:,} used ({pct:.0f}%)")
    elif month_total >= 0.8 * limit:
        print(f"WARNING (80%+): {month_display}: ${month_total:.2f} of ${limit:,} used ({pct:.0f}%)")
    else:
        print(f"{month_display}: ${month_total:.2f} of ${limit:,} used ({pct:.0f}%)")

    if month_purchases:
        print()
        print("Purchases:")
        sorted_purchases = sorted(month_purchases, key=lambda x: x.get("date", ""), reverse=True)
        for p in sorted_purchases[:10]:
            d = p.get("date", "")
            try:
                d_fmt = datetime.strptime(d, "%Y-%m-%d").strftime("%b %d")
            except ValueError:
                d_fmt = d
            merchant = p.get("merchant", "Unknown")
            item = p.get("item", "Unknown item")
            amount = p.get("amount_usd", 0)
            order_id = p.get("order_id", "N/A")
            print(f"  {d_fmt}  {merchant}  ${amount:.2f}  — {item}")
            print(f"         Order: {order_id}")

        if len(month_purchases) > 10:
            print(f"  ... and {len(month_purchases) - 10} more")
    else:
        print()
        print("No purchases this month.")

    print()
    print(f"Card on file: •••• {last4}")
PYEOF
