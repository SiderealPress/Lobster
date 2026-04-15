"""
Tests for BIS-296: list_prospect_companies

Uses unittest.mock to intercept requests.post — no real HTTP calls.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add bin dir to path
sys.path.insert(
    0,
    str(Path(__file__).parent.parent.parent.parent
        / "lobster-shop" / "prospect-enrichment" / "bin"),
)

from list_prospect_companies import list_prospect_companies, _graphql


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_response(data: dict) -> MagicMock:
    """Build a mock requests.Response."""
    mock = MagicMock()
    mock.raise_for_status = MagicMock()
    mock.json.return_value = data
    return mock


def _connection_page(nodes: list[dict], has_next: bool, cursor: str = "cursor1"):
    """Build a GraphQL entities connection page."""
    return {
        "data": {
            "entities": {
                "pageInfo": {
                    "hasNextPage": has_next,
                    "endCursor": cursor if has_next else None,
                },
                "edges": [{"node": n} for n in nodes],
            }
        }
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestListProspectCompanies:
    def test_returns_only_prospect_tagged_orgs(self):
        """Only entities tagged 'prospect' are returned."""
        page = _connection_page(
            [
                {"id": "aaa", "name": "Acme Corp", "tags": ["prospect"]},
                {"id": "bbb", "name": "Ghost Co", "tags": ["customer"]},
                {"id": "ccc", "name": "Beta Inc", "tags": ["prospect", "tier-1"]},
            ],
            has_next=False,
        )
        with patch("requests.post", return_value=_make_response(page)):
            result = list_prospect_companies()

        assert len(result) == 2
        ids = {r["id"] for r in result}
        assert ids == {"aaa", "ccc"}

    def test_returns_empty_when_no_prospects(self):
        """Returns empty list when no orgs have the prospect tag."""
        page = _connection_page(
            [{"id": "aaa", "name": "Regular Co", "tags": ["customer"]}],
            has_next=False,
        )
        with patch("requests.post", return_value=_make_response(page)):
            result = list_prospect_companies()

        assert result == []

    def test_returns_empty_when_no_entities(self):
        """Returns empty list when entities connection is empty."""
        page = _connection_page([], has_next=False)
        with patch("requests.post", return_value=_make_response(page)):
            result = list_prospect_companies()

        assert result == []

    def test_paginates_through_all_pages(self):
        """Follows hasNextPage until exhausted."""
        page1 = _connection_page(
            [{"id": "aaa", "name": "First Co", "tags": ["prospect"]}],
            has_next=True,
            cursor="c1",
        )
        page2 = _connection_page(
            [{"id": "bbb", "name": "Second Co", "tags": ["prospect"]}],
            has_next=False,
        )
        responses = [_make_response(page1), _make_response(page2)]
        with patch("requests.post", side_effect=responses):
            result = list_prospect_companies()

        assert len(result) == 2
        assert {r["id"] for r in result} == {"aaa", "bbb"}

    def test_result_shape_matches_spec(self):
        """Each result has id, name, tags keys."""
        page = _connection_page(
            [{"id": "abc123", "name": "Prospect Corp", "tags": ["prospect", "hot"]}],
            has_next=False,
        )
        with patch("requests.post", return_value=_make_response(page)):
            result = list_prospect_companies()

        assert result == [{"id": "abc123", "name": "Prospect Corp", "tags": ["prospect", "hot"]}]

    def test_handles_null_tags(self):
        """Entities with null tags are not returned (can't be tagged prospect)."""
        page = _connection_page(
            [{"id": "aaa", "name": "No Tags Co", "tags": None}],
            has_next=False,
        )
        with patch("requests.post", return_value=_make_response(page)):
            result = list_prospect_companies()

        assert result == []

    def test_raises_on_graphql_errors(self):
        """GraphQL error payload raises RuntimeError."""
        error_resp = _make_response({"errors": [{"message": "DB offline"}]})
        with patch("requests.post", return_value=error_resp):
            with pytest.raises(RuntimeError, match="GraphQL errors"):
                list_prospect_companies()

    def test_raises_on_http_error(self):
        """HTTP 500 propagates as an exception."""
        import requests as rq

        mock = MagicMock()
        mock.raise_for_status.side_effect = rq.HTTPError("500 Server Error")
        with patch("requests.post", return_value=mock):
            with pytest.raises(rq.HTTPError):
                list_prospect_companies()

    def test_bearer_token_sent_in_header(self):
        """When token is set, Authorization header is included."""
        page = _connection_page([], has_next=False)
        with patch("requests.post", return_value=_make_response(page)) as mock_post:
            list_prospect_companies(token="secret-tok")

        call_kwargs = mock_post.call_args
        headers = call_kwargs.kwargs.get("headers") or call_kwargs[1].get("headers", {})
        assert headers.get("Authorization") == "Bearer secret-tok"

    def test_no_auth_header_when_no_token(self):
        """Without a token, Authorization header is absent."""
        page = _connection_page([], has_next=False)
        with patch("requests.post", return_value=_make_response(page)) as mock_post:
            list_prospect_companies(token="")

        call_kwargs = mock_post.call_args
        headers = call_kwargs.kwargs.get("headers") or call_kwargs[1].get("headers", {})
        assert "Authorization" not in headers
