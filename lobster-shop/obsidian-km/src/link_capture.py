"""
Obsidian KM — Link Capture Module

Captures URLs to the Obsidian vault with metadata, page titles, and archive URLs.
Designed for functional composition with clear input/output contracts.
"""

from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse


# =============================================================================
# Configuration
# =============================================================================

VAULT_PATH = Path(os.path.expanduser("~/obsidian-vault"))
LINKS_FOLDER = VAULT_PATH / "Links"
DEFAULT_AUTO_CAPTURE = True


# =============================================================================
# Data Types
# =============================================================================

@dataclass(frozen=True)
class LinkNote:
    """Immutable representation of a captured link note."""
    title: str
    url: str
    tags: tuple[str, ...]
    captured: datetime
    archived: Optional[str]
    caption: Optional[str]

    def to_markdown(self) -> str:
        """Render note as markdown with YAML frontmatter."""
        tags_yaml = ", ".join(self.tags)
        captured_iso = self.captured.strftime("%Y-%m-%dT%H:%M:%S")
        date_str = self.captured.strftime("%Y-%m-%d")

        archived_line = f'archived: {self.archived}\n' if self.archived else ''
        caption_line = f'\n{self.caption}\n' if self.caption else ''

        return f"""---
title: "{escape_yaml_string(self.title)}"
url: {self.url}
tags: [{tags_yaml}]
captured: {captured_iso}
{archived_line}---

[{self.url}]({self.url})
{caption_line}
Saved from Telegram on {date_str}.
"""


@dataclass(frozen=True)
class CaptureResult:
    """Result of a link capture operation."""
    success: bool
    filepath: Optional[Path]
    message: str
    skipped: bool = False


# =============================================================================
# Pure Functions
# =============================================================================

def escape_yaml_string(s: str) -> str:
    """Escape special characters for YAML string values."""
    return s.replace('\\', '\\\\').replace('"', '\\"')


def slugify(text: str, max_length: int = 50) -> str:
    """
    Convert text to URL-safe slug.

    Pure function: same input always produces same output.
    """
    # Normalize unicode characters
    text = unicodedata.normalize('NFKD', text)
    text = text.encode('ascii', 'ignore').decode('ascii')

    # Convert to lowercase and replace non-alphanumeric with hyphens
    text = re.sub(r'[^\w\s-]', '', text.lower())
    text = re.sub(r'[-\s]+', '-', text).strip('-')

    # Truncate to max length at word boundary
    if len(text) > max_length:
        text = text[:max_length].rsplit('-', 1)[0]

    return text or 'untitled'


def extract_domain(url: str) -> str:
    """Extract domain from URL as fallback title."""
    try:
        parsed = urlparse(url)
        domain = parsed.netloc
        # Remove www. prefix
        if domain.startswith('www.'):
            domain = domain[4:]
        return domain
    except Exception:
        return 'unknown'


def normalize_url(url: str) -> str:
    """
    Normalize URL for duplicate detection.

    Strips trailing slashes, removes www., lowercases domain.
    """
    url = url.strip()
    if not url.startswith(('http://', 'https://')):
        url = 'https://' + url

    try:
        parsed = urlparse(url)
        domain = parsed.netloc.lower()
        if domain.startswith('www.'):
            domain = domain[4:]

        path = parsed.path.rstrip('/')
        query = f'?{parsed.query}' if parsed.query else ''

        return f"{parsed.scheme}://{domain}{path}{query}"
    except Exception:
        return url


def generate_filename(title: str, captured: datetime) -> str:
    """Generate filename for link note."""
    date_prefix = captured.strftime("%Y-%m-%d")
    slug = slugify(title)
    return f"{date_prefix}-{slug}.md"


def create_link_note(
    url: str,
    title: str,
    archived_url: Optional[str] = None,
    caption: Optional[str] = None,
    captured: Optional[datetime] = None,
) -> LinkNote:
    """
    Create an immutable LinkNote from inputs.

    Pure function: no side effects, deterministic output.
    """
    return LinkNote(
        title=title or extract_domain(url),
        url=normalize_url(url),
        tags=('link',),
        captured=captured or datetime.now(timezone.utc),
        archived=archived_url,
        caption=caption.strip() if caption else None,
    )


# =============================================================================
# I/O Functions (side effects isolated here)
# =============================================================================

def ensure_links_folder() -> Path:
    """Create Links folder if it doesn't exist."""
    LINKS_FOLDER.mkdir(parents=True, exist_ok=True)
    return LINKS_FOLDER


def check_duplicate_this_month(url: str, now: Optional[datetime] = None) -> bool:
    """
    Check if URL was already captured this month.

    Returns True if duplicate found, False otherwise.
    """
    now = now or datetime.now(timezone.utc)
    month_prefix = now.strftime("%Y-%m")
    normalized = normalize_url(url)

    if not LINKS_FOLDER.exists():
        return False

    # Check files from this month
    for filepath in LINKS_FOLDER.glob(f"{month_prefix}*.md"):
        try:
            content = filepath.read_text(encoding='utf-8')
            # Look for url: in frontmatter
            if f"url: {normalized}" in content:
                return True
        except Exception:
            continue

    return False


