## Email Autoresponder — Reference

### Scheduled job

- **Job name**: `gmail-auto-draft`
- **Schedule**: Every 5 minutes (`*/5 * * * *`)
- **Account**: albobsterbot@gmail.com
- **What it does**: Finds inbox emails without existing drafts, drafts context-aware HTML replies with named read-only Drive links

### Draft deduplication (critical)

**Before creating any draft**, call `list_drafts` to get all existing drafts. Build a map of `threadId → draftId`. If a thread already has a draft, **skip it entirely** — do not create another, do not modify the existing one.

### Drive file search

The Google Workspace MCP only exposes Gmail — Drive must be queried via REST API directly:

```python
import json, urllib.request, urllib.parse
with open('/home/claude-user/.config/google-workspace-mcp/tokens.json') as f:
    tokens = json.load(f)
token = tokens['access_token']
params = urllib.parse.urlencode({'q': 'trashed=false', 'fields': 'files(id,name,mimeType)', 'pageSize': 50})
req = urllib.request.Request(
    f'https://www.googleapis.com/drive/v3/files?{params}',
    headers={'Authorization': f'Bearer {token}'}
)
with urllib.request.urlopen(req) as r:
    files = json.load(r)['files']
```

Always do a fresh search — do not rely solely on hardcoded IDs.

**Known files (verify against fresh search):**
- `General Investment Partners - Opportunity Fund I Pitch Deck` — ID: `1IIQEmt5-tzoCTjaN19Zhsq9uQWK79GQW0ojX2sWkJo0`
- `Buy My House` (Document) — ID: `1k95Kx3PBjWB7wl4UWNPDRDQADXmtVfMe51rJgkqBEB0`

### Draft logic summary

| Email type | Action |
|------------|--------|
| Business / investment inquiry | HTML draft with named pitch deck link, sign as "General Investment Partners" |
| Real estate / house inquiry | HTML draft with named Buy My House doc link, sign as "Al" |
| Spam / automated / newsletters | Skip — do NOT create a draft |
| Unclear intent | Friendly open-ended reply, reference relevant Drive files if any |

### Link rules (strictly enforced)

- **Named hyperlinks only**: `<a href="URL">Descriptive Name</a>` — NEVER paste raw URLs
- **Read-only links only** — NEVER use `/edit` links:
  - Presentation: `https://docs.google.com/presentation/d/{ID}/view?usp=sharing`
  - Document: `https://docs.google.com/document/d/{ID}/preview`

### Draft format

- Use `html` parameter (not `body`) in `draft_email`
- `to`: sender's email
- `subject`: "Re: [original subject]"
- `threadId`: original email's threadId (required for correct threading)
- Concise: 2–4 short paragraphs

### MCP tools used by the job

- `mcp__google-workspace__list_drafts` — check for existing drafts before creating
- `mcp__google-workspace__search_emails` — find inbox emails
- `mcp__google-workspace__read_email` — read full email content
- `mcp__google-workspace__draft_email` — create the reply draft
- `mcp__lobster-inbox__write_task_output` — log results

### Toggle tools (for Lobster main thread)

- `mcp__lobster-inbox__get_scheduled_job("gmail-auto-draft")` — check status
- `mcp__lobster-inbox__update_scheduled_job(name="gmail-auto-draft", enabled=True/False)` — toggle
- `mcp__lobster-inbox__check_task_outputs(job_name="gmail-auto-draft", limit=5)` — see recent results
