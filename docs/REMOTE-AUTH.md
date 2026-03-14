# Remote / Headless Authentication

Claude Code uses OAuth tokens that expire periodically. On a headless VPS there is
no browser to complete the re-authentication flow. If the token expires unnoticed,
the Claude session crash-loops silently — in one incident, 22,551 restart attempts
over several weeks went undetected.

This document covers how to re-authenticate and how to detect expired tokens.

---

## Method 1: `claude setup-token` (preferred)

1. SSH to the VPS:

   ```bash
   ssh root@162.55.60.42
   ```

2. Run `setup-token` as the lobster user:

   ```bash
   sudo -u lobster bash -c '
     export HOME=/home/lobster
     export PATH=/home/lobster/.local/bin:/usr/local/bin:/usr/bin:/bin
     claude setup-token
   '
   ```

3. The CLI displays an OAuth URL. Copy it and open in **any** browser (your laptop, phone, etc.).

4. Authorize in the browser. You will be redirected to a callback page.

5. The CLI polls `platform.claude.com` automatically using the `state` parameter. Wait up to 30 seconds — it should pick up the code and write credentials.

6. Verify credentials were written:

   ```bash
   cat /home/lobster/.claude/.credentials.json | python3 -c '
     import json, sys, datetime
     d = json.load(sys.stdin)
     oauth = d.get("claudeAiOauth", {})
     if oauth.get("accessToken"):
         exp = datetime.datetime.fromtimestamp(oauth["expiresAt"] / 1000)
         print(f"OK — token expires {exp}")
     else:
         print("MISSING — no token found")
   '
   ```

7. Restart the Claude session:

   ```bash
   systemctl restart lobster-claude
   ```

---

## Method 2: Transfer credentials from local machine (fallback)

If `setup-token` polling does not pick up the code (network issues, timeout, etc.),
transfer OAuth credentials from a machine where Claude Code is already authenticated.

1. **On your Mac** — extract credentials from Keychain:

   ```bash
   security find-generic-password -s "Claude Code-credentials" -w > /tmp/creds.json
   ```

2. **Transfer to VPS**:

   ```bash
   scp /tmp/creds.json root@162.55.60.42:/home/lobster/.claude/.credentials.json
   ```

3. **Fix ownership and permissions**:

   ```bash
   ssh root@162.55.60.42 "chown lobster:lobster /home/lobster/.claude/.credentials.json && chmod 600 /home/lobster/.claude/.credentials.json"
   ```

4. **Clean up locally**:

   ```bash
   rm /tmp/creds.json
   ```

5. **Restart**:

   ```bash
   ssh root@162.55.60.42 "systemctl restart lobster-claude"
   ```

---

## ANTHROPIC_API_KEY conflict

If `ANTHROPIC_API_KEY` is set in the environment, Claude Code uses it **instead of**
OAuth — even when valid OAuth credentials exist. The `config.env` file sets this
variable for other Lobster services (MCP server, scheduled tasks).

`claude-persistent.sh` unsets `ANTHROPIC_API_KEY` before launching Claude so OAuth
is used. If you see "API usage limits" or "Invalid API key" errors in
`claude-session.log` but the credentials file looks valid, check whether the env
var is leaking through.

---

## Detecting expired auth

Signs that authentication has expired:

- **Session log**: `tail /home/lobster/lobster-workspace/logs/claude-session.log` shows
  repeated `"authentication_error"` or `"OAuth token has expired"` messages.
- **Persistent log**: `tail /home/lobster/lobster-workspace/logs/claude-persistent.log`
  shows rapid `"Claude exited with code 1"` entries every 5–50 seconds.
- **Tmux session dead**: `lobster status` shows `lobster-claude` as "running" (systemd
  thinks it is alive because `RemainAfterExit=yes`) but the tmux session is gone:
  ```bash
  sudo -u lobster tmux -L lobster list-sessions
  # "no server running" = Claude is not running
  ```
- **No Telegram responses**: The bot accepts messages but Claude never processes them.

### Check token expiry directly

```bash
python3 -c '
  import json, datetime
  d = json.load(open("/home/lobster/.claude/.credentials.json"))
  exp = d["claudeAiOauth"]["expiresAt"] / 1000
  print("Expires:", datetime.datetime.fromtimestamp(exp))
  print("Status:", "EXPIRED" if exp < datetime.datetime.now().timestamp() else "VALID")
'
```

---

## Post-auth checklist

1. **Verify credentials exist**:

   ```bash
   cat /home/lobster/.claude/.credentials.json | python3 -c '
     import json, sys
     d = json.load(sys.stdin)
     print("OK" if d.get("claudeAiOauth", {}).get("accessToken") else "MISSING")
   '
   ```

2. **Test Claude directly**:

   ```bash
   sudo -u lobster bash -c '
     export HOME=/home/lobster
     export PATH=/home/lobster/.local/bin:/usr/local/bin:/usr/bin:/bin
     claude -p "hi" --max-turns 1 2>&1
   '
   ```

3. **Restart the service**:

   ```bash
   systemctl restart lobster-claude
   ```

4. **Verify startup** (wait 15–20 seconds for Claude to initialize):

   ```bash
   tail -5 /home/lobster/lobster-workspace/logs/claude-persistent.log
   ```

   You should see `"Starting fresh session (attempt 1)..."` without an immediate
   `"Claude exited with code 1"` on the next line.
