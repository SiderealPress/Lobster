# Obsidian KM Skill

Knowledge management tools for Obsidian vaults.

## Tools

### note_list

List notes with filtering and sorting.

**Parameters:**
- `folder` (optional): Filter by folder path relative to vault root
- `tag` (optional): Filter by tag (checks YAML frontmatter)
- `limit` (optional): Max notes to return (default: 20, max: 1000)
- `sort` (optional): Sort by "modified" (default), "created", or "title"

**Returns:**
```json
{
  "notes": [
    {
      "title": "Project Plan",
      "path": "projects/project-plan.md",
      "tags": ["project", "planning"],
      "created": "2024-01-15T10:30:00+00:00",
      "modified": "2024-03-20T14:45:00+00:00",
      "size": 2048
    }
  ],
  "total": 150
}
```

## Configuration

Set the vault path before using:

```
/skill set obsidian-km vault_path /path/to/your/vault
```

## Performance

Optimized for large vaults:
- Uses `os.scandir()` for fast directory traversal
- Lazy evaluation with generators
- Only reads frontmatter (not full files) when filtering by tags
- Target: < 2 seconds for 10,000 notes

## Development

```bash
# Run tests
cd src && uv run pytest test_vault_ops.py -v

# Run server manually
OBSIDIAN_VAULT_PATH=/path/to/vault uv run python obsidian_km_server.py
```
