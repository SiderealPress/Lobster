## Buy-Things Skill

Lobster can purchase items on the user's behalf using Camofox browser automation.
All purchases require explicit confirmation. The monthly spending cap is enforced.

---

### Command routing

| Trigger | Action |
|---------|--------|
| `/buy <item>` or `/order <item>` | Start purchase flow for `<item>` |
| `"buy me X"`, `"order X"`, `"get me X"` | Same as `/buy X` |
| `/buy setup` | Run onboarding to save card and billing info |
| `/spend` | Show current month spending vs cap |
| `/receipts` | List recent purchases with order IDs |

---

### Dispatcher behavior (main thread — 7-second rule)

When a buy/order trigger is received:

1. Check if `~/messages/config/payment.yaml` exists. If not, run onboarding first.
2. Check monthly spend cap. If already at or over the limit, reply with cap warning and stop.
3. Reply: `"Searching for [item] — one moment..."`
4. Mark message processed via `send_reply(..., message_id=message_id)`
5. Spawn background subagent with the purchase flow prompt (see purchase_flow.md)
6. Return to `wait_for_messages()` immediately

### /spend handler (main thread — inline, fast)

```python
import yaml
from pathlib import Path
from datetime import date

payment_path = Path.home() / "messages/config/payment.yaml"
spend_path = Path.home() / "messages/config/spend_log.yaml"

if not payment_path.exists():
    send_reply(chat_id, "No payment config. Run /buy setup first.", message_id=message_id)
else:
    payment = yaml.safe_load(payment_path.read_text())
    limit = payment.get("spending", {}).get("monthly_limit_usd", 1000)
    last4 = payment.get("card", {}).get("number", "????")[-4:]

    data = yaml.safe_load(spend_path.read_text()) if spend_path.exists() else {}
    purchases = (data or {}).get("purchases", [])
    ym = date.today().strftime("%Y-%m")
    month_purchases = [p for p in purchases if p.get("date", "")[:7] == ym]
    total = sum(p.get("amount_usd", 0) for p in month_purchases)
    pct = int(total / limit * 100) if limit else 0

    month_label = date.today().strftime("%B %Y")
    lines = [f"{month_label}: ${total:.2f} of ${limit:,} used ({pct}%)"]
    if total >= limit:
        lines.insert(0, "Monthly limit reached. Purchases paused until next month.")
    elif total >= 0.8 * limit:
        lines.insert(0, "Warning: 80%+ of monthly budget used.")

    if month_purchases:
        lines.append("")
        lines.append("Recent purchases:")
        for p in sorted(month_purchases, key=lambda x: x.get("date",""), reverse=True)[:5]:
            d = p.get("date", "")
            try:
                from datetime import datetime
                d = datetime.strptime(d, "%Y-%m-%d").strftime("%b %d")
            except Exception:
                pass
            lines.append(f"• {d} — {p.get('merchant','?')} — ${p.get('amount_usd',0):.2f} ({p.get('item','?')[:40]})")
    else:
        lines.append("No purchases this month.")

    send_reply(chat_id, "\n".join(lines), message_id=message_id)
```

### /receipts handler (main thread — inline, fast)

```python
import yaml
from pathlib import Path
from datetime import datetime

spend_path = Path.home() / "messages/config/spend_log.yaml"
payment_path = Path.home() / "messages/config/payment.yaml"

if not spend_path.exists():
    send_reply(chat_id, "No purchases on record.", message_id=message_id)
else:
    data = yaml.safe_load(spend_path.read_text()) or {}
    purchases = data.get("purchases", [])
    if not purchases:
        send_reply(chat_id, "No purchases on record.", message_id=message_id)
    else:
        last4 = ""
        if payment_path.exists():
            payment = yaml.safe_load(payment_path.read_text()) or {}
            last4 = payment.get("card", {}).get("number", "")[-4:]

        recent = sorted(purchases, key=lambda x: x.get("date",""), reverse=True)[:5]
        lines = ["Recent purchases:", ""]
        for i, p in enumerate(recent, 1):
            d = p.get("date", "")
            try:
                d = datetime.strptime(d, "%Y-%m-%d").strftime("%b %d")
            except Exception:
                pass
            oid = p.get("order_id", "N/A")
            lines.append(f"{i}. {d} — {p.get('merchant','?')}")
            lines.append(f"   {p.get('item','?')} — ${p.get('amount_usd',0):.2f}")
            lines.append(f"   Order: {oid}")
            lines.append("")
        if last4:
            lines.append(f"Card used: •••• {last4}")
        send_reply(chat_id, "\n".join(lines), message_id=message_id)
```
