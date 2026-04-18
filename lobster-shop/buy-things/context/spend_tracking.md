## Spend Tracking

### Files

| File | Purpose |
|------|---------|
| `~/messages/config/payment.yaml` | Card details and monthly limit (chmod 600) |
| `~/messages/config/spend_log.yaml` | Running purchase log |
| `~/messages/receipts/` | Order confirmation screenshots |

---

### spend_log.yaml format

```yaml
purchases:
  - date: "2026-04-15"
    merchant: "Amazon"
    item: "Sony WH-1000XM5 Headphones"
    amount_usd: 279.99
    order_id: "123-4567890-1234567"
    last4: "XXXX"

  - date: "2026-04-12"
    merchant: "Amazon"
    item: "LEGO Technic Set"
    amount_usd: 49.99
    order_id: "123-9876543-7654321"
    last4: "XXXX"
```

### Reading current month total

```python
import yaml
from pathlib import Path
from datetime import date

def get_month_total(year_month: str | None = None) -> float:
    """Get total spending for a given month (YYYY-MM). Defaults to current month."""
    if year_month is None:
        year_month = date.today().strftime("%Y-%m")

    spend_path = Path.home() / "messages/config/spend_log.yaml"
    if not spend_path.exists():
        return 0.0

    data = yaml.safe_load(spend_path.read_text()) or {}
    purchases = data.get("purchases", [])
    return sum(
        p["amount_usd"]
        for p in purchases
        if p.get("date", "")[:7] == year_month
    )
```

### Recording a purchase

```python
def record_purchase(merchant: str, item: str, amount_usd: float, order_id: str, last4: str) -> None:
    spend_path = Path.home() / "messages/config/spend_log.yaml"
    data = yaml.safe_load(spend_path.read_text()) if spend_path.exists() else {}
    if not data:
        data = {}
    if "purchases" not in data:
        data["purchases"] = []

    data["purchases"].append({
        "date": date.today().isoformat(),
        "merchant": merchant,
        "item": item,
        "amount_usd": float(amount_usd),
        "order_id": order_id,
        "last4": last4,
    })

    spend_path.write_text(yaml.dump(data, default_flow_style=False, allow_unicode=True))
```

### /spend response format

```
April 2026: $329.98 of $1,000 used (33%)

Recent purchases:
• Apr 15 — Amazon — $279.99 (Sony WH-1000XM5)
• Apr 12 — Amazon — $49.99 (LEGO Technic Set)
```

At 80%+ usage, prepend:
```
Warning: 80%+ of monthly budget used.
```

At 100%+ usage:
```
Monthly limit of $1,000 reached. Purchases paused until next month.
```

### /receipts response format

Show last 5 purchases:

```
Recent purchases:

1. Apr 15 — Amazon
   Sony WH-1000XM5 — $279.99
   Order: 123-4567890-XXXXXXX

2. Apr 12 — Amazon
   LEGO Technic Set — $49.99
   Order: 123-9876543-XXXXXXX

Card used: •••• XXXX
```

Note: order IDs are shown in full (they are not sensitive).

### Monthly cap enforcement

The cap is checked in two places:
1. **Before searching** (dispatcher): if already at cap, stop immediately
2. **Before checkout** (purchase subagent): re-check in case another purchase ran concurrently

Both checks use the same logic:
```python
payment = yaml.safe_load(payment_path.read_text())
limit = payment.get("spending", {}).get("monthly_limit_usd", 1000)
month_total = get_month_total()

if month_total >= limit:
    # Block purchase
    return
```

### Cap reset

The cap resets on the 1st of each month — no action needed. `get_month_total()`
filters by `YYYY-MM` prefix, so the new month automatically starts at $0.

### Screenshot receipts

Confirmation screenshots are saved to `~/messages/receipts/`:
- Filename: `YYYY-MM-DD-{order_id}.png`
- Example: `2026-04-15-123-4567890-1234567.png`

The receipts directory is created by `scripts/install.sh`. If it doesn't exist,
create it with `mkdir -p ~/messages/receipts`.
