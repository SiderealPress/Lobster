"""
Crustdata B2B data enrichment integration.

Crustdata is a real-time B2B data platform with 1B+ person profiles and company data.
Provides person enrichment (by LinkedIn URL, email), person search, and company enrichment.

Self-serve plan:
  - /person/search    — 0.03 credits per result
  - /person/enrich    — 1–7 credits per record (base 1 + add-ons)
  - /company/search   — 0.03 credits per result
  - /company/enrich   — 2 credits per record
  - /company/identify — free (domain → company)

Live endpoints (enterprise/plan-gated):
  - /person/professional_network/enrich/live — 7 credits
  - /person/professional_network/search/live — 2 credits

Auth: Bearer token via Authorization header.
Rate limit: 15 requests per minute (default).

Modules:
    client   - CrustdataClient class + error types
"""
