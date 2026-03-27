## Email Autoresponder — Behavior

This skill manages a scheduled Gmail auto-draft job (`gmail-auto-draft`). It runs every 5 minutes, scans the inbox, and creates draft replies — never sending them.

---

### Toggle commands

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

### Natural language patterns to recognize

| Pattern | Intent |
|---------|--------|
| "turn on/off email autoresponder" | Toggle the job |
| "enable/disable auto-drafting" | Toggle the job |
| "start/stop email drafts" | Toggle the job |
| "is the autoresponder running?" | Status check |
| "what emails did you draft?" | Show recent results |
| "show email autoresponder results" | Show recent results |
| "/autoresponder", "/autodraft", "/email" | Show status + toggle options |

---

### Response format

Keep replies concise (mobile-first). For status:

```
Email autoresponder: ON
Last run: 3 minutes ago (success)
Schedule: every 5 minutes

Commands:
- Turn off: "stop autoresponder"
- See results: "show email drafts"
```

---

### Important rules

- NEVER trigger or run the email processing logic directly — it runs as a scheduled job
- NEVER send emails on behalf of the user — the job only creates drafts
- NEVER re-enable the job without the user asking — respect their toggle
- Always confirm the toggle with a clear on/off status message
