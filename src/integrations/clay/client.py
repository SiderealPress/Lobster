"""
Clay.com API Client for Kissinger CRM enrichment.
==================================================

Clay is a waterfall enrichment platform that aggregates 100+ data sources
(LinkedIn, Apollo, Clearbit, Hunter, PDL, etc.) to achieve ~95% email find rate.

## API Model (as of 2026) — CONFIRMED FROM OFFICIAL DOCS

Clay does NOT have a traditional public REST API for standard plan users.

### What the CLAY_API_KEY actually is

The key from app.clay.com/settings/api is a **webhook verification token** —
used to authenticate webhooks Clay SENDS TO YOU (so you can verify Clay is
the sender). It is NOT a credential for calling Clay's API directly.

### Plan requirements for direct API

  - `api.clay.com/v3/sources/people/*`  — **Enterprise plan only**
  - The key returns HTTP 401 ("You must be logged in") or HTTP 404
    ("Invalid URL") on standard plans regardless of auth header format.
  - Confirmed via official docs: https://university.clay.com/docs/using-clay-as-an-api

### Standard-plan approach: webhook-table model

The correct programmatic approach on standard plans:

  1. Create a Clay table with enrichment columns (waterfall email finder,
     LinkedIn enrichment, etc.)
  2. Configure an HTTP Action on the table to POST enriched results to a
     callback URL (e.g. a Lobster endpoint)
  3. Push contacts into the table via CLAY_WEBHOOK_URL (table settings →
     Webhook URL in Clay UI)
  4. Clay runs its waterfall enrichment asynchronously
  5. Results arrive at the callback URL

### Methods in this client

  `lookup_by_email` / `lookup_by_linkedin` / `lookup_by_name`:
    Attempt direct REST lookup — **only works on Enterprise plan**.
    Raises ClayError with status_code=401 on standard plan (auth failure).
    The caller (enrich_contact.py, enrich.py) must treat 401 as
    "plan limitation — skip" rather than "no data found".

  `enrich_via_webhook(contacts, webhook_url)`:
    Standard-plan batch enrichment. Requires CLAY_WEBHOOK_URL env var.
    Push contacts → Clay enriches → results POST back to your callback URL.

## Epistemic standards

- All Clay-sourced data tagged `clay-enriched` on entity
- Provenance key: `clay`
- Confidence: 0.75 (slightly below Apollo's 0.8 — waterfall aggregation
  means Clay may pull from lower-confidence secondary sources)
- If Clay data conflicts with existing Apollo data: keep Apollo, log discrepancy
- Inferred edges (colleague relationships): inferred=true,
  how_they_know="Colleague inference (Clay)"

## Usage

    from integrations.clay.client import ClayClient, ClayPlanError

    client = ClayClient()  # reads CLAY_API_KEY from env / config.env

    # Lookup by email (Enterprise direct API — raises ClayPlanError on standard plan)
    try:
        person = client.lookup_by_email("jane@example.com")
    except ClayPlanError:
        pass  # standard plan — use webhook model instead

    # Webhook-table model (all plans): push batch to Clay table
    # Requires CLAY_WEBHOOK_URL env var to be set to the Clay table webhook URL
    client.enrich_via_webhook([{"email": "jane@example.com", "name": "Jane Smith"}])
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ── Constants ─────────────────────────────────────────────────────────────────

CONFIG_ENV = str(Path.home() / "lobster-config/config.env")

CLAY_API_BASE    = "https://api.clay.com/v3"
CLAY_PEOPLE_BASE = f"{CLAY_API_BASE}/sources/people"
CLAY_COMPANY_URL = f"{CLAY_API_BASE}/sources/companies/search-by-domain"

CLAY_CONFIDENCE  = 0.75   # Slightly below Apollo (0.8) — waterfall may use secondary sources
CLAY_TAG         = "clay-enriched"
CLAY_PROV_SOURCE = "clay"
CLAY_RELIABILITY_TIER = 2  # medium, same as Apollo

# Rate limits per manifest.json: 20 req/min, 2000/day
CLAY_RATE_LIMIT_SLEEP = 3.1  # seconds between calls (20/min = 3s per call, +0.1 buffer)


# ── Response dataclasses ───────────────────────────────────────────────────────

@dataclass
class ClayPerson:
    """Typed representation of a Clay person lookup response."""
    # Core identity
    name: str = ""
    first_name: str = ""
    last_name: str = ""
    email: str = ""
    # Professional
    title: str = ""
    org: str = ""
    seniority: str = ""
    departments: str = ""
    # Contact
    linkedin_url: str = ""
    phone: str = ""
    twitter_url: str = ""
    # Location
    city: str = ""
    state: str = ""
    country: str = ""
    # Clay metadata
    clay_id: str = ""
    confidence_score: float = 0.0
    data_sources: list[str] = field(default_factory=list)
    # Raw response for debugging
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_response(cls, data: dict[str, Any]) -> "ClayPerson":
        """Parse a Clay API person response into a typed object.

        Clay's response structure mirrors Apollo in many fields but may
        nest data under 'person', 'data', or at the top level depending
        on the endpoint.
        """
        # Normalise — Clay may wrap under 'person' or 'data'
        p = data
        if "person" in data:
            p = data["person"]
        elif "data" in data and isinstance(data["data"], dict):
            p = data["data"]

        def _str(key: str) -> str:
            v = p.get(key, "")
            if isinstance(v, list):
                return ", ".join(str(x) for x in v if x)
            return str(v).strip() if v else ""

        # Departments: Clay may return as list or string
        depts_raw = p.get("departments", p.get("department", ""))
        if isinstance(depts_raw, list):
            departments = ", ".join(str(d) for d in depts_raw if d)
        else:
            departments = str(depts_raw).strip() if depts_raw else ""

        # Data sources: which sub-providers Clay used
        sources_raw = p.get("_sources", p.get("sources", []))
        if isinstance(sources_raw, list):
            data_sources = [str(s) for s in sources_raw if s]
        else:
            data_sources = []

        return cls(
            name=_str("name") or f"{_str('first_name')} {_str('last_name')}".strip(),
            first_name=_str("first_name"),
            last_name=_str("last_name"),
            email=_str("email") or _str("work_email"),
            title=_str("title") or _str("job_title"),
            org=_str("organization_name") or _str("company") or _str("employer"),
            seniority=_str("seniority") or _str("seniority_level"),
            departments=departments,
            linkedin_url=_str("linkedin_url") or _str("linkedin"),
            phone=_str("phone") or _str("mobile_phone"),
            twitter_url=_str("twitter_url") or _str("twitter"),
            city=_str("city"),
            state=_str("state"),
            country=_str("country"),
            clay_id=_str("id") or _str("clay_id"),
            confidence_score=float(p.get("confidence_score", p.get("confidence", 0)) or 0),
            data_sources=data_sources,
            raw=data,
        )

    def is_empty(self) -> bool:
        """True if Clay returned no useful data."""
        return not any([self.name, self.email, self.linkedin_url, self.title])

    def to_meta_fields(self) -> dict[str, str]:
        """Return a dict of non-empty fields suitable for Kissinger entity meta."""
        fields: dict[str, str] = {}
        if self.email:       fields["email"]        = self.email
        if self.title:       fields["title"]        = self.title
        if self.org:         fields["org"]          = self.org
        if self.seniority:   fields["seniority"]    = self.seniority
        if self.departments: fields["departments"]  = self.departments
        if self.linkedin_url:fields["linkedin_url"] = self.linkedin_url
        if self.phone:       fields["phone"]        = self.phone
        if self.twitter_url: fields["twitter_url"]  = self.twitter_url
        if self.city:        fields["city"]         = self.city
        if self.state:       fields["state"]        = self.state
        if self.country:     fields["country"]      = self.country
        if self.clay_id:     fields["clay_id"]      = self.clay_id
        if self.first_name:  fields["first_name"]   = self.first_name
        if self.last_name:   fields["last_name"]    = self.last_name
        return fields


@dataclass
class ClayError(Exception):
    """Error from Clay API."""
    message: str
    status_code: int = 0

    def __str__(self) -> str:
        if self.status_code:
            return f"Clay API error {self.status_code}: {self.message}"
        return f"Clay API error: {self.message}"


@dataclass
class ClayPlanError(ClayError):
    """
    Raised when the Clay API request fails due to plan limitations.

    HTTP 401 ("You must be logged in") indicates the CLAY_API_KEY is a webhook
    verification token, not a direct-API credential. Direct person/company
    lookups via api.clay.com/v3/sources require an Enterprise plan.

    Callers should catch this and fall back to the webhook-table model
    (enrich_via_webhook) rather than treating it as "no data found".
    """
    def __str__(self) -> str:
        return (
            f"Clay plan limitation (HTTP {self.status_code}): {self.message}\n"
            "Direct lookups via api.clay.com/v3/sources require an Enterprise plan.\n"
            "The CLAY_API_KEY is a webhook verification token, not a direct-API key.\n"
            "Use enrich_via_webhook() with CLAY_WEBHOOK_URL for standard-plan enrichment.\n"
            "Docs: https://university.clay.com/docs/using-clay-as-an-api"
        )


# ── Client ────────────────────────────────────────────────────────────────────

class ClayClient:
    """
    Clay.com enrichment API client.

    Mirrors the Apollo client pattern (urllib.request, no third-party deps).
    Auth: X-Clay-API-Key header (direct API) or Bearer (webhook model).

    On first call, verifies the API key is present. Does not validate against
    Clay until an actual lookup is made.
    """

    def __init__(self, api_key: str = "") -> None:
        self.api_key = api_key or _load_api_key()
        if not self.api_key:
            raise ClayError(
                "CLAY_API_KEY not found in environment or ~/lobster-config/config.env. "
                "Get a key at https://app.clay.com/settings/api"
            )

    # ── Internal HTTP helpers ─────────────────────────────────────────────────

    def _post(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        """POST JSON to a Clay endpoint, return parsed JSON response."""
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={
                "Content-Type": "application/json",
                "X-Clay-API-Key": self.api_key,
                # Also send as Bearer for forward-compat with newer Clay auth model
                "Authorization": f"Bearer {self.api_key}",
                "User-Agent": "kissinger-clay/1.0 (+https://github.com/SiderealPress/lobster)",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read()
                return json.loads(body)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            raise ClayError(
                f"HTTP {e.code} from {url}: {body[:500]}",
                status_code=e.code,
            ) from e
        except urllib.error.URLError as e:
            raise ClayError(f"Network error contacting Clay: {e.reason}") from e
        except json.JSONDecodeError as e:
            raise ClayError(f"Clay returned invalid JSON: {e}") from e

    def _rate_limit(self) -> None:
        """Simple courtesy sleep to stay within Clay's 20 req/min limit."""
        time.sleep(CLAY_RATE_LIMIT_SLEEP)

    # ── Person lookup endpoints ───────────────────────────────────────────────

    def lookup_by_email(self, email: str) -> ClayPerson | None:
        """
        Look up a person by their email address.

        Uses Clay's people/search-by-email endpoint. Enterprise plan only.

        Returns None if Clay has no data for this email (empty response).
        Raises ClayPlanError on 401/402/404 — these all indicate plan limitations
        (standard plan key returns 401 "You must be logged in"; the endpoint
        itself returns 404 on standard plan before auth is even checked).

        Clay confidence for email lookups is high (~0.9 sub-source accuracy).
        """
        if not email or not email.strip():
            return None

        url = f"{CLAY_PEOPLE_BASE}/search-by-email"
        try:
            resp = self._post(url, {"email": email.strip().lower()})
            return _parse_person_response(resp)
        except ClayError as e:
            if e.status_code in (401, 402, 404):
                raise ClayPlanError(
                    f"Direct person lookup requires Enterprise plan (got HTTP {e.status_code}). "
                    "CLAY_API_KEY is a webhook verification token, not a direct-API credential.",
                    status_code=e.status_code,
                ) from e
            raise
        finally:
            self._rate_limit()

    def lookup_by_linkedin(self, linkedin_url: str) -> ClayPerson | None:
        """
        Look up a person by their LinkedIn URL.

        Normalises the URL to remove tracking params before sending.
        """
        if not linkedin_url or not linkedin_url.strip():
            return None

        # Normalise: remove query strings, ensure https
        url_clean = linkedin_url.strip().split("?")[0].rstrip("/")
        if not url_clean.startswith("http"):
            url_clean = "https://" + url_clean

        endpoint = f"{CLAY_PEOPLE_BASE}/search-by-linkedin"
        try:
            resp = self._post(endpoint, {"linkedin_url": url_clean})
            return _parse_person_response(resp)
        except ClayError as e:
            if e.status_code in (401, 402, 404):
                raise ClayPlanError(
                    f"Direct person lookup requires Enterprise plan (got HTTP {e.status_code}). "
                    "CLAY_API_KEY is a webhook verification token, not a direct-API credential.",
                    status_code=e.status_code,
                ) from e
            raise
        finally:
            self._rate_limit()

    def lookup_by_name(
        self,
        name: str,
        company: str = "",
        title: str = "",
    ) -> ClayPerson | None:
        """
        Look up a person by name (+ optional company/title context).

        Name-based lookup is lower confidence than email or LinkedIn.
        Clay uses this to match against their waterfall sources.
        """
        if not name or not name.strip():
            return None

        payload: dict[str, Any] = {"name": name.strip()}
        if company:
            payload["company"] = company.strip()
        if title:
            payload["title"] = title.strip()

        endpoint = f"{CLAY_PEOPLE_BASE}/search-by-name"
        try:
            resp = self._post(endpoint, payload)
            return _parse_person_response(resp)
        except ClayError as e:
            if e.status_code in (401, 402, 404):
                raise ClayPlanError(
                    f"Direct person lookup requires Enterprise plan (got HTTP {e.status_code}). "
                    "CLAY_API_KEY is a webhook verification token, not a direct-API credential.",
                    status_code=e.status_code,
                ) from e
            raise
        finally:
            self._rate_limit()

    def lookup_company(self, domain: str) -> dict[str, Any] | None:
        """
        Look up a company by domain using Clay's company enrichment endpoint.

        Returns raw dict with keys: name, domain, industry, employee_count,
        funding_stage, linkedin_url, description, tech_stack, etc.
        Returns None if not found.
        """
        if not domain or not domain.strip():
            return None

        # Strip protocol if present
        domain_clean = domain.strip().lower()
        for prefix in ("https://", "http://", "www."):
            if domain_clean.startswith(prefix):
                domain_clean = domain_clean[len(prefix):]

        try:
            resp = self._post(CLAY_COMPANY_URL, {"domain": domain_clean})
            if not resp or _is_empty_response(resp):
                return None
            return resp
        except ClayError as e:
            if e.status_code in (401, 402, 404):
                raise ClayPlanError(
                    f"Direct company lookup requires Enterprise plan (got HTTP {e.status_code}). "
                    "CLAY_API_KEY is a webhook verification token, not a direct-API credential.",
                    status_code=e.status_code,
                ) from e
            raise
        finally:
            self._rate_limit()

    def enrich_via_webhook(
        self,
        contacts: list[dict[str, Any]],
        webhook_url: str = "",
    ) -> dict[str, Any]:
        """
        Push contacts into a Clay table via its webhook URL for batch enrichment.

        Clay will run its waterfall enrichment and POST results back to the
        HTTP Action callback URL configured on the table.

        Requires CLAY_WEBHOOK_URL env var (or explicit webhook_url param) to be
        set to the Clay table's webhook URL (from Clay UI: table settings → webhook).

        Each contact dict should have at least one of:
          - email
          - linkedin_url
          - name + company (for name-based lookup)

        Returns dict with: submitted (int), error (str | None).
        """
        url = webhook_url or os.environ.get("CLAY_WEBHOOK_URL", "").strip()
        if not url:
            return {
                "submitted": 0,
                "error": (
                    "CLAY_WEBHOOK_URL not set. Set this to your Clay table's webhook URL "
                    "(Clay UI: table → Settings → Webhook URL). "
                    "See: https://university.clay.com/docs/http-api-integration-overview"
                ),
            }

        # Clay webhooks accept a JSON array of row objects
        data = json.dumps(contacts).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={
                "Content-Type": "application/json",
                "X-Clay-API-Key": self.api_key,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                return {
                    "submitted": len(contacts),
                    "http_status": resp.status,
                    "response": body[:500],
                    "error": None,
                }
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            return {
                "submitted": 0,
                "http_status": e.code,
                "error": f"HTTP {e.code}: {body[:300]}",
            }
        except Exception as e:
            return {
                "submitted": 0,
                "error": str(e),
            }

    def smoke_test(self) -> dict[str, Any]:
        """
        Verify API key and connectivity.

        Attempts a direct person lookup. On standard (non-Enterprise) plans,
        Clay returns HTTP 401/404 (ClayPlanError) — the CLAY_API_KEY is a
        webhook verification token, not a direct-API credential. This is a
        plan limitation, NOT a broken key.

        Returns dict with keys:
          ok (bool) — True if API is reachable
          api_reachable (bool) — True if we got any HTTP response
          plan_tier (str) — "enterprise", "standard", or "unknown"
          person (ClayPerson|None)
          error (str|None)
          webhook_model_available (bool) — True if CLAY_WEBHOOK_URL is set
        """
        test_email = "satya.nadella@microsoft.com"
        webhook_url = os.environ.get("CLAY_WEBHOOK_URL", "").strip()
        try:
            person = self.lookup_by_email(test_email)
            plan_tier = "enterprise" if person else "enterprise_no_data"
            return {
                "ok": True,
                "api_reachable": True,
                "plan_tier": plan_tier,
                "person": person,
                "test_email": test_email,
                "error": None,
                "webhook_model_available": bool(webhook_url),
            }
        except ClayPlanError as e:
            # Standard plan — direct API blocked, but this is expected.
            # Key is valid as a webhook verification token.
            return {
                "ok": True,   # key exists, API reachable — plan is just standard
                "api_reachable": True,
                "plan_tier": "standard",
                "person": None,
                "test_email": test_email,
                "error": (
                    f"Direct lookup requires Enterprise plan (HTTP {e.status_code}). "
                    "CLAY_API_KEY is a webhook verification token. "
                    "Set CLAY_WEBHOOK_URL to use the webhook-table model."
                ),
                "webhook_model_available": bool(webhook_url),
            }
        except ClayError as e:
            return {
                "ok": False,
                "api_reachable": e.status_code != 0,
                "plan_tier": "unknown",
                "person": None,
                "test_email": test_email,
                "error": str(e),
                "webhook_model_available": bool(webhook_url),
            }


# ── Helpers ────────────────────────────────────────────────────────────────────

def _load_api_key() -> str:
    """Load CLAY_API_KEY from environment or ~/lobster-config/config.env."""
    key = os.environ.get("CLAY_API_KEY", "").strip()
    if key:
        return key
    try:
        with open(CONFIG_ENV) as f:
            for line in f:
                line = line.strip()
                if line.startswith("CLAY_API_KEY="):
                    return line.split("=", 1)[1].strip()
    except FileNotFoundError:
        pass
    return ""


def _is_empty_response(resp: dict[str, Any]) -> bool:
    """True if Clay returned a response indicating no data found."""
    # Clay may return {"data": null}, {"person": null}, {"error": "not found"}, etc.
    if not resp:
        return True
    if resp.get("data") is None and resp.get("person") is None and "error" not in resp:
        return True
    if isinstance(resp.get("error"), str) and "not found" in resp["error"].lower():
        return True
    return False


def _parse_person_response(resp: dict[str, Any]) -> ClayPerson | None:
    """Parse a Clay person response, returning None if no useful data."""
    if not resp or _is_empty_response(resp):
        return None
    person = ClayPerson.from_response(resp)
    if person.is_empty():
        return None
    return person
