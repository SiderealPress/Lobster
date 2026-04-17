## Purchase Flow

This document describes the full purchase flow executed by the background subagent
when a user requests a purchase.

---

### Subagent prompt template

When the dispatcher spawns the purchase subagent, use this prompt:

```
Complete a purchase for the user.

item_query = "<what the user wants to buy>"
chat_id = <chat_id>

## Phase 1 — Load config

Load payment config:
```python
import yaml
from pathlib import Path
payment_path = Path.home() / "messages/config/payment.yaml"
payment = yaml.safe_load(payment_path.read_text())
card = payment["card"]
limit = payment.get("spending", {}).get("monthly_limit_usd", 1000)
```

Load spend log to check monthly total:
```python
spend_path = Path.home() / "messages/config/spend_log.yaml"
if spend_path.exists():
    spend_data = yaml.safe_load(spend_path.read_text()) or {"purchases": []}
else:
    spend_data = {"purchases": []}

from datetime import date
today = date.today()
month_total = sum(
    p["amount_usd"]
    for p in spend_data.get("purchases", [])
    if p.get("date", "")[:7] == today.strftime("%Y-%m")
)

if month_total >= limit:
    send_reply(chat_id, f"Monthly spending cap of ${limit:,} reached. No more purchases this month.")
    return
```

## Phase 2 — Product search

Use fetch_page to search for the item on Amazon:
```python
search_url = f"https://www.amazon.com/s?k={item_query.replace(' ', '+')}"
result = fetch_page(search_url)
```

Parse the top 3-5 results from the page HTML/text. Extract:
- Product title
- Price
- Product URL (ASIN-based: https://www.amazon.com/dp/<ASIN>)
- Image URL (if available)

If Amazon fails, try Google Shopping:
```python
google_url = f"https://www.google.com/search?tbm=shop&q={item_query.replace(' ', '+')}"
result = fetch_page(google_url)
```

## Phase 3 — Present results to user

Format as a numbered list with prices:

send_reply(chat_id, f"""Found these for "{item_query}":

1. {title1} — ${price1}
   {url1}

2. {title2} — ${price2}
   {url2}

3. {title3} — ${price3}
   {url3}

Reply with 1, 2, or 3 to select, or "none" to cancel.""")

Wait for user reply (check_inbox, poll until a message from chat_id arrives, timeout 5 minutes).

If user replies "none" or "cancel": reply "No purchase made." and stop.
If user replies 1, 2, or 3: proceed with the selected product.

## Phase 4 — Confirmation

After selection, read the selected product details and ask for confirmation:

send_reply(chat_id, f"""Order summary:

**{selected_title}**
${selected_price} from Amazon

Shipping to: {card['billing_address']['street']}, {card['billing_address']['city']}

Total (estimated): ${selected_price}

Reply **yes** to confirm, or **no** to cancel.""")

Wait for user reply (timeout 10 minutes).

If "no" or "cancel": reply "Purchase cancelled." and stop.
If "yes" or "confirm": proceed with checkout.

## Phase 5 — Checkout via Camofox

Navigate to the product page and complete checkout.
See camofox_checkout.md for detailed Camofox instructions.

Pass to checkout:
- product_url: selected product URL
- card: payment config card object
- chat_id: for sending status updates

## Phase 6 — Record purchase

After successful checkout:

```python
import yaml
from pathlib import Path
from datetime import date

spend_path = Path.home() / "messages/config/spend_log.yaml"
spend_data = yaml.safe_load(spend_path.read_text()) if spend_path.exists() else {"purchases": []}
if spend_data is None:
    spend_data = {"purchases": []}

spend_data["purchases"].append({
    "date": date.today().isoformat(),
    "merchant": "Amazon",
    "item": selected_title,
    "amount_usd": float(selected_price),
    "order_id": order_id,
    "last4": card["number"][-4:],
})

spend_path.write_text(yaml.dump(spend_data, default_flow_style=False))
```

## Phase 7 — Send receipt

send_reply(chat_id, f"""Order placed!

**{selected_title}**
Order #{order_id}
Amount: ${selected_price}
Card: •••• {card['number'][-4:]}

I'll send a screenshot of the confirmation.""")

Save the confirmation screenshot to:
~/messages/receipts/{date.today().isoformat()}-{order_id}.png

If screenshot saved successfully, send it via send_reply (text = order summary).

Check new monthly total and warn if over 80%:
```python
new_total = month_total + float(selected_price)
if new_total >= 0.8 * limit:
    send_reply(chat_id, f"Heads up: you've now used ${new_total:.2f} of your ${limit:,} monthly limit ({new_total/limit*100:.0f}%).")
```
```

---

### Waiting for user replies

When waiting for user input between steps:

```python
import time

def wait_for_user_reply(chat_id, timeout_seconds=300):
    """Poll inbox for a reply from the given chat_id."""
    start = time.time()
    while time.time() - start < timeout_seconds:
        messages = check_inbox(source="telegram", limit=5)
        for msg in messages:
            if msg.get("chat_id") == chat_id:
                mark_processed(msg["message_id"])
                return msg.get("text", "").strip().lower()
        time.sleep(3)
    return None  # Timeout
```

If the user doesn't respond within the timeout:
- For product selection: `"No selection received. Purchase cancelled."`
- For confirmation: `"Confirmation timed out. Purchase cancelled."`

---

### Error handling

| Error | Response |
|-------|----------|
| No products found | `"Couldn't find '{item}'. Try a different description?"` |
| Checkout page error | `"Checkout page failed to load. Want me to try again?"` |
| Out of stock | `"That item appears to be out of stock. Want me to search for alternatives?"` |
| Card declined | `"Card declined. Check your payment details with /buy setup."` |
| Address not accepted | `"Amazon didn't accept the shipping address. Check /buy setup to update it."` |
| Screenshot failed | Proceed without screenshot, note in receipt message |
| Network error | `"Network error during checkout. Want me to retry?"` |
