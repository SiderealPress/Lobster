# Remote / Headless Authentication

Claude Code uses OAuth credentials stored in `~/.claude/.credentials.json`. This file
carries a refresh token, enabling Claude Code to silently renew the access token without
any manual intervention. This is the **only** supported auth mechanism in Lobster (Option B).

If the credentials file is missing or the refresh token is absent, the Claude session
will fail to authenticate. If the file is present but the access token has expired,
Claude Code refreshes it automatically on the next API call.

---

## Authenticate (or re-authenticate)

Run `claude auth login` as the lobster user. It generates an OAuth URL that you open in
**any** browser (your laptop, phone, etc.). After authorizing in the browser, the CLI
polls for the callback and writes `~/.claude/.credentials.json` automatically.

```bash
sudo -u lobster bash -c '
  export HOME=/home/lobster
  export PATH=/home/lobster/.local/bin:/usr/local/bin:/usr/bin:/bin
  claude auth login
'
```

No token copying or pasting is required. The credentials file will contain both an
`accessToken` and a `refreshToken`.

---

## Verify credentials

```bash
cat /home/lobster/.claude/.credentials.json | python3 -c '
  import json, sys, datetime
  d = json.load(sys.stdin)
  oauth = d.get("claudeAiOauth", {})
  if oauth.get("refreshToken"):
      exp = datetime.datetime.fromtimestamp(oauth["expiresAt"] / 1000)
      print(f"OK — refresh token present, access token expires {exp}")
  elif oauth.get("accessToken"):
      print("WARNING — access token present but NO refresh token (re-run claude auth login)")
  else:
      print("MISSING — no token found (run claude auth login)")
'
```

---

## Transfer credentials from another machine (fallback)

If `claude auth login` is unavailable (e.g., network timeout), transfer credentials
from a machine where Claude Code is already authenticated with a refresh token.

1. **On your Mac** — extract credentials from Keychain:

   ```bash
   security find-generic-password -s "Claude Code-credentials" -w > /tmp/creds.json
   ```

2. **Verify the local credentials have a refresh token** before transferring:

   ```bash
   python3 -c "
   import json; d = json.load(open('/tmp/creds.json'))
   print('has refresh_token:', bool(d.get('claudeAiOauth', {}).get('refreshToken')))
   "
   ```

3. **Transfer to VPS**:

   ```bash
   scp /tmp/creds.json root@162.55.60.42:/home/lobster/.claude/.credentials.json
   ```

4. **Fix ownership and permissions**:

   ```bash
   ssh root@162.55.60.42 "chown lobster:lobster /home/lobster/.claude/.credentials.json && chmod 600 /home/lobster/.claude/.credentials.json"
   ```

5. **Clean up locally**:

   ```bash
   rm /tmp/creds.json
   ```

6. **Restart**:

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
  has_refresh = bool(d["claudeAiOauth"].get("refreshToken"))
  print("Expires:", datetime.datetime.fromtimestamp(exp))
  print("Status:", "EXPIRED" if exp < datetime.datetime.now().timestamp() else "VALID")
  print("Has refresh token:", has_refresh)
'
```

---

## Post-auth checklist

1. **Verify credentials exist and have a refresh token**:

   ```bash
   cat /home/lobster/.claude/.credentials.json | python3 -c '
     import json, sys
     d = json.load(sys.stdin)
     oauth = d.get("claudeAiOauth", {})
     has_refresh = bool(oauth.get("refreshToken"))
     has_access = bool(oauth.get("accessToken"))
     print("access_token:", "OK" if has_access else "MISSING")
     print("refresh_token:", "OK" if has_refresh else "MISSING — re-run claude auth login")
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
