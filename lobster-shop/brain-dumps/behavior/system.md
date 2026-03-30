## Brain Dump Routing

When a transcribed voice message arrives, the dispatcher must decide whether it is a brain dump or a regular message.

### Detection

Route to the `brain-dumps` agent when ALL of these are true:
1. The message is a voice note (pre-transcribed; read from `msg["transcription"]`)
2. `LOBSTER_BRAIN_DUMPS_ENABLED` is not `"false"` (env var check)
3. The content looks like a brain dump (see table below)

| IS a brain dump | NOT a brain dump |
|----------------|-----------------|
| Stream of consciousness | Direct questions ("What time is it?") |
| Multiple unrelated topics | Commands ("Set a reminder for...") |
| Random ideas or thoughts | Specific task requests |
| Phrases like "brain dump", "note to self", "thinking out loud" | Single focused topic requiring action |
| Personal reflections with no clear ask | Requests for information |

### Dispatcher behavior (main thread)

When the voice note is identified as a brain dump:

```
1. send_reply(chat_id, "On it — processing your brain dump.", message_id=message_id)
   # send_reply with message_id atomically marks it processed
2. Spawn brain-dumps agent (run_in_background=True):
   Task(
     prompt=(
       "---\n"
       f"task_id: brain-dump-{message_id}\n"
       f"chat_id: {chat_id}\n"
       f"source: {source}\n"
       f"reply_to_message_id: {telegram_message_id}\n"
       "---\n\n"
       f"Process this brain dump:\n"
       f"Transcription: {transcription}"
     ),
     subagent_type="brain-dumps",
     run_in_background=True,
   )
3. Return to wait_for_messages() immediately
```

When the voice note is NOT a brain dump, handle it normally (reply inline or delegate as appropriate for the content).

### Full processing pipeline

The `brain-dumps` agent (`.claude/agents/brain-dumps.md`) handles all staged processing:
- Stage 1: Triage (classify type, extract entities, assess urgency)
- Stage 2: Context matching (match to known projects, people, goals)
- Stage 3: Enrichment (labels, links, action items)
- Stage 4: Context update suggestions

The agent saves enriched output as a GitHub issue in the user's brain-dumps repository.
