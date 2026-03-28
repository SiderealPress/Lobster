## LobsterTalk Context Handler — Behavior

This skill manages the `lobstertalk-incoming-handler` scheduled job. It runs every 5 minutes and answers context queries from AlbertLobster via bot-talk.

---

### Toggle commands

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

### Checking recent query results

When the user asks "what did Albert ask?", "show lobstertalk results", "what queries came in":

```
send_reply(chat_id, "Checking recent LobsterTalk query activity...")
Task(prompt="Call check_task_outputs with job_name='lobstertalk-incoming-handler', limit=5. Summarize what queries were received and how they were answered. Send to chat_id X.", subagent_type="general-purpose")
```

---

### Natural language patterns to recognize

| Pattern | Intent |
|---------|--------|
| "/lobstertalk", "/botquery" | Show status + toggle options |
| "what did Albert ask?" | Show recent results |
| "enable/disable the context handler" | Toggle the job |
| "is the lobstertalk handler running?" | Status check |

---

### Response format

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

- NEVER trigger the context lookup logic directly — it runs as a scheduled job
- NEVER send bot-talk messages on behalf of the user — the job handles replies
- NEVER re-enable the job without the user asking
