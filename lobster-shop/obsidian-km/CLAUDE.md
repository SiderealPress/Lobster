# Obsidian KM Skill

Access your Obsidian vault from Telegram. Create notes on the go, search your knowledge base, and manage your personal wiki without opening the app. Notes sync via CouchDB and remain compatible with Obsidian on all devices.

## Available MCP Tools

### `note_create(title, content, folder?, tags?)`
Create a new note with YAML frontmatter.

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `title` | string | required | Note title (becomes filename) |
| `content` | string | required | Markdown content |
| `folder` | string | `"Inbox"` | Target folder in vault |
| `tags` | string[] | `[]` | Tags for frontmatter |

Returns: `"Created note at <path>"`

### `note_read(title_or_path, folder?)`
Read an existing note by title or relative path.

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `title_or_path` | string | required | Note title or relative path |
| `folder` | string | `null` | Folder to search in (optional) |

Returns: Note content with frontmatter parsed.

### `note_search(query, folder?, limit?)`
Full-text search across vault using ripgrep.

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `query` | string | required | Search query (case-insensitive) |
| `folder` | string | `null` | Restrict to subfolder |
| `limit` | int | `10` | Max results (1-100) |

Returns: `{ query, folder, count, results: [{ title, path, excerpt, tags }] }`

### `note_append(title_or_path, content, separator?)`
Append content to an existing note.

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `title_or_path` | string | required | Note to append to |
| `content` | string | required | Content to append |
| `separator` | string | `"\n\n"` | Separator before new content |

Returns: `"Appended to <path>"`

### `note_list(folder?, tag?, limit?, sort?)`
List notes with optional filtering.

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `folder` | string | `null` | Filter by folder |
| `tag` | string | `null` | Filter by tag |
| `limit` | int | `20` | Max results |
| `sort` | string | `"modified"` | Sort by: `modified`, `created`, `title` |

Returns: `[{ title, path, modified, tags }]`

## Natural Language Patterns

| User says | Action | Notes |
|-----------|--------|-------|
| "Save a note: Project Ideas — Some brainstorm content" | `note_create(title="Project Ideas", content="Some brainstorm content")` | Title before `—`, content after |
| "Create a note about API design in Projects folder" | `note_create(title="API design", folder="Projects", content=...)` | Extract folder from message |
| "Note: remember to call mom" | `note_create(title="remember to call mom", content="")` | Quick capture, empty content OK |
| "Search my notes for machine learning" | `note_search(query="machine learning")` | |
| "Find notes about kubernetes in Projects" | `note_search(query="kubernetes", folder="Projects")` | |
| "What's in my inbox?" | `note_list(folder="Inbox")` | List inbox notes |
| "Show my recent notes" | `note_list(sort="modified", limit=10)` | |
| "List notes tagged with #work" | `note_list(tag="work")` | Strip `#` from tag |
| "Read my note about meeting notes" | `note_read(title_or_path="meeting notes")` | Fuzzy title match |
| "Open the API design note" | `note_read(title_or_path="API design")` | |
| "Add to my daily log: finished the PR review" | `note_append(title_or_path="Daily/2024-01-15", content="- finished the PR review")` | Append with list marker |
| "Append to project ideas: new feature concept" | `note_append(title_or_path="project ideas", content="new feature concept")` | |
| https://example.com/article | Automatic link capture | See Automatic Behaviors |
| https://example.com/article — Great read on APIs | Link capture with caption as note content | Caption becomes body |

## Vault Structure

```
~/obsidian-vault/
  .obsidian/           # Obsidian settings (preserved, not synced)
  Inbox/               # Default folder for new notes
  Notes/               # General notes
  Links/               # Captured URLs
  Daily/               # Daily notes (YYYY-MM-DD.md)
  Archive/             # Archived notes
  attachments/         # Images and files
```

### Frontmatter Format

Notes use YAML frontmatter for metadata:

```yaml
---
title: Note Title
tags: [tag1, tag2]
created: 2024-01-15T10:30:00Z
---

# Note Title

Content here...
```

## Automatic Behaviors

### Link Capture (when `auto_capture_links` is `true`)

When the user sends a bare URL or a URL with caption:
1. Save to `Links/` folder with page title as note title
2. Run Commonbook archival (archive.org + brain-dumps issue #17)

**Bare URL:**
```
https://example.com/great-article
```
→ Creates `Links/Great Article Title.md` with URL as content

**URL with caption:**
```
https://example.com/great-article — Must read on system design
```
→ Creates note with caption as body, URL in frontmatter

### Daily Notes

When appending to "daily log" or "today's note":
- Auto-create `Daily/YYYY-MM-DD.md` if it doesn't exist
- Append with timestamp prefix: `- 10:30 — <content>`

## Preferences

Set via `set_skill_preference("obsidian-km", key, value)` or in `preferences/defaults.toml`.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `vault_path` | string | `~/obsidian-vault` | Path to Obsidian vault |
| `default_folder` | string | `"Inbox"` | Default folder for new notes |
| `default_limit` | int | `20` | Default results for `note_list` |
| `default_sort` | string | `"modified"` | Sort order: `modified`, `created`, `title` |
| `auto_capture_links` | bool | `true` | Auto-save URLs to Links/ folder |
| `link_folder` | string | `"Links"` | Folder for captured links |
| `default_tags` | string[] | `[]` | Tags added to all new notes |
| `max_search_results` | int | `10` | Max results for `note_search` |

## Error Handling

| Error | Response | Recovery |
|-------|----------|----------|
| Note not found | "I couldn't find a note called '<title>'. Try searching: `note_search(query='<partial>')`" | Suggest search |
| Vault not found | "Obsidian vault not found at `<path>`. Run `~/lobster/lobster-shop/obsidian-km/install.sh` to set up." | Point to installer |
| CouchDB down | "CouchDB isn't responding. Check with `systemctl --user status couchdb`" | Show service command |
| Duplicate note | "A note called '<title>' already exists in <folder>. Want me to append instead?" | Offer append |
| Invalid folder | "Folder '<folder>' doesn't exist in your vault. Available: Inbox, Notes, Links, Daily, Archive" | List valid folders |
| Search no results | "No notes found matching '<query>'. Try a broader search term." | Suggest alternatives |

## Bot Commands

| Command | Description | Example |
|---------|-------------|---------|
| `/note <title>` | Quick note creation | `/note Project ideas` |
| `/vault` | Show vault status and stats | `/vault` |
| `/search <query>` | Search notes | `/search machine learning` |

## Service Management

```bash
# Check CouchDB
systemctl --user status couchdb

# Check sync status
curl -s http://localhost:5984/obsidian_notes | jq '.doc_count'

# Health check
~/lobster/config/health-checks/obsidian-km.sh

# Restart MCP server
systemctl --user restart lobster-obsidian-km
```

## Integration with Commonbook

When `auto_capture_links` is enabled, link capture also triggers the Commonbook workflow:
1. Archive URL at `https://web.archive.org/save/<URL>`
2. Comment on brain-dumps issue #17 with: original link, context, archive URL, date

This ensures all captured links are both saved locally in Obsidian AND archived on archive.org for permanence.
