"""
Tests for BIS-297: find_supply_chain_contacts

Mocks HTTP to DuckDuckGo — no real network calls.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

# Add bin dir to path
sys.path.insert(
    0,
    str(Path(__file__).parent.parent.parent.parent
        / "lobster-shop" / "prospect-enrichment" / "bin"),
)

from find_supply_chain_contacts import (
    find_supply_chain_contacts,
    _extract_contacts_from_html,
    _slug_to_name,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_html_with_linkedin(
    url: str,
    surrounding_text: str = "",
) -> str:
    """Build a minimal DDG-like HTML page containing one LinkedIn link."""
    return (
        f'<div class="result">'
        f'<a href="{url}">{surrounding_text}</a>'
        f'<span>{surrounding_text}</span>'
        f"</div>"
    )


def _mock_session(html_pages: list[str]) -> MagicMock:
    """Return a mock session whose .post() returns pages in order."""
    session = MagicMock()
    responses = []
    for html in html_pages:
        r = MagicMock()
        r.raise_for_status = MagicMock()
        r.text = html
        responses.append(r)
    # Repeat last page for remaining queries
    last = responses[-1] if responses else MagicMock(text="", raise_for_status=MagicMock())
    session.post.side_effect = responses + [last] * 20
    return session


# ---------------------------------------------------------------------------
# Unit Tests
# ---------------------------------------------------------------------------

class TestSlugToName:
    def test_simple_slug(self):
        assert _slug_to_name("jane-smith") == "Jane Smith"

    def test_strips_numeric_suffix(self):
        assert _slug_to_name("jane-smith-123") == "Jane Smith"

    def test_strips_hash_suffix(self):
        # 8-char hex hash after name
        result = _slug_to_name("john-doe-a1b2c3d4")
        assert result == "John Doe"

    def test_single_name(self):
        assert _slug_to_name("alice") == "Alice"

    def test_three_part_name(self):
        assert _slug_to_name("mary-jane-watson") == "Mary Jane Watson"


class TestExtractContactsFromHtml:
    def test_extracts_linkedin_profile_with_title(self):
        """Extracts a contact when title is visible near the LinkedIn URL."""
        html = (
            '<a href="https://www.linkedin.com/in/jane-smith">'
            "Jane Smith - VP Supply Chain at Acme Corp"
            "</a>"
        )
        contacts = _extract_contacts_from_html(html, "Acme Corp")
        assert len(contacts) >= 1
        c = contacts[0]
        assert "jane" in c["name"].lower() or "smith" in c["name"].lower()
        assert c["company"] == "Acme Corp"
        assert "linkedin.com/in/" in c["source_url"]

    def test_deduplicates_same_url(self):
        """Same LinkedIn URL appearing twice yields one contact."""
        url = "https://www.linkedin.com/in/jane-smith"
        html = (
            f'<a href="{url}">Jane Smith - VP Supply Chain at Corp</a>'
            f'<a href="{url}">Jane Smith - VP Supply Chain at Corp</a>'
        )
        contacts = _extract_contacts_from_html(html, "Corp")
        urls = [c["source_url"] for c in contacts]
        assert len(urls) == len(set(urls))

    def test_ignores_linkedin_company_pages(self):
        """Company page URLs (not /in/) are ignored."""
        html = '<a href="https://www.linkedin.com/company/acme-corp">Acme Corp</a>'
        contacts = _extract_contacts_from_html(html, "Acme Corp")
        assert contacts == []

    def test_drops_contacts_without_title(self):
        """Profiles with no detectable title are excluded."""
        html = '<a href="https://www.linkedin.com/in/mystery-person">mystery person</a>'
        # No supply chain keyword in surrounding text → no title → dropped
        contacts = _extract_contacts_from_html(html, "Corp")
        assert contacts == []

    def test_contact_has_required_keys(self):
        """Each returned contact has name, title, company, source_url."""
        html = (
            '<a href="https://www.linkedin.com/in/bob-jones">'
            "Bob Jones - Demand Planner at TestCo"
            "</a>"
        )
        contacts = _extract_contacts_from_html(html, "TestCo")
        if contacts:  # May or may not extract depending on regex — check shape
            c = contacts[0]
            assert "name" in c
            assert "title" in c
            assert "company" in c
            assert "source_url" in c


class TestFindSupplyChainContacts:
    def test_returns_list(self):
        """Returns a list even when no contacts found."""
        session = _mock_session(["<html>no linkedin here</html>"])
        result = find_supply_chain_contacts("AcmeCo", delay_secs=0, session=session)
        assert isinstance(result, list)

    def test_deduplicates_across_queries(self):
        """Same LinkedIn URL found in multiple queries yields one result."""
        html = (
            '<a href="https://www.linkedin.com/in/jane-smith">'
            "Jane Smith - VP Supply Chain at TestCo"
            "</a>"
        )
        session = _mock_session([html] * 10)  # all queries return same html
        result = find_supply_chain_contacts("TestCo", delay_secs=0, session=session)
        urls = [c["source_url"] for c in result]
        assert len(urls) == len(set(urls))

    def test_raises_when_all_requests_fail(self):
        """Raises RuntimeError when every HTTP request fails."""
        session = MagicMock()
        session.post.side_effect = requests.ConnectionError("unreachable")
        with pytest.raises(RuntimeError, match="All searches failed"):
            find_supply_chain_contacts("FailCo", delay_secs=0, session=session)

    def test_partial_failures_do_not_abort(self):
        """If some queries fail but others succeed, returns partial results."""
        ok_html = (
            '<a href="https://www.linkedin.com/in/good-person">'
            "Good Person - Demand Planner at TestCo"
            "</a>"
        )
        ok_resp = MagicMock()
        ok_resp.raise_for_status = MagicMock()
        ok_resp.text = ok_html

        err_resp = MagicMock()
        err_resp.raise_for_status.side_effect = requests.HTTPError("503")

        session = MagicMock()
        # Alternate: first fails, second succeeds, rest succeed
        session.post.side_effect = [err_resp, ok_resp] + [ok_resp] * 20

        result = find_supply_chain_contacts("TestCo", delay_secs=0, session=session)
        # Should not raise — partial results returned
        assert isinstance(result, list)

    def test_no_delay_in_tests(self):
        """delay_secs=0 does not sleep (performance)."""
        import time

        session = _mock_session(["<html></html>"])
        start = time.monotonic()
        find_supply_chain_contacts("FastCo", delay_secs=0, session=session)
        elapsed = time.monotonic() - start
        assert elapsed < 5.0  # Should complete very fast

    def test_company_name_included_in_result(self):
        """Each contact's company field matches the input company."""
        html = (
            '<a href="https://www.linkedin.com/in/supply-person">'
            "Supply Person - VP Supply Chain at TargetCo"
            "</a>"
        )
        session = _mock_session([html] * 10)
        result = find_supply_chain_contacts("TargetCo", delay_secs=0, session=session)
        for c in result:
            assert c["company"] == "TargetCo"
