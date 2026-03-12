# Multiplayer Telegram Bot — Behavior

This skill enables Lobster to participate in Telegram group chats with
per-group access control. Group messages are routed to a separate inbox
source (`lobster-group`) to keep them isolated from direct messages.

## When to Use

Activate this skill when the owner wants to:
- Add Lobster to a Telegram group chat
- Control who in a group can interact with Lobster
- See messages from a group separately from personal DMs
- Register group members for bot access

## Commands

### /enable-group-bot GROUP_ID
Enable group mode for a specific Telegram chat ID. This writes an entry to
`~/messages/config/group-whitelist.json` allowing that group to send messages.

Usage: `/enable-group-bot -1001234567890`

### /whitelist USER_ID GROUP_ID
Add a Telegram user ID to the whitelist for a specific group.

Usage: `/whitelist 123456789 -1001234567890`

## Group Message Routing

- All messages from `chat.type = "group"` or `"supergroup"` are detected automatically
- Messages land in `source=lobster-group` inbox, NOT the main DM inbox
- Non-whitelisted senders are silently ignored (no reply, no error)
- Whitelisted users get full Lobster access within the group context

## Whitelist Config Format

The whitelist lives at `~/messages/config/group-whitelist.json`:

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

## Behavior Guidelines

1. **Never reply to non-whitelisted users in groups** — silent drop only
2. **Registration flow**: unknown users in enabled groups get a DM: "To use Lobster in this group, please send /register"
3. **Group context**: when responding in groups, be concise — multiple people may be reading
4. **Group vs DM**: keep group conversations in `lobster-group` source, DMs in `default`
