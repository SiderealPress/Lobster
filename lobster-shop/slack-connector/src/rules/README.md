# Trigger Engine Rules

Drop TOML rule files in this directory to configure automated triggers.
The trigger engine hot-reloads rules on any file change — no restart required.

## Rule format

Each rule file contains three sections: `[rule]`, `[trigger]`, and `[action]`.

```toml
[rule]
name = "unique-rule-name"          # Required, must be unique across all files
description = "What this rule does" # Optional
enabled = true                      # Set to false to disable without deleting

[trigger]
event = "message"                   # message | reaction_added | app_mention | file_shared | slash_command
channels = ["C03GHI789"]           # Empty list matches ALL channels
users = []                          # Empty list matches ALL users
keywords = ["deploy", "outage"]     # Matched case-insensitively against message text
keyword_mode = "any"                # "any" = at least one keyword | "all" = every keyword
# Optional trigger fields:
# emoji = "thumbsup"               # For reaction_added events
# command = "/deploy"               # For slash_command events
# file_type = "pdf"                 # For file_shared events
# regex = "JIRA-\\d+"              # Regex pattern matched against message text

[action]
type = "lobster_task"               # lobster_task | send_reply | telegram_notify | webhook | shell
# Action-specific fields vary by type (see examples/)
```

## Template variables

Use `{variable_name}` in action fields. Available variables:

| Variable | Description |
|----------|-------------|
| `{message_text}` | The message text |
| `{channel_id}` | Slack channel ID |
| `{channel_name}` | Channel name |
| `{user_id}` | Slack user ID |
| `{username}` | Username |
| `{ts}` | Message timestamp |
| `{thread_ts}` | Thread timestamp |
| `{emoji}` | Reaction emoji name |
| `{original_message_text}` | Original message (for reactions) |
| `{command_text}` | Slash command text |
| `{file_name}` | Shared file name |
| `{date}` | Current date (YYYY-MM-DD) |

## File placement

- Place active rules directly in this directory (e.g., `my-rule.toml`)
- Files in `examples/` are not auto-loaded
- Subdirectories (except `examples/`) are scanned recursively
