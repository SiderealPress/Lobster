# Eloso Prospecting Ontology

**Version:** 1.0.0
**Status:** Canonical
**Last updated:** 2026-04-09
**Owner:** Eloso / Lobster pipeline

---

## Overview

This document defines the canonical tagging taxonomy and meta-field schema for
all prospect-related entities in Kissinger. Every pipeline write MUST conform to
this ontology. Deviations require a PR against this file.

Kissinger uses a **closed Rust enum for EntityKind**. The only valid kinds for
company entities are the ones compiled into the binary — currently this includes
`org`. All company classification is therefore expressed through **tags** and
**meta fields**, not entity kinds.

---

## 1. Entity Kind Convention

| Entity type | Kissinger kind | Classification mechanism |
|-------------|---------------|--------------------------|
| Company (prospect, supplier, partner) | `org` | tags + meta |
| Person (contact) | `person` | tags + meta |

**Never** attempt to create entities with custom kinds. Use `kind: "org"` for all
company entities and `kind: "person"` for all contacts.

---

## 2. Tag Taxonomy

Tags are lowercase, hyphen-separated strings. They are additive — an entity may
carry multiple tags from different namespaces. Pipeline writes MUST be idempotent:
adding a tag that already exists is a no-op (handled at the write layer).

### 2.1 Pipeline Role Tags

These tags classify the entity's role in the Eloso pipeline.

| Tag | Meaning |
|-----|---------|
| `prospect` | Target customer or design partner candidate |
| `seed` | Manually curated prototype ICP company (subset of prospect) |
| `supplier` | Known or potential supplier to a seed/prospect company |
| `customer` | Known or potential customer of a seed/prospect company |
| `investor` | Generic investor classification |
| `vc_firm` | Venture capital firm |
| `family_office` | Family office investor |
| `angel` | Angel investor |
| `icp_match` | Confirmed ICP match after scoring (icp_score >= 70) |
| `eloso` | Entity is part of the Eloso prospecting universe |

### 2.2 Industry Vertical Tags

Prefix: `vertical:`

| Tag | Meaning |
|-----|---------|
| `vertical:aerospace` | Commercial or defense aerospace |
| `vertical:defense` | Defense systems, primes, defense electronics |
| `vertical:heavy_equipment` | Industrial heavy equipment, construction equipment |
| `vertical:contract_manufacturing` | Contract / EMS manufacturing |
| `vertical:capital_goods` | Industrial capital goods, precision manufacturing |
| `vertical:rail` | Rail cars, rail equipment, rail components |
| `vertical:chemicals` | Specialty chemicals and materials |
| `vertical:ev` | Electric vehicles and EV components |
| `vertical:building_products` | Building products, construction materials |

**Assignment rules:**
- A company MAY carry multiple vertical tags (e.g. a defense aerospace prime:
  `vertical:defense` AND `vertical:aerospace`)
- When in doubt between two verticals, assign the more specific one
- Vertical tags reflect the company's primary revenue source, not peripheral activity

### 2.3 Company Size Tags

Prefix: `size:`

Mutually exclusive — assign exactly one.

| Tag | Revenue range | Meaning |
|-----|--------------|---------|
| `size:enterprise` | > $1B annual revenue | Large enterprise |
| `size:mid_market` | $100M – $1B | Mid-market |
| `size:smb` | < $100M | Small/medium business |

**ICP targeting note:** Eloso's target ICP is `size:mid_market` and `size:enterprise`.
Companies tagged `size:smb` are out-of-ICP unless flagged as strategic exceptions.

### 2.4 Pipeline Stage Tags

Prefix: `stage:`

Tracks progression through the Eloso sales pipeline.

| Tag | Meaning |
|-----|---------|
| `stage:research` | Initial research phase; no outreach yet |
| `stage:contacted` | At least one outreach attempt made |
| `stage:engaged` | Two-way engagement; active conversation |
| `stage:qualified` | Qualified as real opportunity (BANT / pain confirmed) |
| `stage:design_partner` | Active design partner relationship |
| `stage:customer` | Paying customer |

