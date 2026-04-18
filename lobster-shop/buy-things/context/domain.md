## Buy-Things Domain Knowledge

### Primary Shopping Target: Amazon

Amazon is the primary checkout target because:
- Wide product catalog
- Reliable checkout flow
- Consistent page structure for Camofox automation
- Fast shipping options
- Guest checkout available on most products

Amazon product URLs follow this pattern:
- `https://www.amazon.com/dp/<ASIN>` — direct product link
- `https://www.amazon.com/s?k=<query>` — search page

### Secondary Shopping Targets

| Store | Best For | Checkout Notes |
|-------|----------|---------------|
| Target (target.com) | Household goods, electronics | Requires account or guest; standard form checkout |
| Walmart (walmart.com) | Budget items, groceries | Guest checkout available |
| Best Buy (bestbuy.com) | Electronics, appliances | Guest checkout available |
| Home Depot (homedepot.com) | Tools, hardware | Guest checkout available |
| Chewy (chewy.com) | Pet supplies | Guest checkout available |

### Product Search Strategies

**Amazon search URL:** `https://www.amazon.com/s?k={query}&sort=review-rank`

**Query optimization tips:**
- For electronics: include brand + model if known (e.g., "Sony WH-1000XM5")
- For household goods: include material/size (e.g., "stainless steel mixing bowl 5 quart")
- For books: include author name (e.g., "Thinking Fast and Slow Kahneman")
- For generic items: add "bestseller" or sort by reviews

**Parsing Amazon search results:**
The search results page has product cards with:
- `.s-result-item[data-asin]` — each result has a data-asin attribute
- `.a-price-whole` — whole dollar amount
- `.a-price-fraction` — cents
- `.a-size-medium .a-link-normal` — product title link
- `[data-asin]` attribute — the ASIN to build the product URL

**Price extraction:**
Amazon prices may appear as:
- `$XX.XX` — standard format
- `$XX` (no cents) — whole dollar
- Price range `$XX-$XX` — pick the lower end for search results

### Common Product Categories and Notes

| Category | Search tip | Watch out for |
|----------|-----------|---------------|
| Electronics | Include model number if known | Check return policy |
| Clothing | Include size in query | Check seller ratings |
| Books | Author + title | Check if Kindle vs. physical |
| Food/Pantry | Check "Sold by Amazon" (not 3rd party) | Check expiry dates if shown |
| Tools/Hardware | Brand matters (DeWalt, Milwaukee, etc.) | Check voltage/battery compatibility |
| Beauty/Personal care | Check brand + product line | Read ingredient warnings |

### Price sanity checks

Before confirming a purchase, validate the price:
- If price > $500: add extra confirmation step ("This is $XXX — are you sure?")
- If price seems unusually low (< $1 for a physical item): warn about potential scam/counterfeit
- If price changed between search and checkout by > $5: show both prices and ask user

### Shipping address notes

The billing address in payment.yaml is used as the shipping address.
Standard shipping options:
- FREE delivery (Prime or $25+ orders)
- Same-day delivery (available in select areas)
- Standard delivery (3-5 days)

### Order ID formats

| Merchant | Format | Example |
|----------|--------|---------|
| Amazon | `\d{3}-\d{7}-\d{7}` | `123-4567890-1234567` |
| Target | `T\d{9}` | `T100123456` |
| Walmart | `\d{13}` | `2000012345678` |
| Best Buy | `BBY\d{9}` | `BBY012345678` |
