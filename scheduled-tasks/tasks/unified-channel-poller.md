# Unified Channel Poller

**Job**: unified-channel-poller
**Schedule**: Hourly (`0 * * * *`)
**Replaces**: bot-talk-poller, lobstertalk-incoming-handler, lobstertalk-poller, lobster-plans-poller, gmail-email-pipeline

## Context

You are running as a scheduled task. This job consolidates all channel polling into a
single hourly pass. It replaces five separate polling jobs that previously ran at
different intervals. All state files from those jobs remain compatible — this job reads
and writes the same state paths so there is no data loss on transition.

## Design Principles

- **No-op gate**: if nothing changed across all channels, send no Telegram message.
  Silence is correct behavior when everything is quiet.
- **Single consolidated notification**: if anything actionable was found across any
  channel, send exactly one Telegram message summarizing all findings.
- **Recursive re-check**: if any channel had new activity, schedule a follow-up check
  in 5 minutes so hot activity is caught quickly without a standing high-frequency cron.
- **Gmail throttle**: Gmail is expensive to poll. Only check it when the last Gmail
  check was more than 6 hours ago.

---

## Authentication

### Bot-talk token

Use this lookup chain (first non-empty value wins):

1. `~/lobster-workspace/data/bot-talk-token.txt`
2. `BOT_TALK_TOKEN` key in `~/messages/config/config.env`
3. `BOT_TALK_TOKEN` key in `~/lobster-config/config.env`

```python
def _load_bot_talk_token() -> str:
    from pathlib import Path

    token_file = Path.home() / "lobster-workspace" / "data" / "bot-talk-token.txt"
    if token_file.exists():
        token = token_file.read_text().strip()
        if token:
            return token

    for config_path in [
        Path.home() / "messages" / "config" / "config.env",
        Path.home() / "lobster-config" / "config.env",
    ]:
        if config_path.exists():
            for line in config_path.read_text().splitlines():
                line = line.strip()
                if line.startswith("BOT_TALK_TOKEN="):
                    value = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if value:
                        return value
    return ""
```

### GitHub

```bash
export GH_TOKEN=$(grep GH_TOKEN ~/lobster-config/config.env | cut -d= -f2)
```

### Twenty CRM

Read `TWENTY_API_KEY` from `~/lobster-config/config.env`. If missing, skip CRM enrichment
and log a warning.

---

## State Files

| File | Purpose |
|------|---------|
| `~/lobster-workspace/data/bot-talk-state.json` | bot-talk last-seen timestamp, hot_mode |
| `~/lobster-workspace/data/lobstertalk-incoming-state.json` | lobstertalk-incoming last processed ts |
| `~/lobster-workspace/data/lobstertalk-issues-last-seen.txt` | lobstertalk GitHub issues last-seen timestamp |
| `~/lobster-workspace/scheduled-jobs/plans-poller-state.json` | lobster-plans per-issue last-seen comment timestamps |
| `~/lobster-workspace/data/unified-poller-state.json` | unified job state: last_gmail_check, last_run |
| `~/lobster-workspace/data/gmail-processed.json` | Gmail: processed message IDs (keep last 1000) |

Always write state files atomically (write to `.tmp` then rename).

### unified-poller-state.json schema

```json
{
  "last_gmail_check": "2026-01-01T00:00:00Z",
  "last_run": "2026-01-01T00:00:00Z"
}
```

Create with these defaults if the file does not exist.

---

## Instructions

### Phase 0: Load shared state

1. Load `~/lobster-workspace/data/unified-poller-state.json` (create with defaults if missing).
2. Load all per-channel state files (see table above).
3. Note `now_utc = datetime.now(timezone.utc)` — use this consistently throughout.

---

### Phase 1: Bot-talk — general activity (replaces bot-talk-poller)

Poll `http://46.224.41.108:4242/messages` with header `X-Bot-Token: <token>`.

Read `last_message_ts` from `bot-talk-state.json` (default: `"2026-01-01T00:00:00Z"`).

Collect ALL messages with timestamp > `last_message_ts` from **both** SaharLobster and
AlbertLobster. Filter out heartbeat messages (skip any message whose `content` contains
the word "heartbeat", case-insensitive).

If new non-heartbeat messages found:
- Sort chronologically (oldest first).
- Format each with a directional prefix:
  - SaharLobster messages: `📤 SaharLobster → Albert: <content>`
  - AlbertLobster messages: `📥 AlbertLobster → the owner: <content>`
- For each AlbertLobster message that looks actionable, also post a comment on the
  relevant issue in `sayhar/project-lobstertalk` if identifiable.
- Set `hot_mode=true` in bot-talk state.
- Reset `consecutive_empty_polls` to 0.
- Update `last_message_ts` to the latest seen timestamp.
- Record findings for the consolidated notification (see Phase 6).

If no new messages:
- Increment `consecutive_empty_polls`.
- If `consecutive_empty_polls >= 3`: set `hot_mode=false`, clear `hot_mode_activated_at`.
- No notification queued.

