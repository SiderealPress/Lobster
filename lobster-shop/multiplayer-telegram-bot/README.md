# Multiplayer Telegram Bot

**Add Lobster to Telegram groups with per-group access control.**

This skill enables your Lobster assistant to participate in Telegram group chats.
Group messages are routed to a separate inbox (`lobster-group`) so they never
contaminate your personal DMs.

## What It Does

- Detects messages from Telegram groups vs direct messages
- Routes group messages to a dedicated source queue (`lobster-group`)
- Enforces a per-group whitelist — only approved users can interact with Lobster
- Silently ignores messages from unknown or disabled groups (no error messages)
- Sends new users in an enabled group a DM with registration instructions
- Provides `/enable-group-bot`, `/whitelist`, and `/unwhitelist` commands

## Quick Start

### 1. Add your Lobster bot to the Telegram group

In Telegram, open the group, go to **Add Members**, and search for your bot by username (e.g. `@mylobsterbot`). The bot needs at least read access to group messages.

### 2. Get the group's chat ID

Forward any message from the group to [@userinfobot](https://t.me/userinfobot). It will reply with the group's chat ID — a negative integer like `-1001234567890`.

Alternatively, make the bot an admin briefly and send `/id` in the group — some setups will report the chat ID.

### 3. Enable the group

Send this command to your Lobster bot in a private DM:

```
/enable-group-bot -1001234567890 My Group Name
```

The group name is optional but helps with confirmation messages.

### 4. Whitelist users

```
/whitelist 123456789 -1001234567890
```

Where `123456789` is the Telegram user ID of the person to allow. To find a user's ID, ask them to forward a message from themselves to [@userinfobot](https://t.me/userinfobot).

### 5. Start chatting

Whitelisted users can now send messages to Lobster in the group. Non-whitelisted users who message the bot in the group will receive a private DM with registration instructions.

## Commands

| Command | Example | Description |
|---------|---------|-------------|
| `/enable-group-bot GROUP_ID [name]` | `/enable-group-bot -1001234567890 WorkGroup` | Enable Lobster in a Telegram group |
| `/whitelist USER_ID GROUP_ID` | `/whitelist 123456789 -1001234567890` | Allow a user in a group |
| `/unwhitelist USER_ID GROUP_ID` | `/unwhitelist 123456789 -1001234567890` | Remove a user from the allowed list |

## Configuration

The whitelist is stored at `~/messages/config/group-whitelist.json`:

```json
{
  "groups": {
    "-1001234567890": {
      "name": "My Group",
      "enabled": true,
      "allowed_user_ids": [123456789, 987654321]
    }
  }
}
```

You can edit this file directly to make bulk changes (e.g., pre-populate a user list before anyone sends their first message).

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `LOBSTER_MESSAGES` | `~/messages` | Base directory for all Lobster message files |

## Preferences

Stored in `preferences/` and merged into Lobster's system context when the skill is active.

| Key | Default | Description |
|-----|---------|-------------|
| `registration_message` | "Hi! To use Lobster in this group, please send /register to this bot directly." | Message sent to unregistered users |
| `silent_drop_unknown_groups` | `true` | Silently ignore groups not in whitelist |
| `group_source_name` | `"lobster-group"` | Source tag for group messages in inbox |
| `enable_registration_flow` | `true` | Send DMs to unregistered users |

## Message Flow

```
Telegram Group Message
        |
        v
  Is chat.type group/supergroup?
  |-- No  --> normal DM routing (source=telegram)
  `-- Yes --> check group-whitelist.json
                |
                |-- Group not enabled --> DROP (silent)
                `-- Group enabled
                      |
                      |-- user_id in allowed list
                      |     `-- Write to inbox (source=lobster-group)
                      `-- user_id NOT in allowed list
                            `-- Send registration DM to user
```

## Architecture

See `context/architecture.md` for full technical details.

## Module Reference

| Module | Purpose |
|--------|---------|
| `router.py` | Detect group/supergroup messages; assign `source=lobster-group`; build inbox message dicts |
| `whitelist.py` | Load/save `group-whitelist.json`; pure query and mutation functions |
| `gating.py` | Pure gating logic: ALLOW, DROP_SILENT, or SEND_REGISTRATION_DM |
| `registration.py` | Build and send registration DMs to unknown users |
| `commands.py` | Handle `/enable-group-bot`, `/whitelist`, `/unwhitelist` commands |

## Testing

```bash
cd lobster-skills/multiplayer-telegram-bot
pytest
```

The test suite covers:
- Unit tests for each module (pure functions, no I/O)
- Integration tests for the full message flow (`tests/test_integration.py`)

The integration test exercises all 6 scenarios:
1. Empty whitelist rejects all messages
2. `/enable-group-bot` persists the group config
3. `/whitelist` adds a user to the allowed list
4. Allowed user message routes to `lobster-group` inbox
5. Disallowed user triggers registration DM (mock send_fn)
6. Unknown group messages are silently dropped

## Installation

```bash
bash lobster-skills/multiplayer-telegram-bot/install.sh
```

Or, if installing via the Lobster shop:

```bash
bash lobster-shop/multiplayer-telegram-bot/install.sh
```

## Development Status

| Phase | Description | Status |
|-------|-------------|--------|
| 1 | Skill skeleton + README | Complete |
| 2 | Message ingestion for groups | Complete |
| 3 | Whitelist config + gating | Complete |
| 4 | User ID registration flow | Complete |
| 5 | Skill activation command | Complete |
| 6 | Integration test + docs | Complete |
