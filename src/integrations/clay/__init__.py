"""
Clay enrichment integration.

Clay is a waterfall enrichment platform aggregating 100+ underlying data sources
(LinkedIn, Apollo, Clearbit, Hunter, PDL, etc.). Their API model is webhook-table
based: you push contacts into a Clay table via webhook, Clay runs its waterfall
enrichment, then posts results back via an HTTP action.

For programmatic person lookup (Enterprise plan), Clay also exposes a direct
people/company lookup API at https://api.clay.com/v3.

Modules:
    client   - Clay API client (direct API + webhook-table model)
    enrich   - Enrichment script: queries Kissinger for gaps, calls Clay, merges back
"""
