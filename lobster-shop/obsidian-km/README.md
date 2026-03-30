# Obsidian KM Skill

Knowledge management skill for Lobster using Obsidian vaults.

## Overview

Pure Python vault operations using filesystem + `python-frontmatter` + ripgrep.
No external service dependencies — works on headless servers.

## Installation

Requires `python-frontmatter`:

```bash
uv pip install python-frontmatter
```

## Usage

```python
from obsidian_km.src.vault_ops import create_note, read_note, search_notes

# Create a note
path = create_note(
    title="Meeting Notes",
    content="# Meeting Notes\n\nDiscussed roadmap.",
    folder="Inbox",
    tags=["meetings"],
)

# Read a note
note = read_note("Meeting Notes", folder="Inbox")
print(note["content"])

# Search notes
matches = search_notes("roadmap")
for m in matches:
    print(f"{m['title']}: {m['line_content']}")
```

## API Reference

### Path Utilities

- `resolve_vault_path(vault=None)` — Returns vault directory (default: `~/obsidian-vault/`)
- `sanitize_title(title)` — Remove invalid filename characters

### Note Operations

- `create_note(title, content, folder="Inbox", tags=None, vault=None)` — Create note
- `read_note(title_or_path, folder=None, vault=None)` — Read note by title or path
- `append_to_note(title_or_path, content, separator="\n", vault=None)` — Append to note

### Search & Discovery

- `search_notes(query, folder=None, limit=10, vault=None)` — Full-text search (ripgrep)
- `list_notes(folder=None, tag=None, limit=20, sort="modified", vault=None)` — List notes

## Testing

Run the proof of concept:

```bash
cd lobster-shop/obsidian-km
python scripts/vault_poc.py
```

## Design Decisions

See [docs/cli-approach.md](docs/cli-approach.md) for the technical decision document.

## Part of BIS-229 Epic

This module is the foundation for:
- BIS-238: MCP tool wrappers
- BIS-239: Template-based note creation
- BIS-240: Daily note handling
- BIS-241: Note linking and backlinks
- BIS-242: Integration tests
