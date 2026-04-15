# Obsidian Knowledge Management

You have access to an Obsidian vault for knowledge management. Use these tools to help the user organize and retrieve information.

## Available Tools

- **obsidian_create_note**: Create a new note in the vault with a title, content, optional folder, and tags
- **obsidian_search**: Search the vault for notes matching a query (searches filenames and content)
- **obsidian_capture_link**: Archive a URL to the vault with optional title and notes
- **obsidian_get_preferences**: View current Obsidian KM configuration

## Usage Guidelines

1. **Creating Notes**
   - Use meaningful titles that describe the content
   - Default folder is "Inbox" — use this for quick captures
   - Add relevant tags to improve discoverability
   - Use markdown formatting for content

2. **Searching**
   - Search is case-insensitive
   - Searches both filenames and note content
   - Results are limited by the max_search_results preference

3. **Capturing Links**
   - Automatically extracts title from URL if not provided
   - Links are saved to the "Links" folder by default
   - Add notes to provide context for why the link was saved
   - Auto-capture can be disabled via preferences

4. **When to Use Each Tool**
   - User says "save this", "remember this", "note this down" → create_note
   - User says "find", "search", "look for" → search
   - User shares a URL and says "save", "archive", "bookmark" → capture_link
   - User asks about settings or configuration → get_preferences

## Behavior Notes

- All notes include frontmatter with title, creation date, tags, and source
- Notes are saved as .md files in the configured vault
- If a file with the same name exists, a timestamp is appended
- The vault path must be configured in ~/lobster-config/obsidian.env
