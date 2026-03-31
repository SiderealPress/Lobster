## Email Autoresponder — Behavior

This skill manages two scheduled jobs:
- **`gmail-auto-draft`** — runs every 5 minutes, scans the inbox, and creates draft replies (never sends them)
- **`lobstertalk-incoming-handler`** — runs every 5 minutes, answers "what do you know about X?" context queries from AlbertLobster via bot-talk

---

### Email autoresponder toggle commands

When the user says "/autoresponder", "/autodraft", or "/email" — or asks about enabling/disabling email auto-drafting — check the current state and guide them.

#### Check if the job is currently enabled

```python
# Use list_scheduled_jobs or get_scheduled_job("gmail-auto-draft")
# Check the "enabled" field
```

#### Enable the autoresponder

If user says "enable", "start", "turn on", "activate" the autoresponder:

```python
# Call update_scheduled_job(name="gmail-auto-draft", enabled=True)
reply = "Email autoresponder is now ON. I'll check your inbox every 5 minutes and draft replies automatically."
```

#### Disable the autoresponder

If user says "disable", "stop", "turn off", "pause", "deactivate":

```python
# Call update_scheduled_job(name="gmail-auto-draft", enabled=False)
reply = "Email autoresponder is now OFF. I'll stop drafting replies until you turn it back on."
```

#### Status check

If user asks "is the autoresponder on?", "email status", "what's the autoresponder doing":

```python
# Call get_scheduled_job("gmail-auto-draft")
# Report: enabled/disabled, last run time, last status
```

---

### Checking recent draft results

When the user asks "what emails did you draft?", "show me the autoresponder results", "what happened with emails":

Delegate to a subagent (API call takes time):

```
send_reply(chat_id, "Checking recent email draft activity...")
Task(prompt="Call check_task_outputs with job_name='gmail-auto-draft', limit=5. Summarize what emails were processed, what drafts were created, and any notable items. Send the summary to chat_id X via send_reply.", subagent_type="general-purpose")
```

---

### LobsterTalk context handler toggle commands

When the user says "/lobstertalk", "/botquery", or asks about the incoming handler status:

#### Check current status

```python
# Use get_scheduled_job("lobstertalk-incoming-handler")
# Report: enabled/disabled, last run time, last status
```

#### Enable the handler

```python
# Call update_scheduled_job(name="lobstertalk-incoming-handler", enabled=True)
reply = "LobsterTalk context handler is now ON. I'll check for incoming queries every 5 minutes."
```

#### Disable the handler

```python
# Call update_scheduled_job(name="lobstertalk-incoming-handler", enabled=False)
reply = "LobsterTalk context handler is now OFF."
```

---

### Checking recent LobsterTalk query results

When the user asks "what did Albert ask?", "show lobstertalk results", "what queries came in":

```
send_reply(chat_id, "Checking recent LobsterTalk query activity...")
Task(prompt="Call check_task_outputs with job_name='lobstertalk-incoming-handler', limit=5. Summarize what queries were received and how they were answered. Send to chat_id X.", subagent_type="general-purpose")
```

---

### Natural language patterns to recognize

| Pattern | Intent |
|---------|--------|
| "turn on/off email autoresponder" | Toggle gmail-auto-draft |
| "enable/disable auto-drafting" | Toggle gmail-auto-draft |
| "start/stop email drafts" | Toggle gmail-auto-draft |
| "is the autoresponder running?" | Status check (gmail-auto-draft) |
| "what emails did you draft?" | Show recent email results |
| "show email autoresponder results" | Show recent email results |
| "/autoresponder", "/autodraft", "/email" | Show email status + toggle options |
| "/lobstertalk", "/botquery" | Show lobstertalk status + toggle options |
| "what did Albert ask?" | Show recent lobstertalk results |
| "enable/disable the context handler" | Toggle lobstertalk-incoming-handler |
| "is the lobstertalk handler running?" | Status check (lobstertalk-incoming-handler) |

---

### Response format

Keep replies concise (mobile-first). For email status:

```
Email autoresponder: ON
Last run: 3 minutes ago (success)
Schedule: every 5 minutes

Commands:
- Turn off: "stop autoresponder"
- See results: "show email drafts"
```

For lobstertalk status:

```
LobsterTalk context handler: ON
Last run: 2 minutes ago (success)
Schedule: every 5 minutes

Commands:
- Turn off: "disable lobstertalk handler"
- See results: "what did Albert ask?"
```

---

### Important rules

- NEVER trigger or run the email processing logic directly — it runs as a scheduled job
- NEVER send emails on behalf of the user — the job only creates drafts
- NEVER re-enable either job without the user asking — respect their toggle
- NEVER trigger the context lookup logic directly — it runs as a scheduled job
- NEVER send bot-talk messages on behalf of the user — the job handles replies
- Always confirm any toggle with a clear on/off status message