**Default:** New seeds enter at `stage:research`.

### 2.5 Supply Chain Complexity Tags

Prefix: `supply_chain:`

Mutually exclusive — assign exactly one.

| Tag | Meaning |
|-----|---------|
| `supply_chain:complex` | Multi-tier, multi-country sourcing; regulatory traceability requirements |
| `supply_chain:moderate` | Regional multi-supplier; moderate BOM complexity |
| `supply_chain:simple` | Simple, primarily domestic sourcing |

**Heuristics:**
- Aerospace, defense, rail → `supply_chain:complex`
- Capital goods, chemicals, EV → `supply_chain:moderate` to `supply_chain:complex`
- Building products, contract manufacturing → `supply_chain:moderate`

### 2.6 Fit Tags (Legacy / Compatibility)

These tags were present in the v1 seed import and should be preserved.

| Tag | Meaning |
|-----|---------|
| `fit-high` | Strong ICP fit (maps to icp_score >= 70) |
| `fit-medium` | Moderate ICP fit (maps to icp_score 40-69) |
| `fit-low` | Weak fit (maps to icp_score < 40) |

### 2.7 Supply Chain Relationship Tags

These tags encode graph-level relationships on the supplier/customer entity.
They link back to the seed/prospect entity by ID.

| Tag pattern | Applied to | Meaning |
|-------------|-----------|---------|
| `customer_of:{kissinger_id}` | Supplier entity | This org is a customer of the seed with this ID |
| `supplier_of:{kissinger_id}` | Customer entity | This org is a supplier to the seed with this ID |

**Note:** These tags are redundant with meta fields but enable tag-based
graph traversal without a custom edge type.

---

## 3. Meta Field Schema

Meta fields are key-value string pairs stored on entities. Values may contain
JSON-encoded structures where noted.

### 3.1 Core Prospect Meta Fields

| Field key | Type | Description | Example |
|-----------|------|-------------|---------|
| `hq_location` | string | City, state/country | `"Hunt Valley, MD"` |
| `revenue_estimate` | string | Human-readable revenue range | `"$500M-$1B"` |
| `employee_count` | string | Headcount range | `"~3,000-4,000"` |
| `erp_system` | string | Primary ERP in use | `"Oracle"` |
| `key_challenge` | string | Known supply chain pain point | `"Multi-country sourcing with traceability gaps"` |
| `economic_buyer_title` | string | Title of likely economic buyer | `"VP Supply Chain"` |
| `warm_intro_path` | string | Path to a warm introduction | `"Via Moog → Parker Hannifin"` |
| `source` | string | How the company was discovered | `"eloso-prospects-v2"` |
| `icp_score` | string | ICP score 0-100 | `"82"` |
| `last_enriched_at` | string | ISO 8601 timestamp of last enrichment | `"2026-04-09T00:00:00Z"` |
| `pipeline_stage` | string | Current pipeline stage (mirrors stage: tag) | `"research"` |

### 3.2 Supply Chain Graph Meta Fields

Because Kissinger's GraphQL API currently only exposes `works_at` as an edge
relation (see §5 — Known Limitations), supply chain relationships are stored as
structured meta fields in addition to relationship tags.

| Field key | Type | Description |
|-----------|------|-------------|
| `supplies_to` | string (comma-sep IDs) | Kissinger IDs of orgs this entity supplies to |
| `buys_from` | string (comma-sep IDs) | Kissinger IDs of orgs this entity buys from |
| `known_suppliers` | JSON array | Structured supplier list (see schema below) |
| `known_customers` | JSON array | Structured customer list (see schema below) |

**`known_suppliers` JSON schema:**
```json
[
  {
    "name": "Nucor Corporation",
    "kissinger_id": "abc123...",
    "relationship_type": "steel_supplier",
    "confidence": "high",
    "source": "10-K filing + industry knowledge"
  }
]
```

