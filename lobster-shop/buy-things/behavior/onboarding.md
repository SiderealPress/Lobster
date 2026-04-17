## Buy-Things Onboarding Flow

Run this flow when the user sends `/buy setup` or when a purchase is attempted
but `~/messages/config/payment.yaml` does not exist.

---

### Trigger conditions

- User sends `/buy setup`
- User triggers a purchase and `~/messages/config/payment.yaml` is missing
- User triggers a purchase and the file exists but is missing required fields

---

### Subagent prompt

When the dispatcher spawns the onboarding subagent, use this prompt:

```
Run the buy-things onboarding flow for the user.

chat_id = <chat_id from the triggering message>

## Step 1 — Greeting

send_reply(chat_id, """Hi! I'm setting up the buy-things skill so I can make purchases on your behalf.

I'll need a payment card to checkout. Your card details will be stored securely in ~/messages/config/payment.yaml (readable only by you).

Let's start: what's the **card number** (16 digits, no spaces)?""")

Wait for the user's reply (check_inbox with limit=1, poll until a message from chat_id arrives).
Store the card number.

## Step 2 — Expiry

send_reply(chat_id, "Got it (ending in XXXX). What's the **expiry date** (MM/YY)?")
Wait for reply. Validate format MM/YY.

## Step 3 — CVV

send_reply(chat_id, "And the **CVV** (3 or 4 digits on the back)?")
Wait for reply. Validate 3-4 digits.

## Step 4 — Billing address

send_reply(chat_id, """Last thing: **billing address** for the card.

Please send in this format:
Street
City, State ZIP
Country

Example:
123 Main St
San Francisco, CA 94110
US""")
Wait for reply. Parse into street, city, state, zip, country.

## Step 5 — Monthly limit

send_reply(chat_id, "What's your **monthly spending limit** in USD? (e.g. 1000 for $1,000/month)")
Wait for reply. Parse as integer.

## Step 6 — Write config

Write to ~/messages/config/payment.yaml with chmod 600:

```yaml
card:
  number: "<full card number>"
  expiry: "<MM/YY>"
  cvv: "<cvv>"
  billing_address:
    street: "<street>"
    city: "<city>"
    state: "<state>"
    zip: "<zip>"
    country: "<country>"

spending:
  monthly_limit_usd: <limit>
  tracker_file: "~/messages/config/spend_log.yaml"
```

Make sure to chmod 600 immediately after writing:
```python
import os, stat
os.chmod(config_path, stat.S_IRUSR | stat.S_IWUSR)
```

Also initialize spend log if not present:
```python
from pathlib import Path
import yaml
spend_path = Path.home() / "messages/config/spend_log.yaml"
if not spend_path.exists():
    spend_path.write_text(yaml.dump({"purchases": []}))
```

## Step 7 — Confirm

Read back the last 4 digits of the card number for the confirmation message.

send_reply(chat_id, f"""Card ending in {last4} saved.
Monthly limit: ${limit:,}

You're all set! Try it:
• `/buy [item name]` — search and buy
• `/spend` — see this month's usage
• `/receipts` — recent purchases

All purchases require your confirmation before checkout.""")
```

---

### Re-onboarding

If the user sends `/buy setup` when a config already exists:

1. Read existing config
2. Reply: `"You already have a card on file ending in XXXX. Do you want to replace it? [Yes] [No]"`
3. If Yes: run the full flow above
4. If No: `"Keeping your existing card. You're ready to shop!"`

---

### Validation rules

- Card number: 13-19 digits, strip spaces/dashes before storing
- Expiry: MM/YY format, month 01-12, year current year or later
- CVV: 3-4 digits only
- ZIP: 5 digits (US) or alphanumeric up to 8 chars (international)
- Monthly limit: positive integer, max $50,000
