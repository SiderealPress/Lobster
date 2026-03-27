# Gmail Email Pipeline

**Job**: gmail-email-pipeline
**Schedule**: Every 10 minutes (`*/10 * * * *`)

## Context

You are running as a scheduled task. Poll Gmail for new unread emails, apply a
sensitivity filter, look up senders in Twenty CRM, create/update contacts,
draft suggested replies, notify via bot-talk when Albert Alexander is mentioned,
and send Sahar a Telegram summary.

## Configuration

- **Gmail**: use `gws` CLI (already authenticated)
- **Twenty CRM API**: POST to `https://honest-navy-moose.twenty.com/graphql`
  - API key: read `TWENTY_API_KEY` from `~/lobster-config/config.env`; if missing,
    use hardcoded fallback `eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJjYTIwNGQxMC0yNTc5LTRkN2YtYTY3Ny1iNWUyNTkwMGMyNzQiLCJ0eXBlIjoiQVBJX0tFWSIsIndvcmtzcGFjZUlkIjoiY2EyMDRkMTAtMjU3OS00ZDdmLWE2NzctYjVlMjU5MDBjMjc0IiwiaWF0IjoxNzc0NjQ1NzExLCJleHAiOjQ5MjgyNDkzMTAsImp0aSI6IjAwOWNlMjQyLTUyN2EtNGM5ZC1hNTZjLWFjMGMwZmFmYTRlNSJ9.bTQytwTqdPJAEFrvOPREC93cjavQXAXXKiuCNvmIfiA`
- **Bot-talk**: POST to `http://46.224.41.108:4242/message`
  - Token lookup chain: `~/lobster-workspace/data/bot-talk-token.txt`, then
    `BOT_TALK_TOKEN` in `~/messages/config/config.env`, then
    `BOT_TALK_TOKEN` in `~/lobster-config/config.env`
- **Sahar's Telegram chat_id**: `ADMIN_CHAT_ID_REDACTED`
- **Processed emails tracker**: `~/lobster-workspace/data/gmail-processed.json`

## Sensitivity Filter

**Skip** emails that are clearly personal in nature. Personal topics include:
- Family relationships, children, spouse/partner, parents
- Health, medical, therapy, mental health
- Personal social life, friendships unrelated to work
- Housing, personal finances (non-business)
- Personal travel (holidays, vacations unrelated to work)

**Process** emails that are professional or business-related:
- Work projects, clients, vendors, partners
- Business proposals, contracts, invoices
- Professional networking and introductions
- Product/service inquiries
- Any email from a domain that looks like a business (not personal gmail/hotmail/etc. of known personal contacts)

When in doubt about sensitivity: skip it (err on the side of privacy).

## Instructions

### Step 1: Load processed email IDs

Read `~/lobster-workspace/data/gmail-processed.json`. If missing, treat as empty:
```json
{"processed_ids": []}
```

### Step 2: Poll Gmail for unread inbox emails

```bash
gws gmail users messages list --params '{"userId": "me", "q": "is:unread in:inbox", "maxResults": 10}'
```

Parse the response. For each message ID that is NOT in the processed_ids list:

### Step 3: Fetch full email content

```bash
gws gmail users messages get --params '{"userId": "me", "id": "<MSG_ID>", "format": "full"}'
```

Extract:
- `id` — message ID
- `payload.headers` — look for `From`, `Subject`, `Date`
- Body: check `payload.body.data` (base64url encoded), or walk `payload.parts[]` for
  `mimeType=text/plain` or `text/html` (prefer plain text). Decode base64url to get the
  text.

Parse sender email from the `From` header (e.g., `"Jane Doe <jane@example.com>"` → `jane@example.com`).

### Step 4: Apply sensitivity filter