**`known_customers` JSON schema:**
```json
[
  {
    "name": "FreightCar America",
    "kissinger_id": "def456...",
    "relationship_type": "steel_customer",
    "confidence": "high",
    "source": "public customer disclosure"
  }
]
```

**Confidence values:** `high` | `medium` | `low`

### 3.3 Provenance Meta Fields

All pipeline writes MUST include provenance fields.

| Field key | Meaning |
|-----------|---------|
| `_prov_imported_by` | Author / system that wrote this data |
| `_prov_source` | Logical data source identifier |
| `_prov_imported_at` | ISO 8601 write timestamp |
| `_prov_source_file` | Source file or script name |
| `_prov_script_version` | Script version or git SHA |

---

## 4. Classification Decision Tree

```
Is the company a target customer or design partner?
├── YES → tag: prospect
│   ├── Was it manually curated in the seed list?
│   │   └── YES → also tag: seed
│   ├── Assign vertical: tag(s) based on primary revenue
│   ├── Assign size: tag based on revenue
│   ├── Assign supply_chain: tag based on sourcing complexity
│   └── Assign stage: tag (default: stage:research)
│
└── NO
    ├── Is it a supplier to a prospect?
    │   └── YES → tag: supplier, tag: customer_of:{seed_kissinger_id}
    └── Is it a customer of a prospect?
        └── YES → tag: customer, tag: supplier_of:{prospect_kissinger_id}
```

---

## 5. Known Limitations

### 5.1 Supply Chain Edges via Meta Fields

**Current state:** Kissinger's GraphQL API exposes only one edge relation:
`works_at` (used for person→org employment relationships). There is no
`supplies_to`, `buys_from`, or generic `related_to` edge type.

**Consequence:** Supply chain relationships between organizations must be
encoded as structured meta fields (`known_suppliers`, `known_customers`,
`supplies_to`, `buys_from`) rather than as first-class graph edges.

**Impact:**
- Graph traversal for supply chain paths requires meta-field parsing, not
  edge queries
- Bidirectional relationships must be written to both entities (denormalized)
- No native graph analytics (shortest path, centrality) on supply chain topology

**Recommended future work:** Open a Rust PR against the Kissinger codebase to
extend `EdgeRelation` enum with additional variants:

```rust
pub enum EdgeRelation {
    WorksAt,       // existing
    SuppliesTo,    // NEW: org → org supply chain
    BuysFrom,      // NEW: org → org procurement
    SubsidiaryOf,  // NEW: org → org corporate structure
    CompetesWith,  // NEW: org → org competitive landscape
}
```

This would allow supply chain topology to be expressed as first-class edges,
enabling native graph queries and analytics. Until this PR lands, meta fields
are the canonical storage mechanism.

---

## 6. Tag Validation Rules

Pipeline scripts MUST enforce these rules before writing to Kissinger:

1. **Size tags are mutually exclusive.** An entity must have at most one `size:*` tag.
2. **Stage tags are mutually exclusive.** An entity must have at most one `stage:*` tag.
3. **Supply chain tags are mutually exclusive.** At most one `supply_chain:*` tag.
4. **Vertical tags are additive.** Multiple `vertical:*` tags are valid.
5. **All prospect entities must have** at minimum: one `vertical:*`, one `size:*`,
   one `stage:*`, and one `supply_chain:*` tag.
6. **Provenance is required.** All writes must include `_prov_imported_by`,
   `_prov_source`, and `_prov_imported_at` meta fields.

---

## 7. Versioning

This ontology follows semantic versioning. Breaking changes (removing tags,
renaming fields) require a MAJOR version bump. Additive changes (new tags,
new meta fields) are MINOR. Clarifications and documentation updates are PATCH.

| Version | Date | Change |
|---------|------|--------|
| 1.0.0 | 2026-04-09 | Initial formal ontology (BIS-334) |
