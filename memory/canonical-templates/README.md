# Canonical Memory Templates

These are seed templates for Lobster's canonical memory files. The nightly
consolidation process generates and updates the actual canonical files in
`memory/canonical/`, which is `.gitignore`d since it contains user-specific
data.

## First-time setup

Copy these templates to initialize your canonical memory:

```bash
cp -r memory/canonical-templates/* memory/canonical/
```

After the first nightly consolidation run, these files will be replaced with
synthesized content from your actual memory events.

## Files

| Template | Purpose |
|----------|---------|
| `handoff.md` | Complete session briefing document (the "crown jewel") |
| `priorities.md` | Numbered priority stack |
| `daily-digest.md` | Daily activity summary |
| `pending-decisions.md` | Open decisions needing resolution |
| `people/owner.md` | Owner profile template |
| `projects/example.md` | Project tracking template |
