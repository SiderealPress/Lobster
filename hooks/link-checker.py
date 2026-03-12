#!/usr/bin/env python3
"""
Link enforcement hook for send_reply.

Fires before mcp__lobster-inbox__send_reply tool calls.
Blocks messages that reference completed work but contain no clickable markdown links.

Exit codes:
  0 - Allow the tool call (or non-blocking warning written to stderr)
  2 - Block the tool call (Claude Code shows stderr to Claude)
"""

import json
import re
import sys


# Patterns indicating completed work that references a specific GitHub artifact
# Keep this tight: only explicit PR/issue number references, not generic words
COMPLETION_PATTERNS = [
    r"\bissue\s*#\d+",
    r"\bpr\s*#\d+",
    r"\bpull\s+request\s*#\d+",
]

# Pattern for markdown links: [text](url)
MARKDOWN_LINK_RE = re.compile(r"\[.+?\]\(https?://[^\)]+\)")

# Pattern for bare URLs (not already wrapped in markdown)
BARE_URL_RE = re.compile(r"(?<!\()https?://\S+")

# Compile completion patterns with case-insensitive flag
COMPLETION_RES = [re.compile(p, re.IGNORECASE) for p in COMPLETION_PATTERNS]


def has_completion_language(text: str) -> bool:
    return any(r.search(text) for r in COMPLETION_RES)


def has_markdown_links(text: str) -> bool:
    return bool(MARKDOWN_LINK_RE.search(text))


def find_bare_urls(text: str) -> list:
    # Find URLs that are NOT already inside a markdown link [text](url)
    # Strategy: find all markdown links first, collect their URL spans, then
    # check bare URLs against those spans.
    md_links = list(MARKDOWN_LINK_RE.finditer(text))
    md_url_spans = set()
    for m in md_links:
        # The URL is inside (...) at the end of the match
        inner = m.group(0)
        url_start_in_match = inner.index("](") + 2
        abs_start = m.start() + url_start_in_match
        abs_end = m.end() - 1  # exclude trailing ")"
        md_url_spans.update(range(abs_start, abs_end))

    bare = []
    for m in BARE_URL_RE.finditer(text):
        # If any character of this URL is inside a markdown link, skip it
        if any(pos in md_url_spans for pos in range(m.start(), m.end())):
            continue
        bare.append(m.group(0))
    return bare


def main():
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        # If we can't parse the input, allow the call
        sys.exit(0)

    tool_name = data.get("tool_name", "")
    tool_input = data.get("tool_input", {})

    # Only check send_reply calls
    if tool_name != "mcp__lobster-inbox__send_reply":
        sys.exit(0)

    text = tool_input.get("text", "")
    if not text:
        sys.exit(0)

    has_completion = has_completion_language(text)
    has_links = has_markdown_links(text)
    bare_urls = find_bare_urls(text)

    # BLOCKING: references a specific PR/issue but has no links at all (not even bare URLs)
    if has_completion and not has_links and not bare_urls:
        print(
            "BLOCKED: Message references completed work but contains no clickable links. "
            "Add markdown links [text](url) before sending.",
            file=sys.stderr,
        )
        sys.exit(2)

    # NON-BLOCKING: bare URLs exist that aren't wrapped in markdown
    if bare_urls:
        print(
            "Warning: Message contains bare URLs that aren't wrapped in markdown links. "
            "Consider wrapping them as [text](url) for clickable links. "
            f"Bare URLs found: {', '.join(bare_urls)}",
            file=sys.stderr,
        )
        sys.exit(0)

    sys.exit(0)


if __name__ == "__main__":
    main()
