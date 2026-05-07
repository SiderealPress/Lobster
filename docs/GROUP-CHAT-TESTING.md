# Group Chat Smoke Test Checklist

After pulling main and running `lobster upgrade`, use this checklist to verify
group chat support is working end-to-end.

## Manual prerequisites (do once, before testing)

1. **Disable BotFather privacy mode** for your bot (e.g. @your_lobster_bot):
   - Open BotFather in Telegram
   - Select your bot → Bot Settings → Group Privacy → Turn off
   - Without this step, the bot cannot see messages in groups where it is not
     mentioned by name.

2. **Re-add the bot to the group** (required after privacy mode change):
   - Remove your bot from the test group
   - Re-add it — this triggers the `my_chat_member` join event that registers
     the group

## Post-upgrade smoke test

Run after every `lobster upgrade` that touches group chat code:

### 1. Migration check

```bash
ls ~/messages/config/group-whitelist.json
cat ~/messages/config/group-whitelist.json
```

Expected: file exists and contains at least `{"groups": {...}}`. Your test group
should already be present if previously whitelisted.

### 2. Service restart

```bash
lobster restart
journalctl -u lobster-bot --no-pager -n 20
```

Expected: no import errors, bot starts and connects to Telegram.

### 3. Message from whitelisted user in whitelisted group

Send a plain text message in your test group from a whitelisted user.

Expected:
- Message appears in `~/messages/inbox/` within a few seconds
- File has `"source": "lobster-group"` and the correct `"group_chat_id"`
- **No "Message received. Processing..." ack appears in the group** (suppressed for groups)
- Lobster eventually replies in the group thread

```bash
ls -lt ~/messages/inbox/ | head -5
cat ~/messages/inbox/<newest-file>.json | python3 -m json.tool
```

### 4. Message from non-whitelisted user in whitelisted group

Have a user who is NOT in the `allowed_users` list for that group send a message.

Expected:
- Message does NOT appear in `~/messages/inbox/`
- No reply from Lobster (silent drop)

### 5. Bot added to non-whitelisted group

Add the bot to a group that is not in `group-whitelist.json`.

Expected:
- Bot leaves the group immediately
- No group entry added to `group-whitelist.json`

### 6. Unit test suite

```bash
cd ~/lobster
uv run pytest tests/unit/test_bot/ -v
```

Expected: all tests pass.

## Whitelist management commands (in-chat)

These commands work in DMs with the bot (from an allowed user):

| Command | Effect |
|---|---|
| `/enable_group_bot` | Enable group bot feature flag |
| `/whitelist <group_id>` | Add a group to the whitelist |
| `/unwhitelist <group_id>` | Remove a group from the whitelist |
| `/list_groups` | Show all whitelisted groups |

## Troubleshooting

**Bot not responding in group:** Check privacy mode is off in BotFather and the
bot was re-added after disabling it.

**`group-whitelist.json` missing:** Run `lobster upgrade` — Migration 62 creates
it automatically.

**Messages not reaching inbox:** Confirm the sending user's ID is in the
group's `allowed_users` list inside `group-whitelist.json`.
