# Prospecting Ontology

This directory contains the canonical classification system for Eloso prospect
entities in Kissinger.

## Files

- **`prospecting-ontology.md`** — Full ontology specification: tag taxonomy,
  meta field schema, validation rules, and known limitations

## Quick Reference

### Tag Namespaces

| Namespace | Example | Meaning |
|-----------|---------|---------|
| `vertical:` | `vertical:defense` | Industry vertical |
| `size:` | `size:enterprise` | Revenue-based size band |
| `stage:` | `stage:research` | Pipeline stage |
| `supply_chain:` | `supply_chain:complex` | Sourcing complexity |
| `customer_of:` | `customer_of:{id}` | Supplier relationship back-ref |
| `supplier_of:` | `supplier_of:{id}` | Customer relationship back-ref |

### ICP Verticals

Eloso targets: `aerospace`, `defense`, `heavy_equipment`, `contract_manufacturing`,
`capital_goods`, `rail`, `chemicals`, `ev`, `building_products`

### Size Bands

- `size:enterprise` → > $1B revenue
- `size:mid_market` → $100M – $1B
- `size:smb` → < $100M (out of ICP unless flagged)

### Supply Chain Complexity

- `supply_chain:complex` → multi-tier, multi-country, regulated (aerospace/defense/rail)
- `supply_chain:moderate` → regional multi-supplier, moderate BOM
- `supply_chain:simple` → simple domestic sourcing

## Key Constraint

Kissinger uses a **closed Rust enum for EntityKind**. All classification uses
`kind: "org"` with tags and meta fields. Do not attempt to add new entity kinds.
See the main ontology doc §5 for the recommendation to extend Kissinger's edge
schema in a future Rust PR.
