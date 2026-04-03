# Remote / Headless Authentication

Lobster authenticates Claude Code via the `CLAUDE_CODE_OAUTH_TOKEN` environment
variable, set in `~/lobster-config/config.env`. The token is loaded by
`claude-persistent.sh` at startup and passed directly to Claude Code.

`claude auth status --output-format json` is the single source of truth for
auth state. The health check (`health-check-v3.sh`) and token refresh cron
(`token-refresh.sh`) both use this command — no credentials file path checks.

---

## Check auth status

```bash
# Canonical check — works regardless of how the token was provisioned:
claude auth status --output-format json
```

Expected output when healthy:
```json
{"loggedIn": true, "authMethod": "oauth_token", ...}
```

---

## Authenticate (or re-authenticate)

When `CLAUDE_CODE_OAUTH_TOKEN` expires, obtain a new token and update config.env:

1. **Get a new token** — run `claude setup-token` (or `claude auth login`) as the lobster user:

   ```bash
   sudo -u lobster bash -c '
     export HOME=/home/lobster
     export PATH=/home/lobster/.local/bin:/usr/local/bin:/usr/bin:/bin
     claude setup-token
   '
   ```

   This displays a URL — open it in any browser (laptop, phone, etc.),
   authorize, then paste the token back when prompted.

2. **Update config.env**:

   ```bash
   # Edit ~/lobster-config/config.env and set:
   CLAUDE_CODE_OAUTH_TOKEN=<new-token>
   ```

3. **Restart the service**:

   ```bash
   systemctl restart lobster-claude
   ```

4. **Verify**:

   ```bash
   claude auth status --output-format json
   ```

---

## Transfer credentials from another machine

If `claude setup-token` is unavailable, copy a working token from another machine:

1. **On your Mac** — get the current OAuth token:

   ```bash
   security find-generic-password -s "Claude Code-credentials" -w | \
     python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('claudeAiOauth',{}).get('accessToken','') or d.get('oauthAccount',{}).get('accessToken',''))"
   ```

2. **Update config.env on the VPS**:

   ```bash
   ssh root@<vps-ip> "sed -i 's|^CLAUDE_CODE_OAUTH_TOKEN=.*|CLAUDE_CODE_OAUTH_TOKEN=<token>|' /home/lobster/lobster-config/config.env"
   ```

3. **Restart**:

   ```bash
   ssh root@<vps-ip> "systemctl restart lobster-claude"
   ```

---

## ANTHROPIC_API_KEY conflict

If `ANTHROPIC_API_KEY` is set in the environment, Claude Code uses it **instead of**
OAuth — even when `CLAUDE_CODE_OAUTH_TOKEN` is set. The `config.env` file sets this
variable for other Lobster services (MCP server, scheduled tasks).

`claude-persistent.sh` unsets `ANTHROPIC_API_KEY` before launching Claude so OAuth
is used. If you see "API usage limits" or "Invalid API key" errors in
`claude-session.log` but `claude auth status` reports logged in, check whether the env
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

---

## Post-auth checklist

1. **Verify auth is working**:

   ```bash
   claude auth status --output-format json
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
