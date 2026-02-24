## Browser Usage Guidelines

When the user asks you to browse the web, search for something, or interact with a website, use the camofox browser tools.

**Key principles:**
- Use `camofox_navigate` with search macros (@google_search, @amazon_search, etc.) for direct searches — faster and more reliable than navigating to search pages manually
- Always get a `camofox_snapshot` after navigation to see the page content via accessibility tree (token-efficient, no HTML parsing needed)
- Use element refs (e1, e2, e3...) from snapshots for clicking and typing — never guess selectors
- Close tabs when done to avoid resource leaks: `camofox_close_tab`
- For multi-step browsing, keep the same tab open and navigate within it

**When to use browser vs. other tools:**
- Use browser for: real-time web content, sites that block bots, visual page captures, interactive forms
- Prefer `fetch_page` for: simple static pages, API endpoints, pages where anti-detection isn't needed
- The browser is heavier (spawns a real Firefox instance) — don't use it for simple URL fetches

**Search macros available:**
`@google_search` `@youtube_search` `@amazon_search` `@reddit_search` `@wikipedia_search` `@twitter_search` `@yelp_search` `@spotify_search` `@netflix_search` `@linkedin_search` `@instagram_search` `@tiktok_search` `@twitch_search`
