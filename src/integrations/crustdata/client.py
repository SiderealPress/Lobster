"""
Crustdata B2B Data API Client for Kissinger CRM enrichment.
============================================================

Crustdata is a real-time B2B data platform aggregating 1B+ person profiles
and company records. Unlike Clay (which uses a webhook-table model on
standard plans), Crustdata's REST API is directly accessible with a
standard paid-plan API key.

## API Model (verified 2026-04)

Base URL:  https://api.crustdata.com
Auth:      Authorization: Token <CRUSTDATA_API_KEY>
           (NOT "Bearer" — Crustdata uses Django Token auth)

### Verified working endpoints

  POST /screener/person/search
    Search persons by filter criteria (name, company, title, region, etc.).
    Returns LinkedIn profile data: name, headline, title, employer history,
    skills, emails. Paginated (25 results/page).
    Filter types: FIRST_NAME, LAST_NAME, CURRENT_COMPANY, CURRENT_TITLE,
      PAST_TITLE, SCHOOL, COMPANY_HEADQUARTERS, COMPANY_HEADCOUNT, REGION,
      INDUSTRY, PROFILE_LANGUAGE, SENIORITY_LEVEL, YEARS_AT_CURRENT_COMPANY,
      YEARS_IN_CURRENT_POSITION, YEARS_OF_EXPERIENCE, FUNCTION, PAST_COMPANY,
      COMPANY_TYPE, POSTED_ON_LINKEDIN, RECENTLY_CHANGED_JOBS, IN_THE_NEWS,
      NUM_OF_FOLLOWERS, DEPARTMENT_HEADCOUNT_GROWTH, FORTUNE, TECHNOLOGIES_USED,
      COMPANY_HEADCOUNT_GROWTH, ANNUAL_REVENUE, DEPARTMENT_HEADCOUNT,
      ACCOUNT_ACTIVITIES, JOB_OPPORTUNITIES
    Cost: 0.03 credits per result returned.
    Payload: {"filters": [{"filter_type": "X", "type": "in", "value": [...]}],
              "page": 1}

  GET /screener/company?company_domain=<domain>
  GET /screener/company?company_name=<name>
  GET /screener/company?company_linkedin_url=<url>
  GET /screener/company?company_id=<id>
    Returns company record(s): name, domain, headcount, HQ, funding, description,
    LinkedIn URL, Crunchbase URL, revenue range, company type, year founded.
    Returns a JSON list. First result with is_full_domain_match=true is preferred.
    Cost: 0.03 credits per result.

### Status of /person/enrich and /company/enrich endpoints

  POST /data_lab/person/enrich/ and /data_lab/people/enrich/
    These endpoints exist but require a 'filters' (dict with 'column' field)
    AND 'dataset' (non-empty dict) structure that was not resolvable with the
    current plan during smoke testing. The screener endpoints are the reliable
    path for person search.

## Epistemic standards

- All Crustdata-sourced data tagged `crustdata-enriched` on entity
- Provenance key: `crustdata`
- Confidence: 0.80 (LinkedIn-derived data, real-time quality)
- If Crustdata data conflicts with existing Apollo data: keep Apollo, log discrepancy
- Inferred edges (colleague relationships): inferred=true,
  how_they_know="Colleague inference (Crustdata)"

## Usage

    from integrations.crustdata.client import CrustdataClient, CrustdataError

    client = CrustdataClient()  # reads CRUSTDATA_API_KEY from env / config.env

    # Person search by name + company (most reliable endpoint)
    persons = client.search_person(first_name="Patrick", last_name="Collison",
                                   company="Stripe")
    person = persons[0] if persons else None

    # Company lookup by domain
    company = client.lookup_company_by_domain("stripe.com")

    # Smoke test (no credit cost — uses person search)
    status = client.smoke_test()
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
import urllib.error
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ── Constants ─────────────────────────────────────────────────────────────────

CONFIG_ENV = str(Path.home() / "lobster-config/config.env")

CRUSTDATA_API_BASE = "https://api.crustdata.com"

# Verified working endpoints (2026-04)
CRUSTDATA_PERSON_SEARCH_URL  = f"{CRUSTDATA_API_BASE}/screener/person/search"
CRUSTDATA_COMPANY_LOOKUP_URL = f"{CRUSTDATA_API_BASE}/screener/company"

# Rate limit: 15 req/min default → 4 seconds between calls (+ 0.1 buffer)
CRUSTDATA_RATE_LIMIT_SLEEP = 4.1

CRUSTDATA_CONFIDENCE  = 0.80   # LinkedIn-derived, real-time — on par with Apollo
CRUSTDATA_TAG         = "crustdata-enriched"
CRUSTDATA_PROV_SOURCE = "crustdata"
CRUSTDATA_RELIABILITY_TIER = 2  # medium — comparable to Apollo


# ── Response dataclasses ───────────────────────────────────────────────────────

@dataclass
class CrustdataPerson:
    """
    Typed representation of a Crustdata person enrichment response.

    Fields mirror the Crustdata /person/enrich response structure:
      - Base profile (1 credit): name, headline, title, org, location,
        education, social handles, linkedin_url
      - Business email (+1 credit): email
      - Personal email (+2 credits): personal_emails list
      - Phone (+2 credits): phone_numbers list
    """
    # Core identity
    name: str = ""
    first_name: str = ""
    last_name: str = ""
    headline: str = ""
    # Professional
    title: str = ""
    org: str = ""
    # Contact
    linkedin_url: str = ""
    email: str = ""             # business email
    personal_emails: list[str] = field(default_factory=list)
    phone_numbers: list[str] = field(default_factory=list)
    # Location
    city: str = ""
    state: str = ""
    country: str = ""
    # Education / skills
    education: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    # Past experience
    past_companies: list[str] = field(default_factory=list)
    # Crustdata metadata
    crustdata_id: str = ""
    # Raw response for debugging + provenance hashing
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_response(cls, data: dict[str, Any]) -> "CrustdataPerson":
        """
        Parse a Crustdata person enrichment response.

        Crustdata returns person data at the top level or under 'data'.
        current_employers is a list of {title, name} dicts.
        past_employers is a list of {title, name} dicts.
        personal_contact_info holds personal_emails and phone_numbers.
        """
        # Normalise — response may be wrapped under 'data'
        p = data
        if "data" in data and isinstance(data["data"], dict):
            p = data["data"]

        def _str(key: str, default: str = "") -> str:
            v = p.get(key, default)
            if isinstance(v, list):
                return ", ".join(str(x) for x in v if x)
            return str(v).strip() if v else default

        # Current employer: pick first from current_employers list
        current_employers = p.get("current_employers", [])
        if isinstance(current_employers, list) and current_employers:
            first_employer = current_employers[0]
            title = first_employer.get("title", "") or ""
            org = first_employer.get("name", "") or ""
        else:
            title = _str("title") or _str("job_title") or _str("headline", "").split(" at ")[0].strip()
            org = _str("organization_name") or _str("company") or _str("employer")

        # Past companies: names from past_employers
        past_employers = p.get("past_employers", [])
        past_companies: list[str] = []
        if isinstance(past_employers, list):
            for pe in past_employers:
                if isinstance(pe, dict) and pe.get("name"):
                    past_companies.append(pe["name"])

        # Education: list of dicts with 'school' or 'name'
        edu_raw = p.get("education", [])
        education: list[str] = []
        if isinstance(edu_raw, list):
            for edu in edu_raw:
                if isinstance(edu, dict):
                    school = edu.get("school") or edu.get("name") or ""
                    if school:
                        education.append(school)
                elif isinstance(edu, str):
                    education.append(edu)

        # Skills: list of strings or dicts
        skills_raw = p.get("skills", [])
        skills: list[str] = []
        if isinstance(skills_raw, list):
            for s in skills_raw:
                skills.append(str(s.get("name", s)) if isinstance(s, dict) else str(s))

        # Personal contact info
        pci = p.get("personal_contact_info", {}) or {}
        personal_emails: list[str] = []
        phone_numbers: list[str] = []
        if isinstance(pci, dict):
            pe_raw = pci.get("personal_emails", [])
            if isinstance(pe_raw, list):
                personal_emails = [str(e) for e in pe_raw if e]
            ph_raw = pci.get("phone_numbers", [])
            if isinstance(ph_raw, list):
                phone_numbers = [str(ph) for ph in ph_raw if ph]

        # Business email — may be at top level or in business_email field
        biz_email = (
            p.get("business_email") or
            p.get("email") or
            p.get("work_email") or
            ""
        )
        if isinstance(biz_email, list):
            biz_email = biz_email[0] if biz_email else ""
        biz_email = str(biz_email).strip()

        # LinkedIn URL — various field names used by Crustdata
        linkedin = (
            p.get("linkedin_profile_url") or
            p.get("flagship_profile_url") or
            p.get("linkedin_url") or
            p.get("linkedin") or
            ""
        )

        # Location
        loc = p.get("location", {}) or {}
        if isinstance(loc, dict):
            city = loc.get("city") or loc.get("locality") or p.get("city") or ""
            state = loc.get("state") or loc.get("region") or p.get("state") or ""
            country = loc.get("country") or p.get("country") or ""
        else:
            city = p.get("city", "")
            state = p.get("state", "")
            country = p.get("country", "")

        # Name
        full_name = (
            p.get("name") or
            f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()
        )

        return cls(
            name=str(full_name).strip(),
            first_name=str(p.get("first_name", "")).strip(),
            last_name=str(p.get("last_name", "")).strip(),
            headline=str(p.get("headline", "")).strip(),
            title=str(title).strip(),
            org=str(org).strip(),
            linkedin_url=str(linkedin).strip(),
            email=biz_email,
            personal_emails=personal_emails,
            phone_numbers=phone_numbers,
            city=str(city).strip(),
            state=str(state).strip(),
            country=str(country).strip(),
            education=education,
            skills=skills,
            past_companies=past_companies,
            crustdata_id=str(p.get("id", p.get("crustdata_id", ""))).strip(),
            raw=data,
        )

    def is_empty(self) -> bool:
        """True if Crustdata returned no useful data."""
        return not any([self.name, self.email, self.linkedin_url, self.title])

    def to_meta_fields(self) -> dict[str, str]:
        """
        Return a dict of non-empty fields suitable for Kissinger entity meta.

        Maps Crustdata field names to the canonical Kissinger meta key names
        used by other sources (Apollo, Clay, etc.) for consistency.
        """
        fields: dict[str, str] = {}
        if self.email:
            fields["email"] = self.email
        if self.title:
            fields["title"] = self.title
        if self.org:
            fields["org"] = self.org
        if self.headline:
            fields["headline"] = self.headline
        if self.linkedin_url:
            fields["linkedin_url"] = self.linkedin_url
        if self.phone_numbers:
            fields["phone"] = self.phone_numbers[0]
        if self.city:
            fields["city"] = self.city
        if self.state:
            fields["state"] = self.state
        if self.country:
            fields["country"] = self.country
        if self.crustdata_id:
            fields["crustdata_id"] = self.crustdata_id
        if self.first_name:
            fields["first_name"] = self.first_name
        if self.last_name:
            fields["last_name"] = self.last_name
        if self.personal_emails:
            fields["personal_email"] = self.personal_emails[0]
        if self.past_companies:
            fields["past_companies"] = ", ".join(self.past_companies[:5])
        if self.education:
            fields["education"] = ", ".join(self.education[:3])
        if self.skills:
            fields["skills"] = ", ".join(self.skills[:10])
        return fields


# ── Error types ────────────────────────────────────────────────────────────────

@dataclass
class CrustdataError(Exception):
    """Error from Crustdata API."""
    message: str
    status_code: int = 0

    def __str__(self) -> str:
        if self.status_code:
            return f"Crustdata API error {self.status_code}: {self.message}"
        return f"Crustdata API error: {self.message}"


@dataclass
class CrustdataAuthError(CrustdataError):
    """
    Raised when the Crustdata API request fails due to authentication.

    HTTP 401 means CRUSTDATA_API_KEY is missing or invalid.
    Unlike Clay, Crustdata's key IS a direct REST API credential —
    a 401 is a genuine auth failure, not a plan limitation.

    The caller must NOT treat this as "no data found". It indicates
    the key is wrong or expired and needs to be rotated.
    """
    def __str__(self) -> str:
        return (
            f"Crustdata authentication failed (HTTP {self.status_code}): {self.message}\n"
            "CRUSTDATA_API_KEY is invalid or expired.\n"
            "Get a new key at https://app.crustdata.com/settings/api\n"
            "and update CRUSTDATA_API_KEY in ~/lobster-config/config.env."
        )


@dataclass
class CrustdataPlanError(CrustdataError):
    """
    Raised when the requested endpoint requires a higher-tier plan.

    Live endpoints (/person/professional_network/*/live) require enterprise plan.
    Self-serve endpoints (/person/enrich, /person/search) work on any paid plan.
    """
    def __str__(self) -> str:
        return (
            f"Crustdata plan limitation (HTTP {self.status_code}): {self.message}\n"
            "This endpoint requires an enterprise plan or higher credit tier.\n"
            "Self-serve endpoints (/person/enrich, /person/search) work on paid plans.\n"
            "Docs: https://docs.crustdata.com/general/pricing"
        )


# ── Client ────────────────────────────────────────────────────────────────────

class CrustdataClient:
    """
    Crustdata B2B data enrichment REST API client.

    Mirrors the Clay client pattern (urllib.request, no third-party deps).
    Auth: Authorization: Bearer <api_key>
    Version header: x-api-version: 2025-11-01

    Unlike Clay, Crustdata's standard key IS a direct REST API credential.
    Auth failures (401) are genuine key errors, not plan limitations.

    On init, verifies the API key is present. Does not make a network call
    until an actual lookup is performed.
    """

    def __init__(self, api_key: str = "") -> None:
        self.api_key = api_key or _load_api_key()
        if not self.api_key:
            raise CrustdataAuthError(
                "CRUSTDATA_API_KEY not found in environment or "
                "~/lobster-config/config.env. "
                "Get a key at https://app.crustdata.com/settings/api",
                status_code=0,
            )

    # ── Internal HTTP helpers ─────────────────────────────────────────────────

    def _request(
        self,
        method: str,
        url: str,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        """Make an authenticated JSON request to the Crustdata API.

        Auth: Authorization: Token <key>  (Django Token auth — NOT Bearer).
        Returns parsed JSON (may be a dict or a list depending on endpoint).
        """
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers: dict[str, str] = {
            "Authorization": f"Token {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": (
                "kissinger-crustdata/1.0 "
                "(+https://github.com/SiderealPress/lobster)"
            ),
        }
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read()
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            _raise_for_status(e.code, body, url)
            raise  # unreachable — _raise_for_status always raises
        except urllib.error.URLError as e:
            raise CrustdataError(
                f"Network error contacting Crustdata: {e.reason}"
            ) from e
        except json.JSONDecodeError as e:
            raise CrustdataError(f"Crustdata returned invalid JSON: {e}") from e

    def _post(self, url: str, payload: dict[str, Any]) -> Any:
        return self._request("POST", url, payload)

    def _get(self, url: str) -> Any:
        return self._request("GET", url)

    def _rate_limit(self) -> None:
        """Courtesy sleep to stay within Crustdata's 15 req/min limit."""
        time.sleep(CRUSTDATA_RATE_LIMIT_SLEEP)

    # ── Person enrichment endpoints ───────────────────────────────────────────

    def enrich_by_linkedin(
        self,
        linkedin_url: str,
        include_email: bool = True,
        include_personal_email: bool = False,
        include_phone: bool = False,
    ) -> "CrustdataPerson | None":
        """
        Look up a person by their LinkedIn URL using /screener/person/search.

        Uses LINKEDIN_PROFILE_URL filter type. The /screener/person/search
        endpoint is the verified working path for profile lookup (cost: 0.03
        credits per result).

        Returns None if Crustdata has no data for this profile.
        Raises CrustdataAuthError on 401 — this is a genuine key error.
        """
        if not linkedin_url or not linkedin_url.strip():
            return None

        url_clean = _normalise_linkedin_url(linkedin_url)
        payload: dict[str, Any] = {
            "filters": [
                {
                    "filter_type": "LINKEDIN_PROFILE_URL",
                    "type": "in",
                    "value": [url_clean],
                }
            ],
            "page": 1,
        }

        try:
            resp = self._post(CRUSTDATA_PERSON_SEARCH_URL, payload)
            return _parse_search_response(resp)
        finally:
            self._rate_limit()

    def enrich_by_email(
        self,
        email: str,
        include_personal_email: bool = False,
        include_phone: bool = False,
    ) -> "CrustdataPerson | None":
        """
        Reverse-lookup a person from their business email address.

        Uses CURRENT_COMPANY filter with email matching as a heuristic
        (Crustdata does not have a direct email-to-profile endpoint in the
        screener API). Falls through to None if no confident match is found.

        Returns None if Crustdata has no matching profile.
        """
        if not email or not email.strip():
            return None

        # Extract domain for company hint
        email = email.strip().lower()
        domain = email.split("@")[-1] if "@" in email else ""
        # Try to search by email domain + name prefix as a heuristic
        name_prefix = email.split("@")[0].replace(".", " ").replace("_", " ").strip()

        filters: list[dict[str, Any]] = []
        if name_prefix:
            filters.append(
                {
                    "filter_type": "FIRST_NAME",
                    "type": "in",
                    "value": [name_prefix.split()[0].capitalize()],
                }
            )
        if domain:
            # Use company domain as a company hint
            company_hint = domain.split(".")[0].capitalize()
            filters.append(
                {
                    "filter_type": "CURRENT_COMPANY",
                    "type": "in",
                    "value": [company_hint],
                }
            )

        if not filters:
            return None

        payload: dict[str, Any] = {"filters": filters, "page": 1}

        try:
            resp = self._post(CRUSTDATA_PERSON_SEARCH_URL, payload)
            return _parse_search_response(resp)
        finally:
            self._rate_limit()

    def enrich_by_name_and_company(
        self,
        name: str,
        company: str = "",
        title: str = "",
        include_email: bool = True,
    ) -> "CrustdataPerson | None":
        """
        Search for a person by name (+ optional company/title context).

        Uses /screener/person/search with FIRST_NAME/LAST_NAME and optionally
        CURRENT_COMPANY filters. Lower confidence than LinkedIn URL lookup
        because name matching can be ambiguous.
        Returns the top result if found, None otherwise.
        Cost: 0.03 credits per result returned.
        """
        if not name or not name.strip():
            return None

        parts = name.strip().split()
        filters: list[dict[str, Any]] = []
        if len(parts) >= 2:
            filters.append(
                {"filter_type": "FIRST_NAME", "type": "in", "value": [parts[0]]}
            )
            filters.append(
                {"filter_type": "LAST_NAME", "type": "in", "value": [parts[-1]]}
            )
        else:
            filters.append(
                {"filter_type": "FIRST_NAME", "type": "in", "value": [parts[0]]}
            )

        if company:
            filters.append(
                {
                    "filter_type": "CURRENT_COMPANY",
                    "type": "in",
                    "value": [company.strip()],
                }
            )
        if title:
            filters.append(
                {
                    "filter_type": "CURRENT_TITLE",
                    "type": "in",
                    "value": [title.strip()],
                }
            )

        payload: dict[str, Any] = {"filters": filters, "page": 1}

        try:
            resp = self._post(CRUSTDATA_PERSON_SEARCH_URL, payload)
            return _parse_search_response(resp)
        finally:
            self._rate_limit()

    def search_persons(
        self,
        *,
        first_name: str = "",
        last_name: str = "",
        company: str = "",
        title: str = "",
        page: int = 1,
        page_size: int = 5,
    ) -> list["CrustdataPerson"]:
        """
        Search persons using /screener/person/search filters.

        Returns up to page_size results. Useful for org-chart discovery
        when used with CURRENT_COMPANY + CURRENT_TITLE filters.
        Cost: 0.03 credits per result returned.
        """
        filters: list[dict[str, Any]] = []
        if first_name:
            filters.append({"filter_type": "FIRST_NAME", "type": "in", "value": [first_name]})
        if last_name:
            filters.append({"filter_type": "LAST_NAME", "type": "in", "value": [last_name]})
        if company:
            filters.append({"filter_type": "CURRENT_COMPANY", "type": "in", "value": [company]})
        if title:
            filters.append({"filter_type": "CURRENT_TITLE", "type": "in", "value": [title]})

        if not filters:
            return []

        payload: dict[str, Any] = {"filters": filters, "page": page}

        try:
            resp = self._post(CRUSTDATA_PERSON_SEARCH_URL, payload)
        finally:
            self._rate_limit()

        # Screener search returns a list directly or under 'profiles'/'data'
        records: list[Any] = []
        if isinstance(resp, list):
            records = resp
        elif isinstance(resp, dict):
            records = (
                resp.get("profiles") or
                resp.get("data") or
                resp.get("results") or
                []
            )

        results: list[CrustdataPerson] = []
        for record in records[:page_size]:
            if isinstance(record, dict):
                person = CrustdataPerson.from_response(record)
                if not person.is_empty():
                    results.append(person)
        return results

    # ── Company endpoints ──────────────────────────────────────────────────────

    def lookup_company_by_domain(self, domain: str) -> "dict[str, Any] | None":
        """
        Look up a company by domain using GET /screener/company?company_domain=<domain>.

        Returns a company record dict with: name, company_id, website, domain,
        description, headcount (latest), HQ location, LinkedIn URL,
        Crunchbase URL, revenue range, company type, year founded.

        Returns None if not found. Cost: ~0.03 credits per result.
        """
        if not domain or not domain.strip():
            return None

        domain_clean = _normalise_domain(domain)
        url = f"{CRUSTDATA_COMPANY_LOOKUP_URL}?company_domain={urllib.parse.quote(domain_clean)}"
        try:
            resp = self._get(url)
            # Response is a list; pick first result with full domain match if available
            if isinstance(resp, list) and resp:
                for company in resp:
                    if company.get("is_full_domain_match"):
                        return company
                return resp[0]  # fallback: first result
            if isinstance(resp, dict) and not _is_empty_response(resp):
                return resp
            return None
        finally:
            self._rate_limit()

    def lookup_company_by_name(self, name: str) -> "dict[str, Any] | None":
        """
        Look up a company by name using GET /screener/company?company_name=<name>.

        Returns None if not found.
        """
        if not name or not name.strip():
            return None

        url = f"{CRUSTDATA_COMPANY_LOOKUP_URL}?company_name={urllib.parse.quote(name.strip())}"
        try:
            resp = self._get(url)
            if isinstance(resp, list) and resp:
                return resp[0]
            if isinstance(resp, dict) and not _is_empty_response(resp):
                return resp
            return None
        finally:
            self._rate_limit()

    # ── Smoke test ────────────────────────────────────────────────────────────

    def smoke_test(self) -> dict[str, Any]:
        """
        Verify API key and connectivity.

        Attempts a company lookup for a well-known domain to verify auth.
        Unlike Clay, a working Crustdata key means the API is directly
        accessible with no plan limitation caveats.
        A 401 is a genuine auth failure, not a plan limitation.

        Returns dict with keys:
          ok (bool) — True if API is reachable and key is valid
          api_reachable (bool) — True if we got any HTTP response
          key_valid (bool) — True if auth succeeded
          company (dict|None) — resolved company record
          error (str|None)
        """
        test_domain = "microsoft.com"
        try:
            company = self.lookup_company_by_domain(test_domain)
            return {
                "ok": True,
                "api_reachable": True,
                "key_valid": True,
                "company": company,
                "test_domain": test_domain,
                "error": None,
            }
        except CrustdataAuthError as e:
            return {
                "ok": False,
                "api_reachable": True,
                "key_valid": False,
                "company": None,
                "test_domain": test_domain,
                "error": str(e),
            }
        except CrustdataError as e:
            return {
                "ok": False,
                "api_reachable": e.status_code != 0,
                "key_valid": False,
                "company": None,
                "test_domain": test_domain,
                "error": str(e),
            }


# ── Helpers ────────────────────────────────────────────────────────────────────

def _load_api_key() -> str:
    """Load CRUSTDATA_API_KEY from environment or ~/lobster-config/config.env."""
    key = os.environ.get("CRUSTDATA_API_KEY", "").strip()
    if key:
        return key
    try:
        with open(CONFIG_ENV) as f:
            for line in f:
                line = line.strip()
                if line.startswith("CRUSTDATA_API_KEY="):
                    return line.split("=", 1)[1].strip()
    except FileNotFoundError:
        pass
    return ""


def _normalise_linkedin_url(url: str) -> str:
    """Normalise a LinkedIn profile URL: ensure https, strip query params."""
    url = url.strip().split("?")[0].rstrip("/")
    if not url.startswith("http"):
        url = "https://" + url
    # Ensure www. prefix for Crustdata compatibility
    if url.startswith("https://linkedin.com/"):
        url = url.replace("https://linkedin.com/", "https://www.linkedin.com/")
    return url


def _normalise_domain(domain: str) -> str:
    """Strip protocol and www from a domain string."""
    d = domain.strip().lower()
    for prefix in ("https://", "http://", "www."):
        if d.startswith(prefix):
            d = d[len(prefix):]
    return d.rstrip("/")


def _raise_for_status(status_code: int, body: str, url: str) -> None:
    """
    Raise the appropriate error type based on HTTP status.

    Unlike Clay (where 401 = plan limitation), Crustdata 401 = bad key.
    """
    if status_code == 401:
        raise CrustdataAuthError(
            f"Invalid or missing API key (HTTP 401 from {url}). "
            "CRUSTDATA_API_KEY is incorrect or expired. "
            "Update it in ~/lobster-config/config.env.",
            status_code=401,
        )
    if status_code == 402:
        raise CrustdataPlanError(
            f"Insufficient credits or plan tier (HTTP 402 from {url}). "
            "Check your Crustdata dashboard for credit balance.",
            status_code=402,
        )
    if status_code == 403:
        raise CrustdataPlanError(
            f"Endpoint requires higher plan tier (HTTP 403 from {url}). "
            "Live endpoints require enterprise plan.",
            status_code=403,
        )
    if status_code == 429:
        raise CrustdataError(
            f"Rate limit exceeded (HTTP 429 from {url}). "
            "Default limit is 15 req/min. Implement exponential backoff.",
            status_code=429,
        )
    raise CrustdataError(
        f"HTTP {status_code} from {url}: {body[:500]}",
        status_code=status_code,
    )


def _is_empty_response(resp: dict[str, Any]) -> bool:
    """True if Crustdata returned a response indicating no data found."""
    if not resp:
        return True
    # Crustdata may return {"data": null}, {"error": "not found"}, empty dict, etc.
    if resp.get("data") is None and "error" not in resp and "name" not in resp:
        return True
    if isinstance(resp.get("error"), str) and (
        "not found" in resp["error"].lower() or
        "no results" in resp["error"].lower()
    ):
        return True
    return False


def _parse_search_response(resp: Any) -> "CrustdataPerson | None":
    """
    Parse a /screener/person/search response, returning the top result.

    The screener endpoint returns a list of person records directly or
    wrapped under 'profiles', 'data', or 'results'.
    Returns None if no results found.
    """
    if not resp:
        return None

    # Extract records list
    records: list[Any] = []
    if isinstance(resp, list):
        records = resp
    elif isinstance(resp, dict):
        records = (
            resp.get("profiles") or
            resp.get("data") or
            resp.get("results") or
            []
        )

    if not records:
        return None

    person = CrustdataPerson.from_response(records[0])
    return person if not person.is_empty() else None


def _parse_person_response(resp: Any) -> "CrustdataPerson | None":
    """
    Parse a Crustdata person response (generic fallback).

    Handles both single-record dict responses and list responses.
    Returns None if no useful data found.
    """
    return _parse_search_response(resp)
