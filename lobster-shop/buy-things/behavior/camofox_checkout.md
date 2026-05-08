## Camofox Checkout Automation

Detailed instructions for completing Amazon checkout using the Camofox browser.

---

### Prerequisites

- Camofox server running at `http://localhost:9377`
- Product URL (Amazon ASIN link)
- Payment card details from `~/messages/config/payment.yaml`
- Billing/shipping address from payment config

---

### Amazon Checkout Flow

#### Step 1 — Navigate to product page

```python
camofox_navigate(product_url)
# Wait for page to load; take a snapshot to confirm
snapshot = camofox_snapshot()
```

Check for "Add to Cart" button in the snapshot. If not found:
- Check for "Buy Now" button (use that instead)
- Check for "Currently unavailable" — if present, report out of stock and stop

#### Step 2 — Add to Cart

Find the "Add to Cart" button reference from the snapshot and click it:

```python
# Find the ref for "Add to Cart" button in the accessibility tree
camofox_click(add_to_cart_ref)
```

Take a snapshot to confirm cart was updated. Look for "Added to Cart" confirmation
or cart count changing. If it failed, try the "Buy Now" flow instead.

#### Step 3 — Proceed to Checkout

Navigate to cart or click "Proceed to Checkout":

```python
camofox_navigate("https://www.amazon.com/gp/cart/view.html")
snapshot = camofox_snapshot()
# Find "Proceed to checkout" button and click it
camofox_click(proceed_to_checkout_ref)
```

#### Step 4 — Sign In (if required)

If Amazon shows a login page, use guest checkout or skip.
Amazon typically prompts for sign-in at checkout. If login is required:

```python
# Check if login page is shown
snapshot = camofox_snapshot()
# Look for "Continue as guest" or "Sign in" options
# Prefer "Continue as guest" to avoid account credentials
camofox_click(guest_checkout_ref)
```

If guest checkout is not available, the checkout cannot proceed without Amazon credentials.
Send the user a message explaining and stop.

#### Step 5 — Enter Shipping Address

Amazon will prompt for a shipping address. Fill in the billing address from config:

```python
card = payment_config["card"]
addr = card["billing_address"]

# Fill in each address field
# Find the input refs in the snapshot
camofox_type(name_field_ref, "Drew Winget")  # Use owner name if available
camofox_type(street_field_ref, addr["street"])
camofox_type(city_field_ref, addr["city"])
# Select state from dropdown if needed
camofox_type(zip_field_ref, addr["zip"])
# Country is usually pre-selected for US
```

Click "Continue" or "Use this address".

#### Step 6 — Select Shipping Speed

Amazon will show shipping options. Select the cheapest/free shipping option:

```python
snapshot = camofox_snapshot()
# Find free shipping / standard shipping option
# Usually "FREE delivery" — click that option
camofox_click(free_shipping_ref)
camofox_click(continue_button_ref)
```

#### Step 7 — Enter Payment

Fill in payment details:

```python
card = payment_config["card"]

# Click "Add a credit or debit card" or find existing payment fields
camofox_type(card_number_field_ref, card["number"])
camofox_type(expiry_field_ref, card["expiry"])  # MM/YY format
camofox_type(cvv_field_ref, card["cvv"])
```

Amazon may split expiry into month/year dropdowns — handle both:
```python
# If separate month/year fields:
month, year = card["expiry"].split("/")
# Select month from dropdown: month (e.g., "04")
# Select year from dropdown: "20" + year (e.g., "2031")
```

Click "Add your card" or "Continue".

#### Step 8 — Review Order

Take a final snapshot to review the order:

```python
snapshot = camofox_snapshot()
# Read the order total from the page
# Extract order total for recording
```

Verify the price matches what was shown to the user (within $5 for tax/shipping variance).
If the total is significantly different, send the user a message and ask for re-confirmation.

#### Step 9 — Place Order

Click "Place your order":

```python
camofox_click(place_order_button_ref)
```

Wait for the confirmation page to load (up to 30 seconds):

```python
import time
time.sleep(5)
snapshot = camofox_snapshot()
```

#### Step 10 — Capture Confirmation

Look for the order confirmation in the snapshot:
- "Thank you, your order has been placed"
- Order number (format: 123-XXXXXXX-XXXXXXX)

Take a screenshot:

```python
screenshot = camofox_screenshot()
# Save screenshot
from pathlib import Path
from datetime import date
receipts_dir = Path.home() / "messages/receipts"
receipts_dir.mkdir(parents=True, exist_ok=True)
screenshot_path = receipts_dir / f"{date.today().isoformat()}-{order_id}.png"
# Write screenshot bytes to file
screenshot_path.write_bytes(screenshot)
```

Extract the order ID from the page for the receipt.

---

### Handling common Amazon checkout issues

| Situation | Action |
|-----------|--------|
| "Sign in required" with no guest option | Notify user, stop, ask for Amazon credentials |
| CAPTCHA detected | Use `camofox_screenshot()` to show user, ask them to solve it |
| "Address not recognized" | Try reformatting ZIP+4, or ask user to update address |
| Card error at payment | Report declined, stop, direct user to `/buy setup` |
| "Item no longer available" during checkout | Report out of stock, stop |
| Page won't load | Try `camofox_navigate(url)` again once; if still fails, report error |
| Unexpectedly high shipping cost | Show user the breakdown and ask to confirm |

---

### Fallback: "Buy Now" flow

If "Add to Cart" is unavailable but "Buy Now" exists:

```python
camofox_click(buy_now_ref)
# Skips cart, goes directly to checkout
# Follow steps 4-10 above
```

---

### Detecting page elements

When reading the Camofox accessibility snapshot, look for these patterns:

- **Add to Cart**: role=button, name contains "Add to Cart"
- **Buy Now**: role=button, name contains "Buy Now"
- **Checkout**: role=button, name contains "Proceed to checkout" or "Place your order"
- **Input fields**: role=textbox with name/label matching "card number", "expiration", "CVV"
- **Order number**: text matching pattern `\d{3}-\d{7}-\d{7}`
- **Order confirmation**: text containing "Thank you" or "order has been placed"

Always take a snapshot before clicking to get current refs — refs change between pages.
