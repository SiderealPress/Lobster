## Slack Connector — Onboarding

When the user first activates the slack-connector skill, guide them through setup.

### Path Selection

Ask the user which account mode they want:

- **Bot account (default)** — Lobster connects as a Slack App. Standard setup, no seat consumed.
- **Person account** — Lobster connects as a real Slack user. Sees all messages in joined channels, consumes a paid seat. Use when bot accounts aren't accepted or Lobster needs to appear as a team member.

### Bot Account Path

1. **Token check** — Verify `LOBSTER_SLACK_BOT_TOKEN` and `LOBSTER_SLACK_APP_TOKEN` are set in `~/lobster-config/config.env`. If missing, explain how to create a Slack App with Socket Mode enabled and obtain both tokens.

2. **Channel selection** — Ask which channels to monitor. Create the initial `channels.yaml` from their response. Default to logging all public channels the bot is invited to.

3. **Ingress preferences** — Confirm default logging settings: messages, reactions, files, and edits are logged; deletes are not. Explain they can change these later via skill preferences.

4. **First run** — Run `install.sh` to install dependencies and start the ingress worker. Confirm the Socket Mode connection is established and messages are being logged.

5. **Verification** — After a few minutes, run `/slack-status` to show the user that messages are flowing and logs are accumulating.

### Person Account Path

1. **Account creation** — Guide the user to create a dedicated Slack user account (e.g., lobster@yourcompany.com).

2. **Token acquisition** — Two options:
   - **Option A (recommended):** Create a Slack App with user scopes, OAuth as the Lobster user.
   - **Option B (dev only):** Legacy token (warn it's deprecated by Slack).

3. **Required user scopes:** channels:history, channels:read, groups:history, groups:read, im:history, im:read, mpim:history, channels:write, users:read, reactions:read, files:read

4. **Token validation** — Validate via `auth.test` and print connected user's name.

5. **Install** — Run `SLACK_ACCOUNT_TYPE=person bash install.sh`. The installer writes `LOBSTER_SLACK_USER_TOKEN` and `LOBSTER_SLACK_ACCOUNT_TYPE=person` to config.env.

6. **Behavior notes** — Explain that in person mode:
   - Lobster logs ALL messages in joined channels (not just @mentions)
   - Lobster does NOT respond to its own messages
   - The Lobster user appears in channel member lists
   - To switch back: `/skill set slack-connector account_type bot`
