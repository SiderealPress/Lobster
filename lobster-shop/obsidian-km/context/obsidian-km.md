## Obsidian KM — Automatic Link Capture

This skill automatically saves URLs that Drew sends to the Obsidian vault at `~/obsidian-vault/Links/`.

### When to capture links

**Trigger:** Any message containing a URL (http:// or https://).

**Before capturing, always check:**
1. The `OBSIDIAN_AUTO_CAPTURE_LINKS` preference — if false, skip capture
2. Run duplicate detection — skip if URL already saved this month

### How to capture a link

Delegate to a background subagent — fetching page titles takes > 7 seconds.

**Dispatcher pattern:**
```
# 1. Acknowledge immediately
send_reply(chat_id, "Link saved.", message_id=message_id)

# 2. Delegate to subagent for the actual work
Task(
    prompt=f"""
    Capture this link to Obsidian vault:

    URL: {url}
    Caption: {caption or "None"}

    Steps:
    1. Check OBSIDIAN_AUTO_CAPTURE_LINKS preference (skip if false)
    2. Run duplicate detection for this month
    3. Fetch page title using fetch_page MCP tool
    4. Archive on archive.org (existing Commonbook behavior)
    5. Save note to ~/obsidian-vault/Links/
    6. Add comment to brain-dumps issue #17 (existing Commonbook behavior)

    Use the link_capture module:
    ```python
    import sys, os
    sys.path.insert(0, os.path.expanduser("~/lobster/lobster-shop/obsidian-km/src"))
    from link_capture import capture_link

    result = await capture_link(
        url="{url}",
        caption="{caption or ''}",
    )
    ```

    Report back only if there's an error or if the link was skipped.
    """,
    subagent_type="general-purpose",
    run_in_background=True
)
```

### Duplicate detection logic

A link is considered a duplicate if a file already exists in `~/obsidian-vault/Links/` this month with the same URL in its frontmatter. Check by:

1. Glob `~/obsidian-vault/Links/{YYYY-MM}*.md` for current month
2. Grep for `url: {normalized_url}` in those files
3. If found, skip capture and log "Duplicate link — already captured this month"

### Note template

Saved to `~/obsidian-vault/Links/{YYYY-MM-DD}-{slug}.md`:

```markdown
---
title: "Page Title"
url: https://example.com/
tags: [link]
captured: 2026-03-30T14:23:00
archived: https://web.archive.org/web/20260330/https://example.com/
---

[https://example.com/](https://example.com/)

Saved from Telegram on 2026-03-30.
```

Where:
- `title` — fetched from page title, or domain if unavailable
- `url` — the original URL
- `archived` — the archive.org snapshot URL (after archiving)
- `slug` — URL-safe version of title (max 50 chars)

### Preference: `OBSIDIAN_AUTO_CAPTURE_LINKS`

| Value | Behavior |
|-------|----------|
| `true` (default) | Automatically capture links to vault |
| `false` | Skip automatic capture; only respond to explicit `/vault` commands |

Check preference using:
```python
from mcp.skill_system.skills import get_skill_preference

auto_capture = get_skill_preference("obsidian-km", "OBSIDIAN_AUTO_CAPTURE_LINKS")
if auto_capture is False:
    # Skip automatic capture
    return
```

### Integration with Commonbook

This skill **extends** existing Commonbook behavior — it does NOT replace it.

When a link is captured:
1. Archive on archive.org ✓ (Commonbook)
2. Comment on brain-dumps issue #17 ✓ (Commonbook)
3. Save note to Obsidian vault ✓ (NEW — this skill)

All three actions should happen for every captured link.

### Vault directory structure

```
~/obsidian-vault/
├── Links/
│   ├── 2026-03-30-example-article.md
│   ├── 2026-03-30-github-repo.md
│   └── ...
└── ...
```

The `Links/` folder is created automatically if it doesn't exist.
