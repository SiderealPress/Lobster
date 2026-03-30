## Obsidian KM — Dispatcher Behavior

### URL Detection

When processing any message, check if it contains a URL:

```python
from link_capture import contains_url, extract_urls

if contains_url(message_text):
    urls = extract_urls(message_text)
    # Trigger link capture for each URL
```

### Automatic Link Capture Flow

1. **Detect URL** in incoming message
2. **Acknowledge immediately**: `send_reply(chat_id, "Link saved.", message_id=message_id)`
3. **Delegate capture** to background subagent (7-second rule)

The subagent handles:
- Preference check (`OBSIDIAN_AUTO_CAPTURE_LINKS`)
- Duplicate detection (skip if captured this month)
- Page title fetch via `fetch_page` MCP tool
- Archive.org archival (existing Commonbook)
- Vault note creation
- Brain-dumps issue comment (existing Commonbook)

### Subagent Prompt Template

```
Capture link to Obsidian vault and archive:

URL: {url}
Caption: {caption}
Chat ID: {chat_id}

Steps:
1. Check OBSIDIAN_AUTO_CAPTURE_LINKS preference
2. Check duplicate (skip if URL captured this month)
3. Fetch page title: fetch_page(url="{url}")
4. Archive: curl -s "https://web.archive.org/save/{url}"
5. Capture to vault using link_capture module
6. Comment on brain-dumps issue #17 with link + archive URL

Use:
```python
import sys, os
sys.path.insert(0, os.path.expanduser("~/lobster/lobster-shop/obsidian-km/src"))
from link_capture import capture_link_sync

result = capture_link_sync(
    url="{url}",
    caption="{caption}",
    title=page_title,
    archived_url=archive_url,
)
print(result.message)
```

Report only on error or skip.
```

### Manual Commands

| Command | Action |
|---------|--------|
| `/vault <url>` | Force-capture URL (bypass duplicate check) |
| `/vault status` | Show capture stats this month |
| `/obsidian` | Alias for `/vault` |

### Error Handling

If capture fails:
- Log error but don't interrupt user flow
- Report to Drew only on persistent failures
- Never expose stack traces in Telegram
