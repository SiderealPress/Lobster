# Obsidian Knowledge Management

Manage your Obsidian vault directly from Lobster.

## What It Does

- **Append to notes** — Add content to existing notes without opening Obsidian
- **Preserve structure** — Frontmatter and formatting are maintained
- **Track changes** — Automatically updates the `modified` timestamp

## Setup

1. Set your vault path:
   ```bash
   export OBSIDIAN_VAULT_PATH="$HOME/Documents/MyVault"
   ```

2. Install the skill:
   ```bash
   bash lobster-shop/obsidian-km/install.sh
   ```

3. Activate:
   ```
   activate_skill("obsidian-km")
   ```

## Tools

### note_append

Append content to an existing Obsidian note.

**Parameters:**
- `title_or_path` (required): Note title or relative path (e.g., "Daily Notes/2024-01-15")
- `content` (required): Content to append
- `separator` (optional): Separator between existing and new content (default: "\n")

**Returns:**
- `file_path`: Absolute path to the note
- `char_count`: New total character count
- `modified_at`: ISO timestamp of modification

**Example:**
```json
{
  "title_or_path": "Inbox",
  "content": "- [ ] Review quarterly report",
  "separator": "\n"
}
```

## Technical Notes

- Uses atomic writes (temp file + rename) to prevent data loss
- Preserves existing frontmatter and updates only the `modified` field
- Note must exist — this tool does not create new notes
- Target response time: < 500ms
