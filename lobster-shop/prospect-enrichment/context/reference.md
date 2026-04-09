# Prospect Enrichment — Technical Reference

## Architecture

Five pipeline stages, each implemented as a standalone Python script:

```
list_prospect_companies.py   (BIS-296)
         │
         ▼  [{id, name, tags}]
find_supply_chain_contacts.py (BIS-297)  ← web search per company
         │
         ▼  [{name, title, company, source_url}]
dedup_crm_contacts.py        (BIS-298)  ← Kissinger search + contactsAtOrg
         │
         ▼  {new: [...], duplicates: [...], fuzzy_matches: [...]}
add_contacts_provenance.py   (BIS-299)  ← createEntity + createEdge
         │
         ▼
org_chart_enrichment.py      (BIS-300)  ← orchestrates all four
```

## Kissinger GraphQL Endpoint

- URL: `http://localhost:8080/graphql`
- Auth: Bearer token from `$KISSINGER_API_TOKEN` (optional — passes through if unset)
- GET requests always allowed (no auth needed for introspection)
- POST mutations require token if `KISSINGER_API_TOKEN` is set in the Kissinger process environment

## BIS-296: list_prospect_companies

- Query: `entities(kind: "org", first: 100)` with Relay cursor pagination
- Filter: entities whose `tags` list contains `"prospect"`
- Returns: `[{id, name, tags}]`

## BIS-297: find_supply_chain_contacts

- Engine: DuckDuckGo HTML search (no API key required)
- Queries issued per company (7 templates):
  - `"{company}" "VP supply chain" site:linkedin.com`
  - `"{company}" "director of supply chain" site:linkedin.com`
  - `"{company}" "demand planner" site:linkedin.com`
  - `"{company}" "supply chain manager" site:linkedin.com`
  - `"{company}" "demand planning" site:linkedin.com`
  - `"{company}" "VP operations" "supply chain" site:linkedin.com`
  - `"{company}" "head of supply chain" site:linkedin.com`
- Extraction: regex-parses LinkedIn profile URLs + surrounding text for name/title
- Deduplicates by source_url within a single run
- Default delay: 1 second between queries (configurable)

## BIS-298: dedup_crm_contacts

- Two lookup strategies:
  1. `contactsAtOrg(orgId)` — fast, scoped to the org
  2. `search(query)` — full-text, broader but slower
- Fuzzy matching via `difflib.SequenceMatcher` on normalised names
  - Normalisation: NFKD decompose, strip accents, lowercase, strip punctuation
  - `>= 0.92` → duplicate (skip)
  - `>= 0.72` → fuzzy_match (flag for human review)
  - `< 0.72` → new (safe to add)
- Output: `{new, duplicates, fuzzy_matches}` each containing augmented contact dicts

## BIS-299: add_contacts_provenance

- createEntity payload:
  ```
  kind: "person"
  tags: ["supply-chain", "prospect-enrichment"]
  meta:
    provenance = "wallace"
    enriched_at = <ISO-8601 UTC>
    title = <title if present>
    source_url = <LinkedIn URL if present>
  ```
- createEdge payload:
  ```
  source: <new entity id>
  target: <org_kissinger_id>
  relation: "works_at"
  ```
- Per-contact errors are non-fatal; recorded in result list

## BIS-300: org_chart_enrichment

- Progress messages: sent via internal Lobster HTTP endpoint
  (`POST localhost:$LOBSTER_MCP_PORT/send_reply`)
- Best-effort delivery — pipeline continues on progress send failure
- `dry_run=True`: all writes skipped, logs `[dry-run]` to stderr
- Final summary fields:
  - `companies_scanned`
  - `contacts_found`
  - `contacts_added`
  - `duplicates_skipped`
  - `fuzzy_flagged`
  - `errors`

## Provenance Tag: "wallace"

All contacts written by this pipeline carry `provenance="wallace"` in their
meta. This allows filtering/auditing pipeline-sourced contacts vs. manually
added ones.
