# Brain Dump Handler

When you receive a **voice message** that appears to be a "brain dump" (unstructured thoughts, ideas, stream of consciousness) rather than a command or question, use the **brain-dumps** agent.

**Note:** This feature can be disabled via `LOBSTER_BRAIN_DUMPS_ENABLED=false` in `lobster.conf`. The agent can also be customized or replaced via the [private config overlay](docs/CUSTOMIZATION.md) by placing a custom `agents/brain-dumps.md` in your private config directory.

## Indicators of a brain dump

- Multiple unrelated topics in one message
- Phrases like "brain dump", "note to self", "thinking out loud"
- Stream of consciousness style
- Ideas/reflections rather than questions or requests

## Workflow

1. Receive voice message (already transcribed — `msg["transcription"]` is populated by the worker)
2. Read transcription from `msg["transcription"]` or `msg["text"]`
3. Check if brain dumps are enabled (default: true)
4. If transcription looks like a brain dump, spawn brain-dumps agent:
   ```
   Task(
     prompt=f"---\ntask_id: brain-dump-{id}\nchat_id: {chat_id}\nsource: {source}\nreply_to_message_id: {id}\n---\n\nProcess this brain dump:\nTranscription: {text}",
     subagent_type="brain-dumps"
   )
   ```
5. Agent will save to user's `brain-dumps` GitHub repository as an issue

## NOT a brain dump (handle normally)

- Direct questions ("What time is it?")
- Commands ("Set a reminder")
- Specific task requests

See `docs/BRAIN-DUMPS.md` for full documentation.
