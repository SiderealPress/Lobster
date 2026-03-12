# Kissinger — Technical Reference

## Architecture

Kissinger is a Rust workspace with these crates:

| Crate | Purpose |
|-------|---------|
| `kissinger-core` | Domain types: Entity, Edge, Interaction, Offer, Need, VortexParams |
| `kissinger-store` | SQLite-backed persistence via CozoDB; search and graph traversal |
| `kissinger-mcp` | MCP stdio server exposing 12 tools — **this is what Lobster uses** |
| `kissinger-cli` | CLI for manual operation (`kissinger` binary) |
| `kissinger-api` | GraphQL HTTP API (separate from MCP) |
| `kissinger-import` | CSV/vCard import utilities |

## MCP Server

The MCP server (`kissinger-mcp` binary) communicates over stdio using the
MCP 2024-11-05 protocol. It is registered with Claude as a persistent server.

**Binary location:** `<skill-dir>/bin/kissinger-mcp` (installed by `install.sh`)

**Database:** `~/.kissinger/graph.db` (or `$KISSINGER_DB`)

**Source:** [aeschylus/kissinger](https://github.com/aeschylus/kissinger) (private)

## Entity Kinds

| Kind | Description |
|------|-------------|
| `person` | Individual contacts |
| `org` | Companies, organizations, groups |
| `project` | Projects being tracked |
| `place` | Physical or virtual locations |
| `property` | Properties, assets |
| `skill` | Skills or capabilities |

## Relation Types

| Relation | Meaning |
|----------|---------|
| `knows` | General acquaintance (default) |
| `owns` | Ownership relationship |
| `located_in` | Physical/geographic containment |
| `works_at` | Employment |
| `works_on` | Contributing to a project |
| `part_of` | Membership or subsystem |
| `funded_by` | Funding relationship |
| `advises` | Advisory relationship |
| `partnered_with` | Business partnership |
| `has_skill` | Entity has a capability |
| `custom` | User-defined relationship |

## Vortex Detection

A "vortex" is a multi-hop chain where offers and needs align across the graph.
For example: A offers technical help → A knows B → B needs technical help → B
knows C → C is looking for partners. Kissinger scores these chains and surfaces
the highest-value ones. Use `kissinger_vortex_scan` when the owner wants introductions
or wants to know who can help whom.

## CLI Usage (for reference)

```bash
# List all contacts
kissinger list --kind person

# Add a person
kissinger add person "Jane Smith" --tags "investor,saas"

# Search
kissinger search "machine learning"

# Show entity with connections
kissinger show <entity-id>
```

The CLI binary is at `<skill-dir>/bin/kissinger` (installed by `install.sh`).
