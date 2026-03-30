# Obsidian KM Skill

Access and manage notes in an Obsidian vault.

## Available Tools

### note_list

List notes in the vault with optional filtering and sorting.

**Parameters:**
- `folder` (optional): Filter to notes within this folder path (relative to vault root)
- `tag` (optional): Filter to notes containing this tag (checks YAML frontmatter `tags` field)
- `limit` (optional, default: 20): Maximum notes to return
- `sort` (optional, default: "modified"): Sort order — "modified", "created", or "title"

**Returns:**
- `notes`: Array of note objects with: title, path, tags, created, modified, size
- `total`: Total count of matching notes (before limit applied)

## Configuration

Set the vault path using skill preferences:

```
/skill set obsidian-km vault_path /path/to/your/vault
```

## Usage Notes

- Tag filtering checks the `tags` field in YAML frontmatter
- Paths are relative to the vault root
- Hidden files/folders (starting with `.`) are excluded
- The `.obsidian` config folder is always excluded
