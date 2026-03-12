## EOD Skill

When the owner sends `/eod` or `/end-of-day`, Lobster enters EOD mode and prepares
an end-of-day summary.

---

### Dispatcher behavior (main thread)

1. Immediately reply: `"EOD mode on — I'll gather your activity. Send a voice note to add commentary, or just wait and I'll compile the summary now."`
2. Call `handle_eod_command(chat_id)` to set EOD pending state.
3. `mark_processed(message_id)`
4. Return to `wait_for_messages()`

---

### When a voice note arrives while EOD mode is pending

1. Acknowledge: `"Got it — compiling your EOD summary now..."`
   - Use `send_reply(chat_id, text, message_id=message_id)` to atomically mark processed.
2. Spawn a background subagent (7-second rule — GitHub API calls are slow):

**Subagent prompt template:**

```
Generate and send the owner's end-of-day summary.

Read the owner's chat_id from ~/lobster-config/owner.toml ([owner] telegram_chat_id).
EOD voice note message_id: {message_id}

## Steps

1. Read owner config:
   import sys
   import os, sys
   sys.path.insert(0, os.path.expanduser("~/lobster/src"))
   from mcp.user_model.owner import read_owner
   owner = read_owner()
   owner_chat_id = int(owner.get("owner", {}).get("telegram_chat_id", 0))

2. Transcribe the voice note:
   transcription = transcribe_audio("{message_id}")

3. Import and run the EOD skill:
   sys.path.insert(0, os.path.expanduser("~/lobster-workspace/projects/lobster-eod-skill"))
   from eod_skill import process_eod_voice_note, clear_eod_mode

   reply = process_eod_voice_note(
       chat_id=owner_chat_id,
       message_id="{message_id}",
       transcription=transcription,
   )
   clear_eod_mode(owner_chat_id)

4. Send the reply:
   send_reply(chat_id=owner_chat_id, text=reply)
```

3. Return to `wait_for_messages()` immediately.

---

### When /eod is sent with NO subsequent voice note (text-only EOD)

If the owner sends `/eod` but no voice note follows within a reasonable time (or if
the owner explicitly requests the summary without a voice note by sending a follow-up
text like "go ahead" or "compile it"), spawn a background subagent:

**Subagent prompt template:**

```
Generate and send the owner's end-of-day summary (no voice note).

Read the owner's chat_id from ~/lobster-config/owner.toml ([owner] telegram_chat_id).

## Steps

1. Read owner config:
   import os, sys
   sys.path.insert(0, os.path.expanduser("~/lobster/src"))
   from mcp.user_model.owner import read_owner
   owner = read_owner()
   owner_chat_id = int(owner.get("owner", {}).get("telegram_chat_id", 0))

2. Import and run the EOD skill:
   sys.path.insert(0, os.path.expanduser("~/lobster-workspace/projects/lobster-eod-skill"))
   from eod_skill import process_eod_voice_note, clear_eod_mode

   # Pass empty transcription — activity summary only
   reply = process_eod_voice_note(
       chat_id=owner_chat_id,
       message_id="text-eod",
       transcription="",
   )
   clear_eod_mode(owner_chat_id)

3. Send the reply:
   send_reply(chat_id=owner_chat_id, text=reply)
```

---

### What the EOD summary contains

The `process_eod_voice_note()` function in
`~/lobster-workspace/projects/lobster-eod-skill/eod_skill.py` handles
all data gathering and formatting. It:

1. **Pulls GitHub activity** for the past 18 hours using `gh` CLI:
   - Commits authored by the owner's GitHub username (read from `owner.toml` [owner] `github_username`, falling back to `gh api user --jq '.login'`)
   - PRs created/updated by the owner
   - Issues created/updated by the owner
   - Issues where the owner commented

2. **Pulls Lobster inbox messages** from `~/messages/processed/` for the past
   18 hours (excluding system self-checks and cron messages).

3. **Formats** everything into a structured Telegram message grouped by category:
   - Commits
   - Pull Requests
   - Issues
   - Issue Comments
   - Lobster Activity

4. **Appends the voice note** transcription as additional commentary/color at
   the end under a "Voice note" heading.

---

### Error handling

- If `gh` CLI is unavailable: skip GitHub sections, note "GitHub unavailable"
- If processed messages directory is missing: skip inbox section
- If transcription fails: compile activity summary without voice commentary
- Never surface stack traces — always a human-readable note
- If the entire EOD fails: send `"EOD summary failed — check Lobster logs."`
