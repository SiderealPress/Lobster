# Lobster Slack Integration: Setup Guide

This guide gets the Lobster Slack integration (the "Wallace" bot) running from scratch. Follow it in order — each step produces a value you need in the next step.

**Time required:** ~15 minutes

---

## Architecture overview

Lobster uses **Socket Mode** for inbound message delivery. Three tokens are required:

| Token prefix | Name | Used for |
|---|---|---|
| `xoxb-` | Bot token | Socket Mode event receiving; reading channel/user info |
| `xapp-` | App-level token | Establishing the WebSocket connection (Socket Mode) |
| `xoxp-` | User token | Posting outbound replies (messages appear as the user, not the bot) |

- **Inbound:** Slack pushes events to Lobster over a persistent WebSocket (Socket Mode). No public URL or webhook required.
- **Outbound:** `chat.postMessage` is called with the user token (`xoxp-`) so replies appear to come from the real user account rather than the bot.

---

## Prerequisites

- Access to a Slack workspace where you can install apps
- SSH access to the Lobster server
- The Lobster config file at `~/lobster-config/config.env`

---

## Step 1: Create the app from the manifest

1. Go to [api.slack.com/apps](https://api.slack.com/apps) and click **Create New App**.
2. Choose **From a manifest**.
3. Select the target workspace and click **Next**.
4. In the manifest editor, select the **JSON** tab.
5. Delete any pre-filled content and paste the entire contents of `docs/slack-app-manifest.json` from this repository.
6. Click **Next**, review the summary (you should see Socket Mode enabled and the bot scopes listed), then click **Create**.

You are now on the app's configuration page.

---

## Step 2: Generate the app-level token (xapp-)

The app-level token is what Socket Mode uses to open the WebSocket connection.

1. In the left sidebar, click **Basic Information**.
2. Scroll down to **App-Level Tokens** and click **Generate Token and Scopes**.
3. Give the token a name (e.g. `lobster-socket`) and add the scope `connections:write`.
4. Click **Generate**.
5. Copy the token — it starts with `xapp-`. This is `LOBSTER_SLACK_APP_TOKEN`.

---

## Step 3: Install the app to the workspace (bot token)

1. In the left sidebar, click **OAuth & Permissions**.
2. Click **Install to Workspace** (or **Reinstall to Workspace** if updating).
3. Click **Allow**.
4. You are returned to the OAuth & Permissions page. The **Bot User OAuth Token** (starts with `xoxb-`) is shown. Copy it. This is `LOBSTER_SLACK_BOT_TOKEN`.

---

## Step 4: Get the user token (xoxp-)

The user token makes outbound replies appear as a real user account (e.g. Wallace) rather than the bot.

1. Still on the OAuth & Permissions page, scroll down to **User OAuth Token**.
2. If one is not shown, the app needs to be installed by the specific user whose identity you want Lobster to use. Have that user visit the app's **Shareable Install Link** (found under **Manage Distribution**) and authorise.
3. Copy the User OAuth Token — it starts with `xoxp-`. This is `LOBSTER_SLACK_USER_TOKEN`.

> **Note:** The user token must have the `chat:write` scope. This is included in the manifest's `user` scopes section.

---

## Step 5: Write tokens to config

SSH into the Lobster server and open `~/lobster-config/config.env` in your editor.

Add or update these lines:

```
LOBSTER_SLACK_BOT_TOKEN=xoxb-...
LOBSTER_SLACK_APP_TOKEN=xapp-...
LOBSTER_SLACK_USER_TOKEN=xoxp-...
```

Replace each `...` with the actual token value you copied above.

---

## Step 6: Configure channel remap (optional)

If the bot DM channel ID differs from the user DM channel ID (common when the bot and user are different Slack identities), use the channel remap to redirect:

```
LOBSTER_SLACK_CHANNEL_REMAP=<bot-dm-channel-id>:<user-dm-channel-id>
```

To find channel IDs, open the DM in Slack and look at the URL:
`https://app.slack.com/client/TXXXXXXXX/DXXXXXXXXX` — the `D...` segment is the channel ID.

Multiple remaps can be comma-separated:

```
LOBSTER_SLACK_CHANNEL_REMAP=D111:D222,D333:D444
```

---

## Step 7: Configure allowed channels / users (optional)

By default, Lobster accepts messages from all channels and all users. To restrict access:

```
# Comma-separated Slack channel IDs
LOBSTER_SLACK_ALLOWED_CHANNELS=C0123456,C0789012

# Comma-separated Slack user IDs
LOBSTER_SLACK_ALLOWED_USERS=U0123456,U0789012
```

If either list is set, a message must match at least one entry to be processed.

---

## Step 8: Restart the Slack router and verify

```bash
sudo systemctl restart lobster-slack-router
sudo systemctl status lobster-slack-router
```

Check the log for successful startup:

```bash
tail -f ~/lobster-workspace/logs/slack-router.log
```

You should see lines like:

```
Outbound messages will use user token (xoxp-) — replies appear as user
Socket Mode connected
Lobster Slack Router running.
```

**Test it:** Send a DM to the Wallace bot in Slack. Within a few seconds the message should appear in `~/messages/inbox/` as a JSON file, and Lobster should reply.

---

## Troubleshooting

### Service fails to start with `ValueError: LOBSTER_SLACK_BOT_TOKEN`

The bot token is missing or empty in `config.env`. Ensure the line exists and the value starts with `xoxb-`.

### Service fails to start with `ValueError: LOBSTER_SLACK_APP_TOKEN`

The app-level token is missing or empty in `config.env`. Ensure the line exists and the value starts with `xapp-`. This token is required for Socket Mode.

### Messages are not being received

- Verify Socket Mode is enabled in the app settings at api.slack.com.
- Check the log for "Socket Mode connected" — if not present, the `xapp-` token or `connections:write` scope may be wrong.
- Verify the bot token has the necessary event scopes (`im:history`, `im:read`, etc.).

### Replies fail with `channel_not_found` or `not_in_channel`

The user token (`xoxp-`) may need `chat:write` scope, or the channel remap may be misconfigured. Check `LOBSTER_SLACK_CHANNEL_REMAP` if the bot DM and user DM channel IDs differ.

### Typing indicator causes errors

Some Slack plans do not allow `chat.update`. Disable the typing indicator:

```
LOBSTER_SLACK_TYPING_INDICATOR=false
```
