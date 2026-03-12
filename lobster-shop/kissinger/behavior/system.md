# Kissinger — CRM Behavior

Kissinger is a private relationship graph CRM. It stores people, orgs,
projects, places, and properties as entities, and tracks the connections
between them. When the owner asks about their network, contacts, or relationships,
use Kissinger's MCP tools directly.

## When to Use Kissinger

Use Kissinger tools proactively whenever the owner:
- Asks about a contact, person, or organization ("what do I know about X?")
- Wants to log a meeting, call, or email
- Asks who they knows at a company or in a field
- Wants to find a connection path between two people
- Asks who they hasn't been in touch with recently
- Wants to record what someone can offer or needs

## Tool Reference

### Entity Management
- `kissinger_add_entity` — Add a new person, org, project, place, property, or skill
  - kinds: `person`, `org`, `project`, `place`, `property`, `skill`
- `kissinger_show_entity` — Get full details + connections for an entity (accepts partial ID prefix)
- `kissinger_list_entities` — List entities, optionally filtered by kind or search query

### Connections
- `kissinger_connect` — Create a directed edge between two entities
  - relation types: `knows`, `owns`, `located_in`, `works_at`, `works_on`,
    `part_of`, `funded_by`, `advises`, `partnered_with`, `has_skill`, `custom`
  - strength: 0.0–1.0 (default 0.5)

### Interactions
- `kissinger_log_interaction` — Log a meeting, call, email, message, or note
  - kinds: `meeting`, `call`, `email`, `message`, `note`
  - Accepts comma-separated entity IDs in `with` field
  - Supports optional `follow_up` date (YYYY-MM-DD)

### Search & Discovery
- `kissinger_search` — Full-text search across all entities and interactions
- `kissinger_find_path` — BFS shortest path between two entities
- `kissinger_contacts_stale` — List contacts not interacted with in N days (default 30)
- `kissinger_vortex_scan` — Detect multi-hop offer/need match chains ("opportunity vortices")

### Graph Intelligence
- `kissinger_add_offer` — Record what an entity can offer (for vortex detection)
- `kissinger_add_need` — Record what an entity needs (for vortex detection)
- `kissinger_graph_stats` — Summary statistics: entity counts by kind, edge counts by type

## Behavior Guidelines

1. **Resolve by name first.** If the owner says "add a meeting with Sarah", search for
   "Sarah" first to get her entity ID before logging the interaction.

2. **Entity IDs can be prefixes.** All tools accept either a full UUID or a unique
   prefix (e.g., the first 8 characters). Use the shortest unambiguous prefix.

3. **Use search liberally.** Before adding a new entity, search to check if it
   already exists. Avoid duplicates.

4. **Log interactions completely.** When the owner mentions a meeting or call happened,
   offer to log it. Include notes and follow-up dates when mentioned.

5. **Vortex scan for introductions.** When the owner wants to know who to connect or
   who might help with something, run `kissinger_vortex_scan` and summarize
   the top opportunity chains clearly.

6. **Morning briefing integration.** When the morning-briefing skill is active,
   include stale contacts (>14 days) and any pending follow-ups from Kissinger.

## Database Location

Kissinger stores its data at `~/.kissinger/graph.db` by default.
This can be overridden with the `KISSINGER_DB` environment variable.
