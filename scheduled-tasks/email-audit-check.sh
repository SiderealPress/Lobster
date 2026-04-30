#!/usr/bin/env bash
# Email audit check — runs twice daily at 8am and 12pm CT
# Writes a task to the Lobster inbox for the dispatcher to process with Opus

set -euo pipefail

LOBSTER_DIR="${LOBSTER_DIR:-$HOME/lobster}"
WORKSPACE="${LOBSTER_WORKSPACE:-$HOME/lobster-workspace}"

SECONDARY_EMAIL="${LOBSTER_SECONDARY_EMAIL:-}"

exec uv run --project "$LOBSTER_DIR" python - "$SECONDARY_EMAIL" <<'PY'
import sys
sys.path.insert(0, "/home/admin/lobster/src")

secondary_email = sys.argv[1] if len(sys.argv) > 1 else ""
secondary_email_line = f"   - {secondary_email} (AWP partner account)" if secondary_email else "   - (configure LOBSTER_SECONDARY_EMAIL in config.env)"

task_content = f"""---
job: email-audit-check
model: opus
---

Run a twice-daily email audit for the AWP inbox.

Using Opus, do the following:

1. Check the Gmail API for the last 24 hours of email activity. Use the auth from ~/lobster-config/config.env (GOOGLE_TOKEN_FILE).

2. Audit email processing state:
   - Are there any emails that arrived but were NOT processed by the gmail-poll.py pipeline?
   - Are there any emails in ~/messages/inbox/ or ~/messages/processing/ with source="gmail" that are stuck?
   - Check the audit log at https://awp-two.vercel.app/api/logs (or directly via AWP_DATABASE_URL) for recent email_action_logs entries — do they match what's in Gmail?

3. Check specifically for emails from:
{secondary_email_line}
   - Any email with investor-related content that may have been skipped by the classifier

4. If you find anything missed or unprocessed:
   - Process it now (classify, CRM import if appropriate, audit log)
   - Notify the inbox owner (chat_id=6645894734) with what was found and what action was taken

5. If everything looks healthy (no gaps, no missed emails):
   - Write a brief status to the audit log and do NOT send a notification (no-op result)
   - Call write_result with chat_id=0 and "No action needed — inbox healthy"

Call write_result when done.
"""

import os
import json
from datetime import datetime, timezone

msg_id = f"scheduled-email-audit-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}"
msg = {
    "id": msg_id,
    "type": "scheduled_reminder",
    "job_name": "email-audit-check",
    "reminder_type": "email-audit-check",
    "task_content": task_content,
    "timestamp": datetime.now(timezone.utc).isoformat(),
    "source": "system",
    "chat_id": 0
}

inbox_dir = os.path.expanduser("~/messages/inbox")
os.makedirs(inbox_dir, exist_ok=True)
path = os.path.join(inbox_dir, f"{msg_id}.json")
with open(path, "w") as f:
    json.dump(msg, f)
print(f"Email audit check queued: {msg_id}")
PY
