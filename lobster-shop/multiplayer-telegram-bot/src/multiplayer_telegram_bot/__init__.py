"""
multiplayer_telegram_bot — Lobster skill for group Telegram messaging.

Modules:
  whitelist   — Load/check group-whitelist.json (pure functions)
  router      — Detect group messages and assign source tags (pure functions)
  gating      — Gate messages: check group enabled + user whitelisted
  registration — Registration flow for unknown users in enabled groups
  commands    — /enable-group-bot and /whitelist command handlers
"""
