#!/usr/bin/env python3
"""
LobsterTalk Incoming Message Handler

Polls bot-talk for incoming messages from AlbertLobster.
When a "what do you know about X?" query arrives, looks up X in:
- Google Drive (robotsquadsm@gmail.com)
- Gmail (robotsquadsm@gmail.com)
- Twenty CRM (honest-navy-moose.twenty.com) -- if API token available
- Lobster memory/conversation history

Then replies back via bot-talk with aggregated context.
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BOT_TALK_BASE = "http://46.224.41.108:4242"
TWENTY_GRAPHQL = "https://honest-navy-moose.twenty.com/graphql"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL_API = "https://gmail.googleapis.com/gmail/v1"
DRIVE_API = "https://www.googleapis.com/drive/v3"

STATE_FILE = Path.home() / "lobster-workspace/data/lobstertalk-incoming-state.json"
BOT_TALK_TOKEN_FILE = Path.home() / "lobster-workspace/data/bot-talk-token.txt"
GWS_CREDS_FILE = Path.home() / ".config/gws/credentials.json"
TWENTY_TOKEN_FILE = Path.home() / "lobster-workspace/data/twenty-api-token.txt"

CHAT_ID = 8305714125


# ---------------------------------------------------------------------------
# Helpers: State
# ---------------------------------------------------------------------------
def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"last_processed_ts": "2026-01-01T00:00:00Z"}


def save_state(state):
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.rename(STATE_FILE)


# ---------------------------------------------------------------------------
# Helpers: Google OAuth
# ---------------------------------------------------------------------------
def get_google_access_token():
    """Refresh and return a fresh Google access token."""
    if not GWS_CREDS_FILE.exists():
        return None
    try:
        creds = json.loads(GWS_CREDS_FILE.read_text())
        resp = requests.post(
            GOOGLE_TOKEN_URL,
            data={
                "client_id": creds["client_id"],
                "client_secret": creds["client_secret"],
                "refresh_token": creds["refresh_token"],
                "grant_type": "refresh_token",
            },
            timeout=10,
        )
        resp.raise_for_status()
        token_data = resp.json()
        access_token = token_data.get("access_token")
        if access_token:
            # Update the credentials file with fresh token
            creds["access_token"] = access_token
            tmp = GWS_CREDS_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(creds, indent=2))
            tmp.rename(GWS_CREDS_FILE)
        return access_token
    except Exception as e:
        print(f"[ERROR] Failed to refresh Google token: {e}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# Helpers: Data source lookups
# ---------------------------------------------------------------------------
def search_drive(name: str, access_token: str) -> list[dict]:
    """Search Google Drive for files mentioning the person."""
    results = []
    if not access_token:
        return results
    try:
        # Search by file name containing the name
        params = {
            "q": f'name contains "{name}" or fullText contains "{name}"',
            "pageSize": 10,
            "fields": "files(id,name,mimeType,createdTime,modifiedTime)",
        }
        resp = requests.get(
            f"{DRIVE_API}/files",
            headers={"Authorization": f"Bearer {access_token}"},
            params=params,
            timeout=10,
        )
        resp.raise_for_status()
        files = resp.json().get("files", [])
        for f in files:
            file_info = {
                "id": f["id"],
                "name": f["name"],
                "mime": f.get("mimeType", ""),
                "content": None,
            }
            # Try to read plain text files
            if "text/plain" in f.get("mimeType", ""):
                try:
                    content_resp = requests.get(
                        f"{DRIVE_API}/files/{f['id']}",
                        headers={"Authorization": f"Bearer {access_token}"},
                        params={"alt": "media"},
                        timeout=10,
                    )
                    if content_resp.status_code == 200:
                        content = content_resp.text[:2000]  # Limit to 2KB
                        file_info["content"] = content
                except Exception as e:
                    print(f"[WARN] Could not read file {f['id']}: {e}", file=sys.stderr)
            results.append(file_info)
    except Exception as e:
        print(f"[ERROR] Drive search failed: {e}", file=sys.stderr)
    return results


def search_gmail(name: str, access_token: str) -> list[dict]:
    """Search Gmail for emails mentioning the person."""
    results = []
    if not access_token:
        return results
    try:
        resp = requests.get(
            f"{GMAIL_API}/users/me/messages",
            headers={"Authorization": f"Bearer {access_token}"},
            params={"q": name, "maxResults": 5},
            timeout=10,
        )
        resp.raise_for_status()
        messages = resp.json().get("messages", [])
        for msg_ref in messages[:5]:
            try:
                msg_resp = requests.get(
                    f"{GMAIL_API}/users/me/messages/{msg_ref['id']}",
                    headers={"Authorization": f"Bearer {access_token}"},
                    params={"format": "metadata", "metadataHeaders": ["Subject", "From", "Date", "To"]},
                    timeout=10,
                )
                msg_resp.raise_for_status()
                msg_data = msg_resp.json()
                headers = {
                    h["name"]: h["value"]
                    for h in msg_data.get("payload", {}).get("headers", [])
                }
                results.append({
                    "id": msg_ref["id"],
                    "subject": headers.get("Subject", "(no subject)"),
                    "from": headers.get("From", ""),
                    "date": headers.get("Date", ""),
                    "snippet": msg_data.get("snippet", ""),
                })
            except Exception as e:
                print(f"[WARN] Could not fetch email {msg_ref['id']}: {e}", file=sys.stderr)
    except Exception as e:
        print(f"[ERROR] Gmail search failed: {e}", file=sys.stderr)
    return results


def search_twenty_crm(name: str, twenty_token: str) -> list[dict]:
    """Search Twenty CRM for the person."""
    results = []
    if not twenty_token:
        return results

    # Split name for first/last search
    parts = name.strip().split()
    first_name = parts[0] if parts else name
    last_name = parts[-1] if len(parts) > 1 else ""

    query = """
    query SearchPeople($filter: PersonFilterInput) {
      people(filter: $filter, first: 5) {
        edges {
          node {
            id
            name { firstName lastName }
            emails { primaryEmail }
            phones { primaryPhoneNumber }
            company { name { value } }
            city
          }
        }
      }
    }
    """
    variables = {
        "filter": {
            "or": [
                {"name": {"firstName": {"like": f"%{first_name}%"}}},
                {"name": {"lastName": {"like": f"%{last_name}%"}}},
            ]
        }
        if last_name else {
            "name": {"firstName": {"like": f"%{first_name}%"}}
        }
    }
    try:
        resp = requests.post(
            TWENTY_GRAPHQL,
            json={"query": query, "variables": variables},
            headers={
                "Authorization": f"Bearer {twenty_token}",
                "Content-Type": "application/json",
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        edges = data.get("data", {}).get("people", {}).get("edges", [])
        for edge in edges:
            node = edge.get("node", {})
            results.append({
                "id": node.get("id"),
                "firstName": node.get("name", {}).get("firstName", ""),
                "lastName": node.get("name", {}).get("lastName", ""),
                "email": node.get("emails", {}).get("primaryEmail", ""),
                "phone": node.get("phones", {}).get("primaryPhoneNumber", ""),
                "company": node.get("company", {}).get("name", {}).get("value", "") if node.get("company") else "",
                "city": node.get("city", ""),
            })
    except Exception as e:
        print(f"[ERROR] Twenty CRM search failed: {e}", file=sys.stderr)
    return results


# ---------------------------------------------------------------------------
# Helpers: Bot-talk
# ---------------------------------------------------------------------------
def bot_talk_get_messages(token: str, since: str | None = None, sender: str = "AlbertLobster") -> list[dict]:
    """Fetch recent bot-talk messages from the given sender."""
    params = {"sender": sender, "limit": 50}
    if since:
        params["since"] = since
    try:
        resp = requests.get(
            f"{BOT_TALK_BASE}/messages",
            headers={"X-Bot-Token": token},
            params=params,
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json().get("messages", [])
    except Exception as e:
        print(f"[ERROR] Bot-talk fetch failed: {e}", file=sys.stderr)
        return []


def bot_talk_send(token: str, content: str, recipient: str = "AlbertLobster") -> bool:
    """Send a message via bot-talk."""
    try:
        resp = requests.post(
            f"{BOT_TALK_BASE}/message",
            headers={"X-Bot-Token": token, "Content-Type": "application/json"},
            json={
                "sender": "SaharLobster",
                "recipient": recipient,
                "content": content,
                "genre": "acknowledgment",
                "tier": "TIER-BOT",
            },
            timeout=10,
        )
        resp.raise_for_status()
        return True
    except Exception as e:
        print(f"[ERROR] Bot-talk send failed: {e}", file=sys.stderr)
        return False


# ---------------------------------------------------------------------------
# Helpers: Query parsing
# ---------------------------------------------------------------------------
QUERY_PATTERNS = [
    r"what do you know about (.+?)[\?\!]?$",
    r"tell me about (.+?)[\?\!]?$",
    r"context on (.+?)[\?\!]?$",
    r"who is (.+?)[\?\!]?$",
    r"any info (?:on|about) (.+?)[\?\!]?$",
    r"what can you tell me about (.+?)[\?\!]?$",
    r"do you have (?:any )?(?:info|information|context) (?:on|about) (.+?)[\?\!]?$",
]


def extract_query_name(content: str) -> str | None:
    """Extract the person name from a query message."""
    content_lower = content.lower().strip()
    for pattern in QUERY_PATTERNS:
        match = re.search(pattern, content_lower, re.IGNORECASE)
        if match:
            name = match.group(1).strip()
            # Clean up common suffixes
            name = re.sub(r"\s+(at|from|in|of)\s+.*$", "", name, flags=re.IGNORECASE)
            # Title-case the name
            name = " ".join(w.capitalize() for w in name.split())
            return name
    return None


def is_query_message(msg: dict) -> bool:
    """Return True if this message contains a person context query."""
    content = msg.get("content", "")
    return extract_query_name(content) is not None


# ---------------------------------------------------------------------------
# Main: Build context reply
# ---------------------------------------------------------------------------
def build_context_reply(name: str, access_token: str | None, twenty_token: str | None) -> str:
    """Look up name in all sources and build a reply."""
    sections = []

    # --- Google Drive ---
    if access_token:
        drive_files = search_drive(name, access_token)
        if drive_files:
            drive_section = "[Google Drive] Files found:\n"
            for f in drive_files:
                drive_section += f"  - {f['name']}\n"
                if f.get("content"):
                    # Indent content lines
                    for line in f["content"].strip().splitlines()[:15]:
                        drive_section += f"    {line}\n"
            sections.append(drive_section.rstrip())
        else:
            sections.append("[Google Drive] No files found mentioning " + name)

    # --- Gmail ---
    if access_token:
        emails = search_gmail(name, access_token)
        if emails:
            email_section = f"[Gmail] {len(emails)} email(s) found:\n"
            for e in emails[:5]:
                email_section += f"  - {e['date'][:16] if e.get('date') else 'N/A'}: \"{e['subject']}\" (from: {e.get('from', 'N/A')})\n"
                if e.get("snippet"):
                    email_section += f"    Snippet: {e['snippet'][:150]}\n"
            sections.append(email_section.rstrip())
        else:
            sections.append("[Gmail] No emails found mentioning " + name)

    # --- Twenty CRM ---
    if twenty_token:
        crm_results = search_twenty_crm(name, twenty_token)
        if crm_results:
            crm_section = "[Twenty CRM] Contacts found:\n"
            for p in crm_results:
                full_name = f"{p['firstName']} {p['lastName']}".strip()
                crm_section += f"  - {full_name}"
                if p.get("company"):
                    crm_section += f" @ {p['company']}"
                crm_section += "\n"
                if p.get("email"):
                    crm_section += f"    Email: {p['email']}\n"
                if p.get("phone"):
                    crm_section += f"    Phone: {p['phone']}\n"
                if p.get("city"):
                    crm_section += f"    City: {p['city']}\n"
            sections.append(crm_section.rstrip())
        else:
            sections.append("[Twenty CRM] No contacts found for " + name)
    else:
        sections.append("[Twenty CRM] Unavailable (no API token)")

    if sections:
        header = f"Context on {name}:\n\n"
        return header + "\n\n".join(sections)
    else:
        return f"No context found for {name} in available data sources."


# ---------------------------------------------------------------------------
# MCP tools shim (for when running as scheduled task with MCP access)
# ---------------------------------------------------------------------------
def send_telegram_notification(text: str):
    """Send a Telegram notification to the user via the MCP inbox server."""  # noname
    # This is called when running as a subagent with MCP access.
    # When running standalone (e.g., cron), this is a no-op.
    # The main dispatcher will see the write_task_output and relay if needed.
    pass


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    # Load config
    if not BOT_TALK_TOKEN_FILE.exists():
        print("[ERROR] Bot-talk token file not found", file=sys.stderr)
        return {"status": "failed", "output": "Bot-talk token file not found"}

    bot_token = BOT_TALK_TOKEN_FILE.read_text().strip()
    state = load_state()
    last_ts = state.get("last_processed_ts", "2026-01-01T00:00:00Z")

    # Load Google credentials
    access_token = get_google_access_token()

    # Load Twenty CRM token (if available)
    twenty_token = None
    if TWENTY_TOKEN_FILE.exists():
        twenty_token = TWENTY_TOKEN_FILE.read_text().strip()

    # Fetch new messages from AlbertLobster
    messages = bot_talk_get_messages(bot_token, since=last_ts, sender="AlbertLobster")

    # Filter for query messages
    query_messages = [m for m in messages if is_query_message(m)]

    if not query_messages:
        save_state(state)
        return {
            "status": "success",
            "output": f"No new queries. Checked {len(messages)} messages since {last_ts}.",
        }

    # Process each query
    handled = []
    latest_ts = last_ts

    for msg in query_messages:
        content = msg.get("content", "")
        name = extract_query_name(content)
        if not name:
            continue

        print(f"[INFO] Processing query about: {name}")

        # Build context reply
        reply = build_context_reply(name, access_token, twenty_token)

        # Send via bot-talk
        sent = bot_talk_send(bot_token, reply, recipient="AlbertLobster")

        if sent:
            handled.append({"name": name, "msg_id": msg.get("id", "?")})
            print(f"[INFO] Replied with context for: {name}")
        else:
            print(f"[WARN] Failed to send reply for: {name}", file=sys.stderr)

        # Track latest timestamp
        msg_ts = msg.get("timestamp", "")
        if msg_ts > latest_ts:
            latest_ts = msg_ts

    # Update state
    if latest_ts > last_ts:
        state["last_processed_ts"] = latest_ts
        save_state(state)

    if handled:
        output = f"Handled {len(handled)} context queries: " + ", ".join(
            f"'{h['name']}'" for h in handled
        )
        return {"status": "success", "output": output, "handled": handled}
    else:
        return {"status": "success", "output": "Query messages found but none extracted a name."}


if __name__ == "__main__":
    result = main()
    print(json.dumps(result, indent=2))
