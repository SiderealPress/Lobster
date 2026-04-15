# Obsidian KM

**Sync and access your Obsidian vault from anywhere via Telegram.**

Take notes, search your knowledge base, and manage your vault without opening the app. Your notes stay in sync between your devices and are always accessible through Lobster.

## What You Can Do

- **"Create a note about my project ideas"** -- Start a new note instantly
- **"Search my notes for machine learning"** -- Find notes by keyword
- **"Read my meeting notes from Monday"** -- Retrieve note content
- **"Append to my daily log: finished the API integration"** -- Add to existing notes
- **"List my recent notes"** -- See what you've been working on

## How It Works

Obsidian KM uses CouchDB as a sync backend for your Obsidian vault. Notes are stored as standard Markdown files (compatible with Obsidian) and synced to CouchDB for fast search and access. A TLS proxy (Caddy) provides secure access.

```
Lobster (Claude Code)
  |
  |-- MCP: note_create, note_read, note_search, etc.
  |
  v
obsidian_km_mcp_server.py (Python MCP server)
  |
  |-- CouchDB queries / file operations
  |
  v
CouchDB <--sync--> Obsidian Vault (~/*.md files)
```

## Setup

Run the installer:


```bash
bash ~/lobster/lobster-shop/obsidian-km/install.sh
```

The installer will:

1. Install and configure CouchDB
2. Create an Obsidian vault (or use existing)
3. Set up database views for fast note lookup
4. Configure Caddy TLS proxy
5. Install the MCP server
6. Register health checks

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `OBSIDIAN_VAULT_DIR` | `~/obsidian-vault` | Path to your Obsidian vault |
| `COUCHDB_PORT` | `5984` | CouchDB port |
| `COUCHDB_ADMIN_USER` | `admin` | CouchDB admin username |
| `COUCHDB_ADMIN_PASS` | (generated) | CouchDB admin password |
| `COUCHDB_DB_NAME` | `obsidian_notes` | Database name for notes |
| `CADDY_HTTPS_PORT` | `5985` | TLS proxy port |

## Tools

| Tool | What It Does |
|------|-------------|
| `note_create` | Create a new note with title and content |
| `note_read` | Read an existing note by title or path |
| `note_search` | Search notes by keyword (uses ripgrep) |
| `note_append` | Append content to an existing note |
| `note_list` | List recent notes, optionally filtered by tag |

## Commands

| Command | What It Does |
|---------|-------------|
| `/note` | Create or manage notes |
| `/vault` | Vault operations (status, sync, etc.) |
| `/search` | Search your notes |

## Managing the Services

```bash
# Check CouchDB status
sudo systemctl status couchdb

# Check Caddy (TLS proxy) status
sudo systemctl status caddy

# Run health check
~/lobster/config/health-checks/obsidian-km.sh

# View CouchDB directly
curl http://localhost:5984/

# Access via TLS proxy
curl -k https://localhost:5985/
```

## Vault Structure

The skill works with standard Obsidian vault structure:

```
~/obsidian-vault/
  .obsidian/           # Obsidian settings (preserved)
  attachments/         # Images and files
  Welcome.md           # Created by installer
  *.md                 # Your notes
```

Notes support YAML frontmatter for metadata:

```yaml
---
title: Project Ideas
tags: [ideas, projects]
created: 2024-01-15
---

# Project Ideas

Your note content here...
```

## Requirements

- CouchDB 3.x
- Caddy 2.x (for TLS proxy)
- ripgrep (for fast search)
- Python 3.11+
- ~200MB disk space for CouchDB + vault

## Architecture

The skill is split into phases (BIS-228 epic):

| Issue | Component | Status |
|-------|-----------|--------|
| BIS-230 | CouchDB installation | In this installer |
| BIS-231 | CouchDB configuration | In this installer |
| BIS-232 | TLS proxy setup | In this installer |
| BIS-233 | Vault creation | In this installer |
| BIS-235 | Health checks | In this installer |
| BIS-236 | Master install.sh | This file |
| BIS-243 | MCP server | Placeholder (coming soon) |

## Status

**In Development** -- Core infrastructure ready, MCP server implementation pending (BIS-243).

