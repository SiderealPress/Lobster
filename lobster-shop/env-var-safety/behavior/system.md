## Env Var Safety (Always On)

When setting, reading, or editing environment variables in ANY context (Vercel CLI, shell, .env files, config files, code), ALWAYS apply the rules below.

---

### Before writing an env var value

1. **Never use `echo` to pipe a value** — `echo "value"` appends `\n`. Use `printf '%s'` instead:
   ```sh
   # Wrong — adds trailing newline:
   echo "my-secret" | vercel env add MY_SECRET

   # Correct — no trailing newline:
   printf '%s' "my-secret" | vercel env add MY_SECRET
   ```

2. **Verify no leading/trailing whitespace** before saving. If you must check:
   ```sh
   printf '%s' "$MY_VAR" | cat -A   # Shows trailing whitespace/newlines
   ```

3. **When setting via `vercel env add`** — always pipe through `printf '%s'`, never `echo`.

4. **Quotes must not be included in the value** — the value `"Bearer token"` (with literal quotes) is wrong; it should be `Bearer token`.

---

### Quadruple-check for these failure modes

| Symptom | Root cause |
|---------|------------|
| OAuth 400 "invalid_client" | `\n` in GOOGLE_CLIENT_ID or CLIENT_SECRET |
| OAuth redirect_uri_mismatch | `\n` or space in GOOGLE_REDIRECT_URI |
| Stripe signature invalid | Whitespace in STRIPE_SECRET_KEY |
| API 401 Unauthorized | Leading/trailing whitespace in token |
| Twilio error 20003 | Whitespace in TWILIO_ACCOUNT_SID or AUTH_TOKEN |

---

### When writing code that reads env vars

**TypeScript/Node.js — use the validated loader:**
```ts
import { requireEnv, optionalEnv } from "@/lib/env";

// Required vars (throws on missing or whitespace-corrupted):
const clientId = requireEnv("GOOGLE_CLIENT_ID");

// Optional vars (returns undefined if unset; still throws on corruption):
const botToken = optionalEnv("ADMIN_TELEGRAM_BOT_TOKEN");
```

**Never use `process.env.FOO` directly for secrets, URLs, or tokens** — always route through `requireEnv` or `optionalEnv`.

**Python — at minimum, validate on read:**
```python
import os

def require_env(name: str) -> str:
    raw = os.environ.get(name)
    if not raw:
        raise ValueError(f"Missing required env var: {name}")
    if raw != raw.strip() or "\n" in raw or "\r" in raw or "\t" in raw:
        raise ValueError(
            f"Env var {name} contains invalid whitespace. "
            f"Raw value: {repr(raw)}"
        )
    return raw
```

---

### When debugging OAuth/API 400 errors

**First check — always do this before anything else:**

```ts
// In TypeScript:
console.log(JSON.stringify(process.env.GOOGLE_CLIENT_ID));
// If you see "\"my-id\\n\"" — that \n is the bug
```

```python
# In Python:
import os, json
print(json.dumps(os.environ.get("GOOGLE_CLIENT_ID")))
```

If you see a `\n` or any whitespace in the JSON output, the fix is:

1. Re-set the env var in Vercel without the newline:
   ```sh
   printf '%s' "clean-value-here" | vercel env add GOOGLE_CLIENT_ID production
   ```

2. Redeploy.

---

### Summary contract

- **`requireEnv`**: throws `Missing required env var: NAME` if undefined/empty; throws `contains invalid whitespace` if corrupted; returns the valid value.
- **`optionalEnv`**: returns `undefined` if not set; throws `contains invalid whitespace` if set but corrupted; returns the valid value otherwise.
- Both validators catch: trailing `\n`, leading spaces, trailing spaces, embedded `\n`, `\r`, `\t`.
- If an env var has leading/trailing whitespace, the app throws a descriptive error immediately at the point of use rather than silently corrupting OAuth URLs or API credentials.
