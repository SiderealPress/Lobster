## EOD Skill — Reference

### What it is

The `/eod` skill generates a structured end-of-day summary for the instance owner.
It pulls 18 hours of real activity data from GitHub and the Lobster inbox, formats
it into a clean bulleted list, and optionally overlays commentary from a voice note.

---

### Configuration

The EOD skill reads owner identity from `~/lobster-config/owner.toml`:

| Field | Section | Used for |
|-------|---------|----------|
| `telegram_chat_id` | `[owner]` | Who receives the EOD summary |
| `github_username` | `[owner]` | Whose GitHub activity to pull |

If `github_username` is not set in `owner.toml`, the skill falls back to
`gh api user --jq '.login'` to determine the authenticated GitHub user.

---

### Activity sources

| Category | Source | How fetched |
|----------|--------|-------------|
| Commits | GitHub API | `gh api search/commits?q=author:{github_username}+committer-date:>{since}` |
| Pull Requests | GitHub API | `gh api search/issues?q=author:{github_username}+type:pr+updated:>{since}` |
| Issues | GitHub API | `gh api search/issues?q=author:{github_username}+type:issue+updated:>{since}` |
| Issue Comments | GitHub API | `gh api search/issues?q=commenter:{github_username}+type:issue+updated:>{since}` |
| Inbox messages | `~/messages/processed/*.json` | Filter by timestamp >= now - 18h |

---

### Output format

```
*EOD — Thursday, March 12*

*Activity — last 18h*

*Commits*
• [owner/repo abc1234](https://github.com/...) — feat: add EOD skill

*Pull Requests*
• [owner/repo#91](https://github.com/...) — Add EOD skill [open]

*Issues*
• [owner/repo#17](https://github.com/...) — Some issue [open]

*Lobster Activity*
• Hey, are you there?
• Work on issue #42

*Voice note*
Today was productive — finished the EOD refactor, reviewed the PR, and
pushed the fix for the transcription timeout. Tomorrow I want to focus
on the login flow.
```

---

### Skill module location

`~/lobster-workspace/projects/lobster-eod-skill/eod_skill.py`

Key functions:
- `handle_eod_command(chat_id)` — activate EOD mode
- `is_eod_pending(chat_id)` — check if EOD mode active
- `clear_eod_mode(chat_id)` — deactivate after processing
- `gather_github_activity(window_hours=18)` — pull GitHub data via gh CLI
- `gather_inbox_activity(window_hours=18)` — scan ~/messages/processed/
- `build_activity_summary(github_activity, inbox_messages)` — format structured list
- `process_eod_voice_note(chat_id, message_id, transcription)` — full EOD workflow

---

### State file

State stored at `~/messages/config/eod-state.json`, keyed by the owner's
Telegram chat_id (read from `~/lobster-config/owner.toml`):

```json
{
  "<owner_telegram_chat_id>": {
    "eod_pending": true,
    "activated_at": "2026-03-12T22:00:00Z"
  }
}
```

---

### Design decisions

**Why activity comes first, voice note second:**
The voice note adds commentary and color to the structured activity list — it
is not the primary source of content. This ensures the EOD summary is
comprehensive even if the voice note is brief or not sent at all.

**Why 18 hours:**
Covers a full working day plus some buffer for late-night commits, without
pulling too much history. Configurable via `_ACTIVITY_WINDOW_HOURS` in the
skill module.

**Why gh CLI instead of GitHub API token:**
`gh` is already authenticated on the Lobster server and handles auth
automatically. No separate token management needed.
