# Granola → Obsidian Sync

**Job**: granola-sync
**Schedule**: `*/30 * * * *` (every 30 minutes)
**Created**: 2026-04-14

## Context

You are a scheduled sync agent. Your job is to pull all new/updated Granola meeting notes into the Obsidian vault at `~/lobster-workspace/obsidian-vault/` and git-commit the results.

This is a raw ingest job — no LLM summarisation, no token spend beyond this invocation. The Python sync script handles all API calls and file I/O.

## Task

Run the Granola sync script:

```bash
cd ~/lobster
source ~/lobster-config/config.env

# Run the sync (Python module)
uv run python -m integrations.granola.sync
```

The script will:
1. Read the last-sync timestamp from `~/lobster-workspace/data/granola-sync-state.json`
2. Fetch all Granola notes created/updated since last sync (full sync on first run)
3. Write each note as `granola/YYYY/MM/YYYY-MM-DD-{slug}.md` in the vault
4. Git-commit the vault with message: `granola: sync {N} notes [{timestamp}]`
5. Update the state file
6. Print a JSON result summary to stdout

## Expected output

The script writes JSON to stdout. Capture it and pass to `write_task_output`.

Example success output:
```json
{
  "status": "success",
  "notes_fetched": 5,
  "notes_written": 4,
  "notes_skipped": 1,
  "notes_errored": 0,
  "committed": true,
  "last_sync_at": "2026-04-14T10:30:00.000Z",
  "vault_path": "/home/lobster/lobster-workspace/obsidian-vault",
  "message": "Synced 4 new/updated notes, skipped 1 unchanged"
}
```

## Error handling

- If the script exits with code 1, the sync failed. Log the output and mark the job as failed.
- Auth errors (GRANOLA_API_KEY missing/invalid) will appear in stderr.
- On failure, call `write_task_output` with `status="failed"`.

## Reporting

After running:

1. Call `write_task_output` with:
   - `job_name`: `granola-sync`
   - `output`: The JSON output from the script (or error message)
   - `status`: `"success"` or `"failed"`

2. If `notes_written > 0`, send a Telegram notification to the admin (chat_id: ADMIN_CHAT_ID_REDACTED):
   - Message: `Granola sync: {notes_written} new notes added to vault. [{timestamp}]`
   - Keep it brief — only send if there are actually new notes.

3. If status is `"failed"`, always notify the admin with the error.

4. Call `write_result` with a concise summary.

## Dry run (for testing)

```bash
uv run python -m integrations.granola.sync --dry-run
```

This fetches notes but does not write to disk or update state.
