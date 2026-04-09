"""
BIS-296: List Prospect Companies

Queries the Kissinger GraphQL API for all org entities tagged "prospect".
Returns a list of {id, name, tags} dicts.

Usage:
    python list_prospect_companies.py [--endpoint URL] [--token TOKEN]

Exits 0 and prints JSON array on success.
Exits 1 on error.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

import requests

KISSINGER_ENDPOINT = os.environ.get(
    "KISSINGER_ENDPOINT", "http://localhost:8080/graphql"
)
KISSINGER_API_TOKEN = os.environ.get("KISSINGER_API_TOKEN", "")

# Relay cursor pagination page size.
_PAGE_SIZE = 100

_ENTITIES_QUERY = """
query ListOrgEntities($first: Int, $after: String) {
  entities(kind: "org", first: $first, after: $after) {
    pageInfo {
      hasNextPage
      endCursor
    }
    edges {
      node {
        id
        name
        tags
      }
    }
  }
}
"""


def _graphql(
    query: str,
    variables: dict[str, Any],
    endpoint: str,
    token: str,
) -> dict[str, Any]:
    """Execute a GraphQL query against Kissinger. Raises on HTTP/network errors."""
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    resp = requests.post(
        endpoint,
        json={"query": query, "variables": variables},
        headers=headers,
        timeout=30,
    )
    resp.raise_for_status()
    payload = resp.json()
    if "errors" in payload:
        raise RuntimeError(f"GraphQL errors: {payload['errors']}")
    return payload["data"]


def list_prospect_companies(
    endpoint: str = KISSINGER_ENDPOINT,
    token: str = KISSINGER_API_TOKEN,
) -> list[dict[str, Any]]:
    """
    Fetch all org entities from Kissinger and return those tagged "prospect".

    Returns:
        List of dicts with keys: id, name, tags
    """
    results: list[dict[str, Any]] = []
    after: str | None = None

    while True:
        variables: dict[str, Any] = {"first": _PAGE_SIZE}
        if after is not None:
            variables["after"] = after

        data = _graphql(_ENTITIES_QUERY, variables, endpoint, token)
        connection = data["entities"]
        page_info = connection["pageInfo"]

        for edge in connection["edges"]:
            node = edge["node"]
            tags: list[str] = node.get("tags") or []
            if "prospect" in tags:
                results.append(
                    {
                        "id": node["id"],
                        "name": node["name"],
                        "tags": tags,
                    }
                )

        if not page_info["hasNextPage"]:
            break
        after = page_info["endCursor"]

    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="List prospect companies from Kissinger")
    parser.add_argument(
        "--endpoint",
        default=KISSINGER_ENDPOINT,
        help="Kissinger GraphQL endpoint (default: $KISSINGER_ENDPOINT or http://localhost:8080/graphql)",
    )
    parser.add_argument(
        "--token",
        default=KISSINGER_API_TOKEN,
        help="Bearer token (default: $KISSINGER_API_TOKEN)",
    )
    args = parser.parse_args()

    try:
        companies = list_prospect_companies(endpoint=args.endpoint, token=args.token)
        print(json.dumps(companies, indent=2))
    except Exception as exc:  # noqa: BLE001
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
