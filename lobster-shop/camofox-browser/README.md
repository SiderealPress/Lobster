# Camofox Browser

**Browse the real web without getting blocked — powered by Camoufox anti-detection.**

Regular browsers get fingerprinted and blocked by Google, Amazon, LinkedIn, and most major sites. Camofox patches Firefox at the C++ level to bypass all of that. This skill gives Lobster a real browser that works everywhere.

## What You Can Do

- **"Search Google for the best coffee shops in Austin"** -- Real Google search, no CAPTCHA
- **"Go to amazon.com and find the top-rated wireless earbuds"** -- Browse Amazon like a human
- **"Check LinkedIn for job postings about ML engineering"** -- No bot detection blocks
- **"Take a screenshot of nytimes.com"** -- Visual page captures
- **"Click the 'Sign In' button on that page"** -- Full page interaction via element refs

## How It Works

Camofox runs a local browser server (Camoufox, a Firefox fork) that Lobster talks to via MCP tools. The browser:

- Spoofs fingerprints at the **C++ level** (not JavaScript shims)
- Provides **accessibility snapshots** instead of raw HTML (90% smaller, token-efficient)
- Uses stable **element refs** (e1, e2, e3) for clicking and typing
- Supports **search macros** for Google, YouTube, Amazon, Reddit, and 10 more sites
- Isolates sessions per user with separate cookies/storage

## Setup

Run the installer:

```bash
bash ~/lobster/lobster-shop/camofox-browser/install.sh
```

The installer will:

1. Check that Node.js >= 18 is installed
2. Clone and install the camofox-browser server
3. Download the Camoufox browser engine (~300MB on first run)
4. Install the Python MCP wrapper
5. Register the MCP server with Claude
6. Start the camofox server

## Tools

| Tool | What It Does |
|------|-------------|
| `camofox_create_tab` | Open a new browser tab at a URL |
| `camofox_snapshot` | Get an accessibility snapshot with clickable element refs |
| `camofox_click` | Click an element by ref (e.g., e1) or CSS selector |
| `camofox_type` | Type text into a form field |
| `camofox_navigate` | Go to a URL or use a search macro (@google_search, etc.) |
| `camofox_scroll` | Scroll the page up/down/left/right |
| `camofox_screenshot` | Take a PNG screenshot of the page |
| `camofox_close_tab` | Close a browser tab |
| `camofox_list_tabs` | List all open tabs |

## Search Macros

Use these with `camofox_navigate` to search directly:

`@google_search` `@youtube_search` `@amazon_search` `@reddit_search` `@wikipedia_search` `@twitter_search` `@yelp_search` `@spotify_search` `@netflix_search` `@linkedin_search` `@instagram_search` `@tiktok_search` `@twitch_search`

## Managing the Server

```bash
# Check if server is running
curl http://localhost:9377/health

# Start the server manually
cd ~/lobster/lobster-shop/camofox-browser/server && npm start

# Stop the server
# The server runs as a systemd user service:
systemctl --user stop camofox-browser
systemctl --user start camofox-browser
systemctl --user status camofox-browser
```

## Architecture

```
Lobster (Claude Code)
  |
  |-- MCP: camofox_create_tab, camofox_snapshot, etc.
  |
  v
camofox_mcp_server.py (Python MCP wrapper)
  |
  |-- HTTP REST calls
  |
  v
camofox-browser server.js (Node.js, port 9377)
  |
  v
Camoufox (patched Firefox engine)
```

## Requirements

- Node.js >= 18
- Python 3.10+
- ~500MB disk space (Camoufox browser engine + dependencies)
- Linux x86_64 (Camoufox binary)

## Credits

- [Camoufox](https://camoufox.com) -- Firefox fork with C++ anti-detection
- [camofox-browser](https://github.com/jo-inc/camofox-browser) -- REST API server by Jo Inc

## Status

**Available** -- Ready to install and use.
