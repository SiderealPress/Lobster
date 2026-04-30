# buy-things — Lobster Skill

Purchase items on your behalf via Telegram. Lobster searches for products,
presents options, and completes checkout using Camofox browser automation.
All purchases require your confirmation.

---

## Quick Start

### 1. Install

```bash
cd /home/admin/lobster/lobster-shop/buy-things
bash scripts/install.sh
```

The installer:
- Creates `~/messages/config/` and `~/messages/receipts/`
- Initializes `spend_log.yaml`
- Registers the skill with Lobster's skill manager

### 2. Configure payment card

Send `/buy setup` to your Lobster Telegram bot. Lobster will ask for:
- Card number
- Expiry date (MM/YY)
- CVV
- Billing address
- Monthly spending limit

Card details are stored in `~/messages/config/payment.yaml` (chmod 600).

### 3. Activate the skill

Via Telegram: tell Lobster to activate the buy-things skill.

Or via MCP:
```python
activate_skill(skill_name="buy-things", mode="triggered")
```

### 4. Buy something

```
/buy Sony headphones
```

---

## Commands

| Command | Description |
|---------|-------------|
| `/buy <item>` | Search and purchase an item |
| `/order <item>` | Same as `/buy` |
| `/spend` | Current month's spending vs cap |
| `/receipts` | Last 5 purchases |
| `/buy setup` | Add or update payment card |

### Natural language triggers

These phrases also activate the skill:
- "buy me X"
- "order X from Y"
- "get me X"
- "can you buy X"
- "purchase X"

---

## Purchase Flow

1. **Search**: Lobster searches Amazon for your item
2. **Options**: Lobster presents 3 results with prices
3. **Select**: You reply `1`, `2`, or `3` (or `none` to cancel)
4. **Confirm**: Lobster shows order summary and asks for confirmation
5. **Checkout**: On `yes`, Lobster completes checkout via Camofox
6. **Receipt**: Lobster sends you the order number and a screenshot

---

## Configuration Files

### `~/messages/config/payment.yaml`

Stores your payment card and spending settings. Permissions: chmod 600.

```yaml
card:
  number: "XXXXXXXXXXXXXXXX"   # Full card number (never shown to user)
  expiry: "MM/YY"
  cvv: "XXX"
  billing_address:
    street: "123 Main St"
    city: "Your City"
    state: "CA"
    zip: "00000"
    country: "US"

spending:
  monthly_limit_usd: 1000
  tracker_file: "~/messages/config/spend_log.yaml"
```

### `~/messages/config/spend_log.yaml`

Running log of all purchases:

```yaml
purchases:
  - date: "2026-04-15"
    merchant: "Amazon"
    item: "Product Name"
    amount_usd: 29.99
    order_id: "123-4567890-1234567"
    last4: "XXXX"
```

### `~/messages/receipts/`

Screenshots of order confirmation pages:
- Filename: `YYYY-MM-DD-{order_id}.png`

---

## Preferences

Customize via MCP `set_skill_preference` or by editing `preferences/defaults.toml`:

| Preference | Default | Description |
|------------|---------|-------------|
| `monthly_limit_usd` | `1000` | Monthly cap (payment.yaml takes precedence) |
| `preferred_store` | `"amazon"` | Default store |
| `search_results_count` | `3` | Number of search results to show |
| `require_confirmation` | `true` | Always confirm before checkout |
| `spend_warning_threshold_pct` | `80` | Warn at this % of cap |
| `shipping_preference` | `"free"` | Preferred shipping speed |
| `save_receipts` | `true` | Save confirmation screenshots |
| `default_shipping_name` | `"Your Name"` | Name on shipping address |

---

## CLI Spend Check

```bash
# Current month
bash scripts/check_spend.sh

# Specific month
bash scripts/check_spend.sh --month 2026-03

# JSON output (for scripting)
bash scripts/check_spend.sh --json
```

---

## Security

- Card number stored only in `payment.yaml` (chmod 600)
- All receipts and logs show only last 4 digits
- Confirmation required before every purchase (no bypass)
- Monthly spending cap enforced at both dispatcher and checkout levels
- Guest checkout used where possible (no Amazon account credentials needed)

---

## Supported Stores

| Store | Status | Notes |
|-------|--------|-------|
| Amazon | Primary | Best support, guest checkout |
| Target | Secondary | Guest checkout available |
| Walmart | Secondary | Guest checkout available |
| Best Buy | Secondary | Guest checkout available |

---

## Troubleshooting

**"Monthly limit reached"** — You've hit your cap. It resets on the 1st of next month.
Update your limit with `/buy setup`.

**"Checkout failed"** — Camofox couldn't complete the purchase. Common causes:
- Amazon requires a sign-in (no guest checkout for that item)
- CAPTCHA presented (Camofox will notify you)
- Network error

**"Card declined"** — Update your card with `/buy setup`.

**"Item not found"** — Try a more specific search term (include brand, model number).

---

## Architecture

```
buy-things/
├── skill.toml                    # Skill manifest (triggers, permissions, deps)
├── behavior/
│   ├── system.md                 # Main dispatcher behavior + /spend + /receipts
│   ├── onboarding.md             # /buy setup flow
│   ├── purchase_flow.md          # Full purchase subagent prompt
│   └── camofox_checkout.md       # Amazon checkout via Camofox
├── context/
│   ├── domain.md                 # Product search knowledge, store patterns
│   └── spend_tracking.md         # Spend log format + helper code
├── preferences/
│   ├── defaults.toml             # Default settings
│   └── schema.toml               # Valid preference keys
├── scripts/
│   ├── install.sh                # Installer
│   └── check_spend.sh            # CLI spend reporter
└── README.md                     # This file
```

The skill is a pure behavior layer — no daemons, no background processes.
It activates when triggered and delegates purchases to background subagents
following Lobster's 7-second rule.
