"""
BIS-297: Find Supply Chain Contacts via Web Search

Given a company name, searches the web (DuckDuckGo HTML endpoint) for
supply-chain and demand-planning roles at that company.

Search queries issued (in order):
  1. "{company} VP supply chain site:linkedin.com"
  2. "{company} director supply chain site:linkedin.com"
  3. "{company} demand planner site:linkedin.com"
  4. "{company} supply chain manager"
  5. "{company} demand planning site:linkedin.com"

Returns a list of {name, title, company, source_url} dicts.
Duplicate source_url entries are deduplicated; empty name/title rows are dropped.

Usage:
    python find_supply_chain_contacts.py --company "Acme Corp"

Exits 0 and prints JSON array on success.
Exits 1 on hard errors.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.parse
from typing import Any

import requests

# --- Supply-chain role search templates ---
_SEARCH_TEMPLATES = [
    '"{company}" "VP supply chain" site:linkedin.com',
    '"{company}" "director of supply chain" site:linkedin.com',
    '"{company}" "demand planner" site:linkedin.com',
    '"{company}" "supply chain manager" site:linkedin.com',
    '"{company}" "demand planning" site:linkedin.com',
    '"{company}" "VP operations" "supply chain" site:linkedin.com',
    '"{company}" "head of supply chain" site:linkedin.com',
]

_DDGO_URL = "https://html.duckduckgo.com/html/"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# Patterns to extract name/title from LinkedIn-style result snippets.
# LinkedIn URLs look like: linkedin.com/in/jane-smith or linkedin.com/pub/...
_LINKEDIN_NAME_RE = re.compile(
    r"linkedin\.com/in/([a-z0-9\-]+)", re.IGNORECASE
)
_TITLE_PATTERNS = [
    # "Jane Smith · VP Supply Chain at Acme Corp"
    re.compile(
        r"([A-Z][a-z]+(?:\s[A-Z][a-z]+)*)\s*[·–|-]\s*"
        r"(VP|Director|Manager|Head|Lead|Senior|Demand\s+Planner[^·–|]*?)"
        r"\s*(?:at|@)\s*[^·–|]+",
        re.IGNORECASE,
    ),
    # "Jane Smith - Supply Chain Manager"
    re.compile(
        r"([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)\s*[-–|]\s*"
        r"((?:VP|Director|Manager|Head|Lead|Senior|Demand\s+Planner)\s+[^.\n]{0,60})",
        re.IGNORECASE,
    ),
]

_SUPPLY_CHAIN_TITLE_RE = re.compile(
    r"\b(?:VP|vice\s+president|director|manager|head|lead|senior|demand\s+planner|"
    r"supply\s+chain|demand\s+planning|procurement|logistics|operations)\b",
    re.IGNORECASE,
)


def _slug_to_name(slug: str) -> str:
    """Convert a LinkedIn URL slug to a guessed display name.

    E.g. "jane-smith-12ab" -> "Jane Smith"
    Strips trailing numeric/hash suffixes.
    """
    parts = slug.split("-")
    # Drop trailing segments that are purely numeric or short hash-like
    clean = []
    for part in parts:
        if re.match(r"^[a-f0-9]{2,8}$", part, re.IGNORECASE) or re.match(r"^\d+$", part):
            break
        clean.append(part.capitalize())
    return " ".join(clean) if clean else slug.replace("-", " ").title()


def _extract_contacts_from_html(
    html: str, company: str
) -> list[dict[str, str]]:
    """Parse DuckDuckGo HTML response for LinkedIn contact snippets."""
    contacts: list[dict[str, str]] = []

    # Split into rough result blocks — DDG wraps each in a <div class="result">
    # We look for anchor tags pointing to LinkedIn profiles.
    link_re = re.compile(
        r'href=["\']?(https?://(?:www\.)?linkedin\.com/in/[^\s"\'>&]+)',
        re.IGNORECASE,
    )
    snippet_re = re.compile(r"<[^>]+>")

    # Find all LinkedIn profile URLs
    seen_urls: set[str] = set()
    for m in link_re.finditer(html):
        url = m.group(1).rstrip("/")
        if url in seen_urls:
            continue
        seen_urls.add(url)

        slug_match = _LINKEDIN_NAME_RE.search(url)
        if not slug_match:
            continue
        slug = slug_match.group(1)
        guessed_name = _slug_to_name(slug)

        # Grab surrounding text (300 chars before + after the match)
        start = max(0, m.start() - 300)
        end = min(len(html), m.end() + 300)
        context = snippet_re.sub(" ", html[start:end])

        title = ""
        for pat in _TITLE_PATTERNS:
            tm = pat.search(context)
            if tm:
                title = tm.group(2).strip()
                break

        # Fallback: grab first supply-chain keyword phrase near the link
        if not title:
            sc_m = _SUPPLY_CHAIN_TITLE_RE.search(context)
            if sc_m:
                # Grab up to 60 chars starting at the match
                fragment = context[sc_m.start() : sc_m.start() + 80].strip()
                title = re.split(r"[·\|\n]", fragment)[0].strip()[:80]

        if not title:
            continue  # Can't determine a role — skip

        contacts.append(
            {
                "name": guessed_name,
                "title": title,
                "company": company,
                "source_url": url,
            }
        )

    return contacts


def _ddgo_search(query: str, session: requests.Session) -> str:
    """Execute a DuckDuckGo HTML search and return the raw HTML."""
    resp = session.post(
        _DDGO_URL,
        data={"q": query, "b": "", "kl": "us-en"},
        headers=_HEADERS,
        timeout=20,
        allow_redirects=True,
    )
    resp.raise_for_status()
    return resp.text


def find_supply_chain_contacts(
    company: str,
    *,
    delay_secs: float = 1.0,
    session: requests.Session | None = None,
) -> list[dict[str, str]]:
    """
    Search the web for supply chain / demand planning contacts at ``company``.

    Args:
        company: Company name to search for.
        delay_secs: Seconds to wait between search queries (be polite).
        session: Optional requests.Session (injected for testing).

    Returns:
        Deduplicated list of {name, title, company, source_url}.
    """
    if session is None:
        session = requests.Session()

    all_contacts: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    errors: list[str] = []

    for template in _SEARCH_TEMPLATES:
        query = template.format(company=company)
        try:
            html = _ddgo_search(query, session)
            contacts = _extract_contacts_from_html(html, company)
            for c in contacts:
                if c["source_url"] not in seen_urls:
                    seen_urls.add(c["source_url"])
                    all_contacts.append(c)
        except requests.RequestException as exc:
            errors.append(f"Search failed for query '{query}': {exc}")

        if delay_secs > 0:
            time.sleep(delay_secs)

    if errors and not all_contacts:
        raise RuntimeError(
            f"All searches failed for '{company}': {'; '.join(errors)}"
        )

    return all_contacts


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Find supply chain contacts at a company via web search"
    )
    parser.add_argument("--company", required=True, help="Company name to search")
    parser.add_argument(
        "--delay",
        type=float,
        default=1.0,
        help="Seconds between requests (default: 1.0)",
    )
    args = parser.parse_args()

    try:
        contacts = find_supply_chain_contacts(args.company, delay_secs=args.delay)
        print(json.dumps(contacts, indent=2))
    except Exception as exc:  # noqa: BLE001
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
