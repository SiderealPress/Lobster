# Slack Integration Setup

This guide walks you through setting up Slack as a message source for Hyperion.

## Overview

The Slack router allows you to interact with Hyperion through Slack, similar to the Telegram integration. It supports:

- Direct messages to the bot
- @mentions in channels where the bot is invited
- Thread replies
- File/image sharing
- Optional channel and user restrictions

## Prerequisites

- A Slack workspace where you have permission to install apps
- Hyperion already installed and running

## Step 1: Create a Slack App

1. Go to [https://api.slack.com/apps](https://api.slack.com/apps)
2. Click **Create New App**
3. Choose **From scratch**
4. Enter an app name (e.g., "Hyperion") and select your workspace
5. Click **Create App**

## Step 2: Enable Socket Mode

Socket Mode allows your bot to receive events without exposing a public URL.

1. In your app settings, go to **Socket Mode** (left sidebar)
2. Toggle **Enable Socket Mode** to On
3. You'll be prompted to create an App-Level Token:
   - Name it something like "hyperion-socket"
   - Add the `connections:write` scope
   - Click **Generate**
4. **Copy the token** (starts with `xapp-`) - you'll need this later

## Step 3: Configure OAuth Scopes

1. Go to **OAuth & Permissions** in the left sidebar
2. Scroll down to **Scopes** > **Bot Token Scopes**
3. Add the following scopes:

| Scope | Purpose |
|-------|---------|
| `app_mentions:read` | Receive @mentions |
| `channels:history` | Read messages in public channels |
| `channels:read` | Get channel information |
| `chat:write` | Send messages |
| `files:read` | Download shared files |
| `groups:history` | Read messages in private channels |
| `groups:read` | Get private channel info |
| `im:history` | Read direct messages |
| `im:read` | Get DM channel info |
| `reactions:write` | Add emoji reactions (for acknowledgments) |
| `users:read` | Get user information |

## Step 4: Subscribe to Events

1. Go to **Event Subscriptions** in the left sidebar
2. Toggle **Enable Events** to On
3. Under **Subscribe to bot events**, add:
   - `app_mention` - When someone @mentions the bot
   - `message.channels` - Messages in public channels
   - `message.groups` - Messages in private channels
   - `message.im` - Direct messages
4. Click **Save Changes**

## Step 5: Install the App

1. Go to **Install App** in the left sidebar
2. Click **Install to Workspace**
3. Review the permissions and click **Allow**
4. **Copy the Bot User OAuth Token** (starts with `xoxb-`)

## Step 6: Configure Hyperion

Add the following to your `config/config.env` file:

```bash
# Slack Integration
HYPERION_SLACK_BOT_TOKEN=xoxb-your-bot-token-here
HYPERION_SLACK_APP_TOKEN=xapp-your-app-token-here

# Optional: Restrict to specific channels (comma-separated channel IDs)
# HYPERION_SLACK_ALLOWED_CHANNELS=C01ABC123,C02DEF456

# Optional: Restrict to specific users (comma-separated user IDs)
# HYPERION_SLACK_ALLOWED_USERS=U01ABC123,U02DEF456
```

## Step 7: Install Dependencies

The Slack router requires the `slack-bolt` package. If not already installed:

```bash
cd ~/hyperion
source .venv/bin/activate
pip install slack-bolt
```

## Step 8: Enable the Service

Create and enable the systemd service:

```bash
# Create service file from template
sudo cp /home/$USER/hyperion/services/hyperion-slack-router.service.template \
        /etc/systemd/system/hyperion-slack-router.service

# Edit the service file to replace placeholders
sudo sed -i "s|{{USER}}|$USER|g" /etc/systemd/system/hyperion-slack-router.service
sudo sed -i "s|{{GROUP}}|$USER|g" /etc/systemd/system/hyperion-slack-router.service
sudo sed -i "s|{{HOME}}|$HOME|g" /etc/systemd/system/hyperion-slack-router.service
sudo sed -i "s|{{INSTALL_DIR}}|$HOME/hyperion|g" /etc/systemd/system/hyperion-slack-router.service
sudo sed -i "s|{{CONFIG_DIR}}|$HOME/hyperion-config|g" /etc/systemd/system/hyperion-slack-router.service

# Reload systemd
sudo systemctl daemon-reload

# Enable and start the service
sudo systemctl enable hyperion-slack-router
sudo systemctl start hyperion-slack-router

# Check status
sudo systemctl status hyperion-slack-router
```

## Step 9: Invite the Bot

1. In Slack, go to the channel where you want to use Hyperion
2. Type `/invite @YourBotName` or click the channel name and add the bot
3. The bot will now respond to @mentions in that channel
4. You can also DM the bot directly

## Usage

### Direct Messages

Simply send a message to the bot - no @mention needed:

```
What's the weather like today?
```

### Channel Messages

@mention the bot in any channel where it's invited:

```
@Hyperion what tasks do I have pending?
```

### Thread Replies

If you start a conversation, responses will come in the same thread to keep channels tidy.

## Troubleshooting

### Bot not responding

1. Check service status: `sudo systemctl status hyperion-slack-router`
2. Check logs: `sudo journalctl -u hyperion-slack-router -f`
3. Verify tokens are correct in config.env
4. Ensure the bot is invited to the channel

### "not_in_channel" errors

The bot must be invited to a channel before it can read or post messages there.

### Socket Mode connection issues

- Ensure the App-Level Token has `connections:write` scope
- Check that Socket Mode is enabled in your app settings
- Verify the token starts with `xapp-`

### Permission errors

- Review the OAuth scopes in your app settings
- Reinstall the app to your workspace after adding new scopes

## Finding IDs

### Channel ID

1. Right-click on a channel name
2. Select "View channel details"
3. Scroll down - the Channel ID is at the bottom (starts with C)

### User ID

1. Click on a user's name
2. Click "View full profile"
3. Click the "..." menu
4. Select "Copy member ID" (starts with U)

## Security Considerations

- Store tokens securely - never commit them to version control
- Use `HYPERION_SLACK_ALLOWED_CHANNELS` and `HYPERION_SLACK_ALLOWED_USERS` to restrict access
- Review the bot's permissions periodically
- Consider creating a dedicated private channel for Hyperion interactions
