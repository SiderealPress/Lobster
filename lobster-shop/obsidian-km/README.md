# Obsidian KM Skill

**Capture ideas, archive links, and build a knowledge base — right from conversation.**

The Obsidian KM skill lets Lobster read and write to your Obsidian vault. Notes are stored as plain Markdown files, compatible with Obsidian and any other Markdown tool.

## What You Can Do

- **Quick capture**: "Note: check out the new RFC on structured outputs"
- **Archive links**: "Save this link" — Lobster archives it and adds it to your Links folder
- **Search your notes**: "What did I write about vector databases?"
- **Daily notes**: Lobster can append to or create daily notes

## Installation

```bash
bash install.sh
```

This creates your vault at `~/obsidian-vault/` with the standard folder structure:

| Folder     | Purpose                                           |
|------------|---------------------------------------------------|
| `Inbox/`   | Quick captures, unprocessed notes                 |
| `Notes/`   | Permanent notes and knowledge base                |
| `Links/`   | Archived web links with context                   |
| `Daily/`   | Daily notes (YYYY-MM-DD.md)                       |
| `Archive/` | Inactive notes and completed projects             |

## Syncing

The vault syncs to other devices via CouchDB using the PouchDB protocol. Install the [Remotely Save](https://github.com/remotely-save/remotely-save) plugin in Obsidian and configure it to connect to your CouchDB server.

See [BIS-230](https://github.com/SiderealPress/Lobster/issues/BIS-230) for CouchDB setup.

## Configuration

Set the vault location (defaults to `~/obsidian-vault/`):

```bash
export OBSIDIAN_VAULT_DIR="$HOME/my-vault"
bash install.sh
```

## Commands

| Command   | Description                                    |
|-----------|------------------------------------------------|
| `/note`   | Create a quick note                            |
| `/vault`  | Search or browse your vault                    |

## MCP Tools

| Tool          | Description                                    |
|---------------|------------------------------------------------|
| `note_create` | Create a new note in the vault                 |
| `note_read`   | Read a note by path or search                  |
| `note_search` | Full-text search across notes                  |
| `note_list`   | List notes in a folder                         |

## Related Issues

- [BIS-228](https://github.com/SiderealPress/Lobster/issues/BIS-228) — Obsidian KM Skill (epic)
- [BIS-230](https://github.com/SiderealPress/Lobster/issues/BIS-230) — Install CouchDB for sync
- [BIS-233](https://github.com/SiderealPress/Lobster/issues/BIS-233) — Create vault structure (this issue)
- [BIS-234](https://github.com/SiderealPress/Lobster/issues/BIS-234) — Note create/read tools