def save_note_to_vault(note: LinkNote) -> Path:
    """
    Write note to vault. Returns filepath.

    Side effect: writes file to disk.
    """
    folder = ensure_links_folder()
    filename = generate_filename(note.title, note.captured)
    filepath = folder / filename

    # Handle collision by appending counter
    counter = 1
    base_filepath = filepath
    while filepath.exists():
        stem = base_filepath.stem
        filepath = folder / f"{stem}-{counter}.md"
        counter += 1

    filepath.write_text(note.to_markdown(), encoding='utf-8')
    return filepath


def get_auto_capture_preference() -> bool:
    """
    Check OBSIDIAN_AUTO_CAPTURE_LINKS preference.

    Returns True (capture enabled) or False (disabled).
    """
    try:
        import sys
        sys.path.insert(0, os.path.expanduser("~/lobster/src"))
        from mcp.skill_system.skills import get_skill_preference

        value = get_skill_preference("obsidian-km", "OBSIDIAN_AUTO_CAPTURE_LINKS")
        if value is None:
            return DEFAULT_AUTO_CAPTURE
        return bool(value)
    except ImportError:
        # Skill system not available, use default
        return DEFAULT_AUTO_CAPTURE
    except Exception:
        return DEFAULT_AUTO_CAPTURE


# =============================================================================
# High-Level API
# =============================================================================

async def capture_link(
    url: str,
    caption: Optional[str] = None,
    title: Optional[str] = None,
    archived_url: Optional[str] = None,
    skip_preference_check: bool = False,
    skip_duplicate_check: bool = False,
) -> CaptureResult:
    """
    Capture a link to the Obsidian vault.

    This is the main entry point for link capture. It:
    1. Checks OBSIDIAN_AUTO_CAPTURE_LINKS preference
    2. Checks for duplicates this month
    3. Creates and saves the note

    Args:
        url: The URL to capture
        caption: Optional caption/context from the message
        title: Optional page title (if already fetched)
        archived_url: Optional archive.org URL (if already archived)
        skip_preference_check: Skip the auto-capture preference check
        skip_duplicate_check: Skip duplicate detection

    Returns:
        CaptureResult with success status, filepath, and message
    """
    # Validate URL
    url = url.strip()
    if not url:
        return CaptureResult(
            success=False,
            filepath=None,
            message="Empty URL provided",
        )

    if not url.startswith(('http://', 'https://')):
        url = 'https://' + url

    # Check preference
    if not skip_preference_check and not get_auto_capture_preference():
        return CaptureResult(
            success=False,
            filepath=None,
            message="Auto-capture disabled (OBSIDIAN_AUTO_CAPTURE_LINKS=false)",
            skipped=True,
        )

    # Check for duplicates
    if not skip_duplicate_check and check_duplicate_this_month(url):
        return CaptureResult(
            success=False,
            filepath=None,
            message=f"Duplicate link — already captured this month: {url}",
            skipped=True,
        )

    # Create note
    note = create_link_note(
        url=url,
        title=title or extract_domain(url),
        archived_url=archived_url,
        caption=caption,
    )

    # Save to vault
    try:
        filepath = save_note_to_vault(note)
        return CaptureResult(
            success=True,
            filepath=filepath,
            message=f"Link captured: {filepath.name}",
        )
    except Exception as e:
        return CaptureResult(
            success=False,
            filepath=None,
            message=f"Failed to save note: {e}",
        )


def capture_link_sync(
    url: str,
    caption: Optional[str] = None,
    title: Optional[str] = None,
    archived_url: Optional[str] = None,
    skip_preference_check: bool = False,
    skip_duplicate_check: bool = False,
) -> CaptureResult:
    """
    Synchronous version of capture_link for non-async contexts.
    """
    import asyncio

    return asyncio.run(capture_link(
        url=url,
        caption=caption,
        title=title,
        archived_url=archived_url,
        skip_preference_check=skip_preference_check,
        skip_duplicate_check=skip_duplicate_check,
    ))


# =============================================================================
# URL Extraction Helper
# =============================================================================

URL_PATTERN = re.compile(
    r'https?://[^\s<>"{}|\\^`\[\]]+',
    re.IGNORECASE
)


def extract_urls(text: str) -> list[str]:
    """
    Extract all URLs from text.

    Returns list of unique URLs in order of first appearance.
    """
    matches = URL_PATTERN.findall(text)
    # Deduplicate while preserving order
    seen = set()
    urls = []
    for url in matches:
        normalized = normalize_url(url)
        if normalized not in seen:
            seen.add(normalized)
            urls.append(url)
    return urls


def contains_url(text: str) -> bool:
    """Check if text contains any URLs."""
    return bool(URL_PATTERN.search(text))
