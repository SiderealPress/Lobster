# Provenance Ontology

Every enrichment write to Kissinger must carry a set of structured `meta` entries
that constitute its **provenance record**. This document is the canonical specification.

---

## Required Provenance Fields

All fields are stored as Kissinger `meta` entries (key/value string pairs) on the
entity or edge being written.

| Key | Format | Example | Required |
|-----|--------|---------|----------|
| `provenance.source` | string — `source_id` from manifest | `"apollo"` | Yes |
| `provenance.source_url` | URI or API endpoint string | `"https://api.apollo.io/v1/people/search"` | Yes |
| `provenance.enriched_at` | ISO 8601 UTC timestamp | `"2026-04-09T18:00:00Z"` | Yes |
| `provenance.enriched_by` | string — always `"wallace"` for automated runs | `"wallace"` | Yes |
| `provenance.pipeline_run_id` | UUID v4 | `"a3f8c1d2-..."` | Yes |
| `provenance.confidence` | `"high"` / `"medium"` / `"low"` | `"high"` | Yes |
| `provenance.goal` | `"org_chart"` / `"work_history"` / `"connections"` | `"org_chart"` | Yes |
| `provenance.raw_response_hash` | `sha256:` + hex digest of raw API response | `"sha256:a3f4..."` | Yes |

### Confidence Mapping

Confidence is derived from the source's `goal_scores[goal]` in the manifest:

| Goal Score | Confidence |
|------------|------------|
| ≥ 0.75 | `"high"` |
| 0.50 – 0.74 | `"medium"` |
| < 0.50 | `"low"` |

---

## Idempotency Rule (Dedup Guard)

Before writing any enrichment data, the pipeline MUST check whether the target entity
already has a `provenance.enriched_at` entry from the **same source** within that
source's `data_freshness_days` window.

**Algorithm:**

```python
existing_meta = {m["key"]: m["value"] for m in entity["meta"]}
last_enriched = existing_meta.get("provenance.enriched_at")
last_source = existing_meta.get("provenance.source")

if last_source == source_id and last_enriched:
    age_days = (now - parse_iso(last_enriched)).total_seconds() / 86400
    if age_days < source["data_freshness_days"]:
        log(f"SKIP {entity_id}: enriched by {source_id} {age_days:.1f}d ago (fresh)")
        return "skipped"
```

**Note:** An entity may have multiple provenance records from different sources.
The freshness check is **per source** — enriching from Apollo does not prevent
enrichment from Hunter.io running on the same entity.

**Multi-source provenance:** When multiple sources enrich the same entity, each
source appends its provenance keys with a source-specific suffix:

```
provenance.source.apollo = "apollo"
provenance.enriched_at.apollo = "2026-04-09T18:00:00Z"
provenance.source.google_serp_free = "google_serp_free"
provenance.enriched_at.google_serp_free = "2026-04-07T10:00:00Z"
```

The unsuffixed keys (`provenance.source`, `provenance.enriched_at`) always reflect
the **most recent** enrichment, regardless of source.

---

## Rollback Log Schema

Every pipeline run appends a JSONL rollback log at:

```
~/lobster-workspace/enrichment-runs/{run_id}-rollback.jsonl
```

Each line is one write event:

```json
{
  "event": "entity_created",
  "run_id": "a3f8c1d2-...",
  "timestamp": "2026-04-09T18:00:05Z",
  "entity_id": "ent_abc123",
  "entity_name": "Jane Smith",
  "source": "google_serp_free",
  "goal": "org_chart",
  "dry_run": false,
  "meta_written": {
    "provenance.source": "google_serp_free",
    "provenance.enriched_at": "2026-04-09T18:00:05Z",
    "provenance.enriched_by": "wallace",
    "provenance.pipeline_run_id": "a3f8c1d2-...",
    "provenance.confidence": "low",
    "provenance.goal": "org_chart",
    "provenance.raw_response_hash": "sha256:..."
  }
}
```

```json
{
  "event": "edge_created",
  "run_id": "a3f8c1d2-...",
  "timestamp": "2026-04-09T18:00:06Z",
  "source_entity": "ent_abc123",
  "target_entity": "ent_org456",
  "relation": "works_at",
  "dry_run": false
}
```

```json
{
  "event": "skipped_fresh",
  "run_id": "a3f8c1d2-...",
  "timestamp": "2026-04-09T18:00:07Z",
  "entity_id": "ent_xyz789",
  "entity_name": "Bob Jones",
  "source": "google_serp_free",
  "last_enriched_at": "2026-04-08T12:00:00Z",
  "age_days": 1.25
}
```

```json
{
  "event": "error",
  "run_id": "a3f8c1d2-...",
  "timestamp": "2026-04-09T18:00:08Z",
  "entity_name": "Unknown Person",
  "source": "google_serp_free",
  "error": "createEntity failed: HTTP 500"
}
```

---

## Run Summary Schema

Every pipeline run writes a result manifest at:

```
~/lobster-workspace/enrichment-runs/{run_id}.json
```

```json
{
  "run_id": "a3f8c1d2-...",
  "started_at": "2026-04-09T18:00:00Z",
  "finished_at": "2026-04-09T18:05:00Z",
  "status": "completed",
  "dry_run": false,
  "contact_id": null,
  "goals": ["org_chart"],
  "sources_attempted": ["google_serp_free"],
  "sources_skipped": ["apollo", "clay", "zoominfo", "hunter", "linkedin_serp", "crunchbase"],
  "companies_scanned": 5,
  "contacts_found": 23,
  "contacts_added": 11,
  "duplicates_skipped": 8,
  "fuzzy_flagged": 4,
  "skipped_fresh": 0,
  "errors": [],
  "rollback_log": "~/lobster-workspace/enrichment-runs/a3f8c1d2-...-rollback.jsonl"
}
```

---

## Audit Queries

To find all Wallace-enriched contacts in Kissinger:

```graphql
query {
  entities(kind: "person", first: 100) {
    edges {
      node {
        id name
        meta { key value }
      }
    }
  }
}
```

Then filter client-side for `meta` entries where `key = "provenance.enriched_by"` and `value = "wallace"`.

To check enrichment age for a specific entity, look for `provenance.enriched_at` in its meta.