Using the rules above, decide: is this email professional/business? If personal, skip it
(add to processed_ids so it won't be re-evaluated next run, but do not act on it).

### Step 5: Look up sender in Twenty CRM

Query Twenty CRM for the sender email:

```graphql
query FindPerson($email: String!) {
  people(filter: { emails: { primaryEmail: { eq: $email } } }) {
    edges {
      node {
        id
        name { firstName lastName }
        emails { primaryEmail }
        company { name }
        jobTitle
      }
    }
  }
}
```

POST to `https://honest-navy-moose.twenty.com/graphql` with:
- Header: `Authorization: Bearer <TWENTY_API_KEY>`
- Header: `Content-Type: application/json`
- Body: `{"query": "<query>", "variables": {"email": "<sender_email>"}}`

### Step 6: Create or update contact in Twenty

If the person was NOT found in Step 5, create them:

```graphql
mutation CreatePerson($input: PersonCreateInput!) {
  createPerson(data: $input) {
    id
    name { firstName lastName }
  }
}
```

With input variables built from the email headers. Only include business context:
- Name (from From header)
- Email address
- Company (from email domain or signature if detectable)
- jobTitle (from signature if detectable, otherwise leave blank)
- Do NOT include any personal information

If the person was found and has no company set, try to update with detected company.

### Step 7: Draft a suggested reply

Compose a short suggested reply (2-4 sentences) appropriate to the email subject and
body. The reply should be professional and concise. Label it clearly as a draft suggestion.

### Step 8: Check for Albert Alexander mention

Scan the email subject and body for any mention of "Albert", "Albert Alexander",
"AlbertLobster", or "albert@" (case-insensitive).

If found, send a bot-talk message:

```python
payload = {
    "sender": "SaharLobster",
    "tier": "TIER-0",
    "genre": "status-update",
    "content": f"[Gmail Pipeline] Email from {sender_email} mentions Albert. Subject: {subject[:100]}"
}
headers = {"X-Bot-Token": token}
requests.post("http://46.224.41.108:4242/message", json=payload, headers=headers, timeout=5)
```

### Step 9: Send Telegram summary to Sahar

Write a message file to `~/messages/inbox/` in this format:

```python
import json, uuid, time
from pathlib import Path

msg = {
    "id": str(uuid.uuid4()),
    "source": "gmail-pipeline",
    "chat_id": ADMIN_CHAT_ID_REDACTED,
    "type": "text",
    "subtype": "scheduler_tick",
    "text": summary_text,
    "timestamp": time.time(),
}
inbox = Path.home() / "messages" / "inbox"
inbox.mkdir(parents=True, exist_ok=True)
(inbox / f"gmail-{msg['id']}.json").write_text(json.dumps(msg))
```

The summary_text should follow this format:
```
Gmail pipeline: {N} new email(s) processed

From: {sender_name} <{sender_email}>
Subject: {subject}
CRM: {found in Twenty / created in Twenty}
Albert mention: {yes/no}

Suggested reply:
{draft_reply}
```

If multiple emails were processed, include one block per email.

If zero emails were processed (all skipped or no new mail), do NOT send a Telegram message.

### Step 10: Save processed email IDs

Add all processed message IDs (including skipped personal ones) to `~/lobster-workspace/data/gmail-processed.json`.

Write atomically: write to `.tmp` then rename.

Keep at most the last 1000 IDs to prevent unbounded growth:
```python
processed_ids = (existing_ids + new_ids)[-1000:]
```

## Error Handling

- If `gws` command fails (not installed, auth error): log the error, write task output
  with status "failed", and exit.
- If Twenty CRM request fails: log the error but continue (CRM lookup is non-blocking).
- If bot-talk ping fails: log the error but continue.
- If Telegram write fails: log the error but continue.
- Always update processed_ids even if downstream steps fail, to avoid re-processing.

## Output

When you complete your task, call `write_task_output` with:
- job_name: "gmail-email-pipeline"
- output: Summary of emails processed, skipped, CRM actions taken
- status: "success" or "failed"

Example output:
```
Processed 2 emails, skipped 1 (personal).
Email 1: jane@acme.com "Q4 proposal" — created CRM contact, no Albert mention, reply drafted.
Email 2: bob@supplier.com "Invoice #123" — found existing CRM contact, no Albert mention, reply drafted.
Skipped: personal@gmail.com (personal topic).
```
