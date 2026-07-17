# Fireflies → Obsidian Sync

**Job**: fireflies-sync
**Schedule**: `*/30 * * * *` (every 30 minutes) — proposed, mirrors granola-sync
**Status**: NOT YET ACTIVATED — see "Activation" below

## Context

You are a scheduled sync agent. Your job is to pull all new Fireflies call
transcripts (across every configured team member's Fireflies account) into
the Obsidian vault at `~/lobster-workspace/obsidian-vault/` and git-commit
the results.

This is a raw ingest job — no LLM summarisation, no token spend beyond this
invocation. Fireflies' own AI summary (including `action_items`) is captured
verbatim; the Python sync script handles all API calls and file I/O.

## Task

Run the Fireflies sync script:

```bash
cd ~/lobster
source ~/lobster-config/config.env

# Run the sync (Python module)
uv run python -m integrations.fireflies.sync
```

The script will:
1. Read the last-sync timestamp from `~/lobster-workspace/data/fireflies-sync-state.json`
2. Fetch all transcripts created since last sync, for every configured account
   (`FIREFLIES_API_KEY` plus any `FIREFLIES_API_KEY_<NAME>`), full sync on first run
3. Write each transcript as `fireflies/YYYY/MM/YYYY-MM-DD-{slug}.md` in the vault,
   with a dedicated "Action Items" section up top
4. Git-commit the vault with message: `fireflies: sync {N} transcripts [{timestamp}]`
5. Update the state file
6. Print a JSON result summary to stdout

## Expected output

Example success output:
```json
{
  "status": "success",
  "transcripts_fetched": 3,
  "transcripts_written": 3,
  "transcripts_skipped": 0,
  "transcripts_errored": 0,
  "committed": true,
  "last_sync_at": "2026-06-01T15:00:00.000Z",
  "vault_path": "/home/lobster/lobster-workspace/obsidian-vault",
  "accounts_polled": ["primary", "jake", "ben", "priya"],
  "message": "Synced 3 new/updated transcripts, skipped 0 unchanged"
}
```

If `FIREFLIES_API_KEY` is not set, the wrapper script exits 0 without error
(no key configured yet is an expected, not exceptional, state) — see
`scheduled-tasks/fireflies-sync.sh`.

## Error handling

- If the script exits with code 1, the sync failed. Log the output and mark the job as failed.
- Auth errors (FIREFLIES_API_KEY missing/invalid for any configured account) will appear in stderr.
- On failure, call `write_task_output` with `status="failed"`.

## Reporting

After running:

1. Call `write_task_output` with:
   - `job_name`: `fireflies-sync`
   - `output`: The JSON output from the script (or error message)
   - `status`: `"success"` or `"failed"`

2. If `transcripts_written > 0`, send a Telegram notification to the admin
   (chat_id from `LOBSTER_ADMIN_CHAT_ID` env var):
   - Message: `Fireflies sync: {transcripts_written} new call transcripts added to vault. [{timestamp}]`
   - Keep it brief — only send if there are actually new transcripts.

3. If status is `"failed"`, always notify the admin with the error.

4. Call `write_result` with a concise summary.

## Dry run (for testing)

```bash
uv run python -m integrations.fireflies.sync --dry-run
```

This fetches transcripts but does not write to disk or update state.

## Activation

This job definition (script + task doc) is committed and ready, but no live
systemd timer has been created for it yet — deliberately, since
`FIREFLIES_API_KEY` is not currently set in `~/lobster-config/config.env`.
Installing the timer before a key exists would mean it fails silently every
30 minutes for no operational benefit. Once the account owner adds the key(s), activate
with the `create_scheduled_job` MCP tool (or the systemd-timer equivalent
used for `granola-sync`):

```
create_scheduled_job(
    name="fireflies-sync",
    schedule="*/30 * * * *",
    context=<this file's content>,
)
```
