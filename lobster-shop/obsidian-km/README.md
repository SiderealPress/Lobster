# Obsidian KM

**Automatic link capture to your Obsidian vault.**

When Drew sends a URL in Telegram, Lobster automatically saves it to your Obsidian vault with:
- Page title (fetched automatically)
- Archive.org backup
- Original caption
- Date captured
- `#link` tag for easy filtering

## Features

- **Automatic capture** — URLs are saved without any extra commands
- **Duplicate detection** — Skip URLs already captured this month
- **Page title fetch** — Uses headless browser to get real page titles
- **Archive integration** — Links are backed up on archive.org
- **Clean markdown** — Notes use YAML frontmatter for Obsidian compatibility

## Installation

```bash
bash lobster-shop/obsidian-km/install.sh
```

Then create your Obsidian vault:
```bash
mkdir -p ~/obsidian-vault/Links
```

## Usage

Just send URLs in Telegram — they're captured automatically.

### Commands

| Command | Description |
|---------|-------------|
| `/vault <url>` | Force-capture a URL (bypass duplicate check) |
| `/vault status` | Show how many links captured this month |

## Configuration

Set preferences via `set_skill_preference`:

| Preference | Default | Description |
|------------|---------|-------------|
| `OBSIDIAN_AUTO_CAPTURE_LINKS` | `true` | Enable automatic capture |
| `OBSIDIAN_VAULT_PATH` | `~/obsidian-vault` | Path to vault |
| `OBSIDIAN_LINKS_FOLDER` | `Links` | Folder for captured links |

## Note Format

```markdown
---
title: "Article Title"
url: https://example.com/article
tags: [link]
captured: 2026-03-30T14:23:00
archived: https://web.archive.org/web/20260330/https://example.com/article
---

[https://example.com/article](https://example.com/article)

Saved from Telegram on 2026-03-30.
```

## Integration

This skill **extends** existing Commonbook behavior:

1. Archive on archive.org (Commonbook)
2. Comment on brain-dumps issue (Commonbook)
3. Save to Obsidian vault (this skill)

All three happen for every captured link.
