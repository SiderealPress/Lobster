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

When `/spend` is received:
- Handle directly (< 7 seconds): read spend_log.yaml, compute monthly total, reply
- Format: `"April 2026: $XXX.XX of $1,000 used (XX%)\n\nRecent: ..."`

When `/receipts` is received:
- Handle directly (< 7 seconds): read spend_log.yaml, show last 5 entries
- Format: one per line with date, merchant, amount, last-4 digits masked order info

When `/buy setup` is received:
- Reply: `"Starting buy-things setup — I'll ask a few questions."`
- Spawn onboarding subagent (see onboarding.md)

---

### Button press UX rules (MANDATORY)

Every button press or user reply must generate an **immediate text response within 2 seconds**.
Never silently transition between states. Send an acknowledgment first, then do background work.

| User action | Immediate acknowledgment |
|-------------|---------------------------|
| Selects option 1 / 2 / 3 / 4 | `"Got it — let me pull up the details for option X..."` |
| Presses "✅ Yes, place order" / replies "yes" | `"✅ Order confirmed! Starting checkout via Camofox now — I'll send your order number in a minute."` |
| Presses "❌ Cancel" / replies "no" | `"Order cancelled — nothing was purchased."` |
| Any other button or ambiguous reply | `"On it..."` |

**Rule:** if a button press triggers background work, send the acknowledgment **before** spawning
the subagent or doing any I/O. This prevents the "did it work?" confusion.

---

### Security rules (non-negotiable)

- NEVER complete a purchase without explicit user confirmation ("Yes" / "Confirm")
- NEVER log or display full card numbers — always show only last 4 digits
- NEVER bypass the monthly spending cap
- NEVER retry a failed checkout without asking user first
- The payment.yaml file must always be chmod 600

---

### Spend tracking (inline for /spend and /receipts)

Read `~/messages/config/spend_log.yaml`:

```python
import yaml
from pathlib import Path
from datetime import date

log_path = Path.home() / "messages/config/spend_log.yaml"
if log_path.exists():
    data = yaml.safe_load(log_path.read_text()) or {"purchases": []}
else:
    data = {"purchases": []}

purchases = data.get("purchases", [])

# Current month total
today = date.today()
month_total = sum(
    p["amount_usd"]
    for p in purchases
    if p.get("date", "")[:7] == today.strftime("%Y-%m")
)
```

Read monthly limit from `~/messages/config/payment.yaml`:
```python
payment_path = Path.home() / "messages/config/payment.yaml"
payment = yaml.safe_load(payment_path.read_text())
limit = payment.get("spending", {}).get("monthly_limit_usd", 1000)
```

Format the /spend response:
```
[Month] [Year]: $XX.XX of $1,000.00 used (XX%)

Recent purchases:
• [date] — [merchant] — $XX.XX
• [date] — [merchant] — $XX.XX
```

Alert threshold: if month_total >= 0.8 * limit, prepend:
`"Warning: you've used 80%+ of your monthly limit."`

---

### Error handling

- Payment config missing → run onboarding, do not proceed with purchase
- At monthly cap → `"Monthly limit of $XXX reached. No purchases this month."`
- Item not found in search → `"Couldn't find [item]. Try a different search term?"`
- Checkout failure → `"Checkout failed: [reason]. Want me to try again?"`
- Card declined → `"Card declined. Check your payment config with /buy setup."`
