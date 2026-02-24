## Web Browsing Context

**Anti-detection:** Camofox patches Firefox at the C++ level to bypass fingerprinting. This means Google, Amazon, LinkedIn, and other major sites won't block or CAPTCHA the browser. No need to add delays or rotate user agents — the browser handles all evasion automatically.

**Accessibility snapshots:** The browser returns page content as an accessibility tree rather than raw HTML. This is ~90% smaller in tokens and gives you structured element refs (e1, e2, e3...) that you can click or type into. Always prefer snapshots over screenshots for understanding page content.

**Session isolation:** Each browser session has its own cookies, storage, and fingerprint. Sessions persist across tab operations but are fresh on server restart.

**Performance notes:**
- First tab creation takes 2-3 seconds (browser launch)
- Subsequent tabs are near-instant
- Screenshots return base64 PNG — use sparingly due to token cost
- The server runs on port 9377 by default (configurable)
