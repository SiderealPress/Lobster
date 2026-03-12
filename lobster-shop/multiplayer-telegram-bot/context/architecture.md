# Multiplayer Telegram Bot — Architecture

## Overview

This skill adds group chat support to Lobster by:

1. **Message detection** — The Lobster bot webhook/polling detects `chat.type` of
   `group` or `supergroup` and routes those messages to a dedicated queue.

2. **Gating layer** — Before any message reaches Lobster's main processing loop,
   it checks `~/messages/config/group-whitelist.json`. If the group is not
   enabled or the sender's `user_id` is not in the allowed list, the message
   is silently dropped.

3. **Separate inbox** — Group messages are stored with `source=lobster-group`,
   keeping them isolated from personal DMs (`source=default` or `source=telegram`).

4. **User registration** — Since Telegram only reveals `user_id` when a user
   messages the bot, the first message from an unknown user in an enabled group
   triggers a DM inviting them to register.

## File Locations

| File | Purpose |
|------|---------|
| `~/messages/config/group-whitelist.json` | Per-group enable/disable + allowed user IDs |
| `~/messages/inbox/` | Main inbox (DMs land here with `source=telegram`) |
| `~/messages/inbox/` with `source=lobster-group` | Group messages land here (same dir, different source tag) |

## Message Flow

```
Telegram Group Message
        |
        v
  Bot webhook/polling
        |
        v
  Is chat.type group/supergroup?
  |-- No  --> normal DM routing (source=telegram)
  `-- Yes --> check group-whitelist.json [router.py]
                |
                |-- Group not enabled --> drop silently [gating.py: DROP_SILENT]
                `-- Group enabled
                      |
                      |-- user_id in allowed list
                      |     `-- Write to inbox (source=lobster-group) [router.py]
                      `-- user_id NOT in allowed list
                            `-- Send DM: "register to use Lobster in this group"
                                [registration.py: SEND_REGISTRATION_DM]
```

## Module Design

All modules follow a **pure-core / I/O-at-edges** pattern. The gating and routing
logic contains no side effects — it returns decisions that the caller executes.
This makes every module independently testable without mocks for the core logic.

### `router.py`

Pure functions for:
- `is_group_message(chat_type)` — detects group/supergroup chat types
- `get_source_for_chat(chat_type)` — maps chat type to inbox source tag
- `build_inbox_message(...)` — constructs a well-formed inbox message dict
- `classify_message(...)` — returns routing metadata (is_group, source, requires_gating)

No I/O. The caller is responsible for writing the returned dict to the inbox.

### `whitelist.py`

Functions for loading and mutating `group-whitelist.json`:
- `load_whitelist(path)` — reads JSON; returns empty store if missing/malformed
- `save_whitelist(store, path)` — atomic write via rename (no partial writes)
- `is_group_enabled(chat_id, store)` — pure query
- `is_user_allowed(user_id, chat_id, store)` — pure query
- `enable_group(chat_id, name, store)` — returns new store (immutable update)
- `add_allowed_user(user_id, chat_id, store)` — returns new store
- `remove_allowed_user(user_id, chat_id, store)` — returns new store

### `gating.py`

Pure gating logic with no I/O:
- `gate_message(chat_id, user_id, store)` — returns `GatingResult` with one of:
  - `GatingAction.ALLOW` — write message to inbox
  - `GatingAction.DROP_SILENT` — discard (group not enabled)
  - `GatingAction.SEND_REGISTRATION_DM` — group enabled, user unknown
- `gate_messages(messages, store)` — batch variant
- Convenience predicates: `should_allow`, `should_drop`, `should_register`

### `registration.py`

Handles the registration DM flow with dependency injection for the send operation:
- `build_registration_dm(user_id, group_chat_id, ...)` — pure, returns `RegistrationDM`
- `send_registration_dm(dm, send_fn)` — calls the injected `send_fn(user_id, text)`
- `handle_registration_flow(user_id, group_chat_id, send_fn, ...)` — combines both

The `send_fn` parameter accepts any callable `(user_id: int, text: str) -> bool`.
In production, pass the actual Telegram bot send method. In tests, pass a mock.

### `commands.py`

Handles the three bot commands with I/O isolated to load/save calls:
- `handle_enable_group_bot(text, whitelist_path)` — parses and applies `/enable-group-bot`
- `handle_whitelist(text, whitelist_path)` — parses and applies `/whitelist`
- `handle_unwhitelist(text, whitelist_path)` — parses and applies `/unwhitelist`
- All return a `CommandResult(success, reply, updated_store)` NamedTuple

## Integration Points

- **Lobster inbox server** (`~/lobster/src/mcp/inbox_server.py`) —
  needs a hook or plugin point to call the gating logic before writing messages
- **Bot integration** (`~/lobster/src/bot/`) — where Telegram webhook
  handling lives; this is where group detection is added
- **Whitelist module** (`src/whitelist.py`) — pure functions for loading/checking
  the whitelist config

## Testing

The test suite is in `tests/` and is structured as:

| File | Coverage |
|------|---------|
| `test_router.py` | Unit tests for all router.py functions |
| `test_whitelist.py` | Unit tests for whitelist load/save/query/mutation |
| `test_gating.py` | Unit tests for gating decision logic |
| `test_registration.py` | Unit tests for registration DM building and sending |
| `test_commands.py` | Unit tests for command parsing and handlers |
| `test_integration.py` | End-to-end flow covering all 6 scenarios |

### Integration Test Scenarios (`test_integration.py`)

1. **Empty whitelist** — all group messages dropped
2. **Enable group** — `/enable-group-bot` persists config; group enabled but no users triggers registration
3. **Add user** — `/whitelist` adds user; persists across reload from disk
4. **Allowed user message** — gates as ALLOW; inbox message has `source=lobster-group`
5. **Disallowed user** — gates as SEND_REGISTRATION_DM; mock send_fn called with correct user_id
6. **Unknown group** — silently dropped; no DM, no inbox write

### Running Tests

```bash
cd lobster-skills/multiplayer-telegram-bot
pytest
```

No external dependencies, API keys, or network access required. The whitelist
I/O tests use pytest's `tmp_path` fixture for isolated temporary files.

## Whitelist JSON Schema

```json
{
  "groups": {
    "<chat_id>": {
      "name": "Group Name",
      "enabled": true,
      "allowed_user_ids": [123456789, 987654321]
    }
  }
}
```

- `chat_id` is always a string key (Telegram group IDs are negative integers,
  e.g. `"-1001234567890"`; JSON object keys are always strings)
- `enabled` can be set to `false` to temporarily disable a group without
  removing its user list
- `allowed_user_ids` is a list of positive integers (Telegram user IDs)
