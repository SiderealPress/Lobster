# Enrichment Source Manifest

`manifest.json` is the single source of truth for all data sources the enrichment pipeline can use.

## How It Works

At pipeline startup, `pipeline_hygiene.py` loads the manifest and evaluates each source's `available` field by checking whether the required `api_key_env` environment variable is set (non-empty). Sources with no `api_key_env` (e.g. DuckDuckGo, company websites) are always available.

Unavailable sources are logged and skipped — the pipeline degrades gracefully rather than failing.

## Goal Coverage

| Goal | Description | Best Sources (in order) |
|------|-------------|------------------------|
| `org_chart` | Officers, supply chain leaders at a company | Apollo → ZoomInfo → Clay → LinkedIn SERP → Crunchbase → Company Website → DuckDuckGo |
| `work_history` | Current + past roles for a contact | Apollo → LinkedIn SERP → Clay → ZoomInfo → Hunter → Kissinger Graph |
| `connections` | First/second-degree relationships | Kissinger Graph → Apollo → LinkedIn SERP → Clay → Crunchbase |

## Currently Available Sources (no API key required)

- **google_serp_free** — DuckDuckGo HTML search. Powers the existing BIS-297 step.
- **company_website** — WebFetch of About/Team/Leadership pages.
- **kissinger_graph** — Local Kissinger GraphQL. Best for connection mapping.

## Currently Unavailable (key not configured)

| Source | Key Needed | Best For |
|--------|------------|----------|
| Apollo.io | `APOLLO_API_KEY` | Org charts + work history |
| LinkedIn via SerpAPI | `SERP_API_KEY` | Work history (structured) |
| Crunchbase | `CRUNCHBASE_API_KEY` | Executive org charts |
| Hunter.io | `HUNTER_API_KEY` | Email + basic work info |
| Clay | `CLAY_API_KEY` | Waterfall enrichment |
| ZoomInfo | `ZOOMINFO_API_KEY` | Highest-quality org charts |

To activate a source: add its API key to `~/lobster-config/config.env` and the pipeline will pick it up on next run — no code changes needed.

## Goal Score Interpretation

`goal_scores` are 0.0–1.0 floats indicating how well a source covers a goal:
- **0.85–1.0** — Excellent primary source
- **0.60–0.84** — Good supplementary source
- **0.30–0.59** — Weak but useful fallback
- **< 0.30** — Not recommended for this goal
