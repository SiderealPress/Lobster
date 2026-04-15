# Prospect Enrichment Skill

## Purpose

This skill runs a 5-step pipeline that discovers supply-chain contacts at
prospect organisations and writes them into Kissinger CRM with full provenance.

---

## Trigger Commands

- `/enrich-prospects` — run the full enrichment pipeline
- `/org-chart` — alias for full enrichment
- `/prospect-enrich` — alias for full enrichment

---

## Dispatcher Behavior (Main Thread)

When the owner sends `/enrich-prospects` (or alias):

1. Reply: `"Starting prospect org-chart enrichment — I'll report back when it's done."`
2. Spawn a **background subagent** (7-second rule — web search + CRM writes are slow):

```python
Task(
    prompt="""
Run the prospect org-chart enrichment pipeline.

Steps:
1. Import pipeline:
   import sys, os
   sys.path.insert(0, os.path.expanduser(
       "~/lobster/lobster-shop/prospect-enrichment/bin"
   ))
   from org_chart_enrichment import run_enrichment

2. Run the pipeline:
   summary = run_enrichment(
       dry_run={dry_run},
       chat_id={chat_id},
       endpoint="http://localhost:8080/graphql",
   )

3. Send the final summary:
   import json
   send_reply(chat_id={chat_id}, text=json.dumps(summary, indent=2))
""",
    subagent_type="general-purpose",
    run_in_background=True,
)
```

3. `mark_processed(message_id)`
4. Return to `wait_for_messages()` immediately.

---

## Dry Run Mode

If the owner says "dry run" or "simulate" along with the enrich command,
pass `dry_run=True` to `run_enrichment()`. Contacts will not be written;
the pipeline will log what it would do and return a preview summary.

---

## Individual Skill Scripts

Each pipeline stage can also be called directly:

| Script | BIS | Purpose |
|--------|-----|---------|
| `list_prospect_companies.py` | 296 | List orgs tagged "prospect" |
| `find_supply_chain_contacts.py` | 297 | Web-search contacts at a company |
| `dedup_crm_contacts.py` | 298 | Filter against existing CRM |
| `add_contacts_provenance.py` | 299 | Write contacts with provenance |
| `org_chart_enrichment.py` | 300 | Orchestrate all 4 steps |

All scripts are in `~/lobster/lobster-shop/prospect-enrichment/bin/`.

---

## Progress Updates

The pipeline sends per-step progress messages to `chat_id` via the Lobster
internal HTTP endpoint. These are best-effort — pipeline does not abort on
send failure.

---

## Error Handling

- Step failures are collected but do not abort the pipeline (except Step 1)
- All errors are included in the final summary under `"errors": [...]`
- Exit code 1 from CLI if any errors occurred
- Never surface stack traces to the user