If the bot-talk HTTP API is unreachable: log the failure, do not alert the owner for
transient outages unless it has been continuously down for >30 minutes.

---

### Phase 2: Bot-talk — SaharLobster incoming queries (replaces lobstertalk-incoming-handler)

Using the same bot-talk API response from Phase 1 (do not re-fetch), filter for messages:
- `sender=AlbertLobster`
- Received since `lobstertalk-incoming-state.json`'s `last_processed_ts`
- Content matches query patterns: "what do you know about", "tell me about", "context on",
  "who is", "any info on"

For each matching query message:

1. Extract the person name from the message.
2. Query all available data sources:

   **Google Drive** (`robotsquadsm@gmail.com`):
   ```bash
   gws drive files list --params '{"q": "fullText contains \"NAME\" or name contains \"NAME\"", "pageSize": 10}'
   ```
   For each matching file, read its content:
   ```bash
   gws drive files get --params '{"fileId": "FILE_ID", "alt": "media"}'
   ```

   **Gmail**:
   ```bash
   gws gmail users messages list --params '{"userId": "me", "q": "NAME", "maxResults": 5}'
   ```
   Get subject and snippet for each result.

   **Twenty CRM** (if token available):
   ```
   POST https://honest-navy-moose.twenty.com/graphql
   Authorization: Bearer <token>
   {"query": "{ people(filter: {name: {firstName: {like: \"%NAME%\"}}}, first: 5) { edges { node { id name { firstName lastName } emails { primaryEmail } phones { primaryPhoneNumber } notes { edges { node { body } } } } } } }"}
   ```

3. Compose a context reply and send via bot-talk:
   ```
   POST http://46.224.41.108:4242/message
   {
     "sender": "SaharLobster",
     "recipient": "AlbertLobster",
     "content": "<reply>",
     "genre": "acknowledgment",
     "tier": "TIER-BOT"
   }
   ```

4. Update `lobstertalk-incoming-state.json`'s `last_processed_ts` to the latest
   processed message timestamp.

5. If a query was handled, record it for the consolidated notification.

---

### Phase 3: LobsterTalk GitHub issues (replaces lobstertalk-poller)

Read `last_seen_ts` from `~/lobster-workspace/data/lobstertalk-issues-last-seen.txt`
(default: epoch 0 if file doesn't exist).

Fetch all open issues:
```bash
gh issue list --repo sayhar/project-lobstertalk --state open \
  --json number,title,labels,assignees,projectItems,comments,updatedAt
```

For each issue with `updatedAt > last_seen_ts`:

1. Fetch full detail:
   ```bash
   gh issue view <number> --repo sayhar/project-lobstertalk --comments
   ```
2. Identify new comments from collaborators (not SaharLobster).
3. If a comment warrants a reply: post one as `🤖🦞 Lobster (ops): <content>`.
4. If an issue has had no activity for 24h and its board status does not match reality:
   flag for notification.
5. If something requires Sahar's direct attention: queue for consolidated notification.

Reconcile the kanban board: for every open issue, if SaharLobster has made progress but
the kanban status is stale, post a status update comment: `🤖🦞 Lobster (ops): <content>`.

Write `now_utc` (ISO 8601) to `lobstertalk-issues-last-seen.txt`.

---

### Phase 4: Lobster-plans GitHub issues (replaces lobster-plans-poller)

Read `~/lobster-workspace/scheduled-jobs/plans-poller-state.json`
(create as `{}` if missing).

Fetch open `awaiting-decision` issues:
```bash
gh issue list --repo sayhar/lobster-plans --label awaiting-decision --state open \
  --json number,title,url,comments \
  --jq '.[] | {number: .number, title: .title, url: .url, owner_comments: [.comments[] | select(.author.login == "sayhar")]}'
```

For each issue:
1. Get last-seen timestamp for this issue number from state file (default: `"1970-01-01T00:00:00Z"`).
2. Filter `owner_comments` to only those with `createdAt > last_seen_timestamp`.
3. If there are new comments:
   - Update state: set `issue_number` → most recent new comment's `createdAt`.
   - Remove `awaiting-decision` label:
     ```bash
     gh issue edit <number> --repo sayhar/lobster-plans --remove-label "awaiting-decision"
     ```
   - Add `approved` label:
     ```bash
     gh issue edit <number> --repo sayhar/lobster-plans --add-label "approved"
     ```
   - Queue for consolidated notification: "Sahar commented on: [title]\n[url]"

Always save the updated plans-poller state file (even when no action taken — advances
the last-seen pointer).

---

### Phase 5: Gmail (replaces gmail-email-pipeline) — throttled to every 6 hours

Check `last_gmail_check` in `unified-poller-state.json`.

**Skip this phase entirely** if `(now_utc - last_gmail_check) < 6 hours`.

If 6 hours have passed, run the full Gmail pipeline:

#### Step 5a: Load processed email IDs

Read `~/lobster-workspace/data/gmail-processed.json`. If missing, treat as
`{"processed_ids": []}`.

#### Step 5b: Poll Gmail for unread inbox emails

```bash
gws gmail users messages list --params '{"userId": "me", "q": "is:unread in:inbox", "maxResults": 10}'
```

For each message ID not in processed_ids:

#### Step 5c: Fetch full email content

```bash
gws gmail users messages get --params '{"userId": "me", "id": "<MSG_ID>", "format": "full"}'
```

Extract `From`, `Subject`, `Date` headers and body (prefer `text/plain`; decode base64url).

#### Step 5d: Automated sender filter

Skip emails where the From address contains: `no-reply`, `noreply`, `donotreply`,
`mailer-daemon`, `notifications@`, `automated@`. Add to processed_ids and continue.

#### Step 5e: Sensitivity filter

Skip emails that are clearly personal (family, health, personal social life, housing,
personal finances, personal travel). Process professional/business emails.
When in doubt, skip.

#### Step 5f: Look up sender in Twenty CRM

```graphql
query FindPerson($email: String!) {
  people(filter: { emails: { primaryEmail: { eq: $email } } }) {
    edges { node { id name { firstName lastName } emails { primaryEmail } company { name } jobTitle } }
  }
}
```

POST to `https://honest-navy-moose.twenty.com/graphql` with
`Authorization: Bearer <TWENTY_API_KEY>`.

If not found, create the contact with name, email, company (from domain), jobTitle
(from signature if detectable). Business context only — no personal info.

If found and no company set, try to update with detected company.

#### Step 5g: Check for Albert Alexander mention

Scan subject and body for "Albert", "Albert Alexander", "AlbertLobster", "albert@"
(case-insensitive). If found, send a bot-talk alert:

```python
payload = {
    "sender": "SaharLobster",
    "tier": "TIER-0",
    "genre": "status-update",
    "content": f"[Gmail Pipeline] Email from {sender_email} mentions Albert. Subject: {subject[:100]}"
}
```

#### Step 5h: Draft a suggested reply

Compose a professional 2-4 sentence reply incorporating any available context.

#### Step 5i: Queue Gmail findings for consolidated notification

Format per email:
```
From: {sender_name} <{sender_email}>
Subject: {subject}
CRM: {found in Twenty / created in Twenty}
Albert mention: yes/no
Suggested reply: {draft}
```

#### Step 5j: Save processed email IDs

Write all processed IDs (including skipped) back to
`~/lobster-workspace/data/gmail-processed.json`. Keep at most the last 1000 IDs.

Write atomically.

Update `last_gmail_check` in `unified-poller-state.json` to `now_utc`.

---

### Phase 6: Send consolidated notification (no-op gate)

Collect all findings queued during Phases 1–5.

**If nothing was found** (no new bot-talk messages, no incoming queries answered, no new
lobstertalk issue activity, no lobster-plans decisions, no Gmail emails processed):
- Do NOT call `send_reply`. Silence is correct.
- Proceed directly to Phase 7.

**If any findings were found**, send a single Telegram message to `chat_id=8305714125`:

```
Unified channel check — {N} channel(s) with activity:

[Bot-talk] {summary or "quiet"}
[Incoming queries] {summary or omit if none}
[LobsterTalk issues] {summary or omit if none}
[Lobster-plans] {summary or omit if none}
[Gmail] {summary or "not checked this run" or omit if none}
```

Keep the message concise — one line per channel with activity. Only include channels
that had actual findings.

---

### Phase 7: Recursive re-check

If any channel had new activity during this run:
```bash
at now + 5 minutes /home/lobster/lobster/scripts/post-reminder.sh unified-channel-poller
```

The `post-reminder.sh` script has built-in dedup — safe to call even if a reminder is
already pending. This keeps the job hot while activity is ongoing without requiring a
permanent high-frequency cron entry.

If nothing changed, do not schedule a re-check.

---

### Phase 8: Write updated state

Update all state files atomically:
- `bot-talk-state.json` — updated `last_message_ts`, `hot_mode`, `consecutive_empty_polls`
- `lobstertalk-incoming-state.json` — updated `last_processed_ts`
- `lobstertalk-issues-last-seen.txt` — current UTC timestamp
- `plans-poller-state.json` — updated per-issue timestamps
- `unified-poller-state.json` — updated `last_run` (and `last_gmail_check` if Gmail ran)

---

## Error Handling

- **Bot-talk API unreachable**: log error, skip Phases 1–2, continue with GitHub phases.
- **GitHub CLI fails**: log error, skip that phase, continue.
- **gws not available**: log error, skip Gmail phase, write task output with a note.
- **Twenty CRM fails**: log warning, skip CRM enrichment, continue.
- Never let one channel failure abort the entire run. Each phase should be independent.

---

## Output

When you complete your task, call `write_task_output` with:
- `job_name`: `"unified-channel-poller"`
- `output`: 1–3 line summary: e.g. `"Bot-talk: 2 new messages. LobsterTalk: no changes. Plans: 0 decisions. Gmail: skipped (checked 2h ago). Notified Sahar."`
- `status`: `"success"` or `"failed"`

Then call `write_result` with a 1–2 line summary for the dispatcher.
