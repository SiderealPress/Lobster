# scheduled-tasks/tasks/

This directory contains **task file templates** for Lobster scheduled jobs.

## What belongs here

Only generic, instance-agnostic content:

- **`*.md.template`** — Template task files with `{{PLACEHOLDER}}` syntax. These
  describe job behavior but contain no hardcoded IPs, chat_ids, API URLs, or user
  identities. Instantiate them by replacing placeholders with real values and
  registering via `create_scheduled_job`.
- **`nightly-github-backup.md`** — A fully generic task file that uses only `$ENV_VAR`
  references (no hardcoded values). Acceptable in the public repo as-is.

## What does NOT belong here

Task files with hardcoded instance data must **not** be committed to this directory:

- Hardcoded IP addresses or hostnames
- Hardcoded Telegram `chat_id` values
- User identity strings (persona names, email addresses)
- Any value that would differ between Lobster instances

Instance-specific task files belong in `~/lobster-workspace/scheduled-jobs/tasks/`
and are created by the running Lobster instance via MCP tools (`create_scheduled_job`),
or by user-upgrade hooks in `~/lobster-user-config/`.

## Template placeholder syntax

Templates use `{{PLACEHOLDER_NAME}}` for values that must be filled in at instantiation
time. Common placeholders:

| Placeholder | Description |
|---|---|
| `{{TELEGRAM_CHAT_ID}}` | Owner's Telegram chat ID |
| `{{LOBSTERTALK_HOST}}` | Bot-talk API base URL (e.g. `http://your-server:4242`) |
| `{{LOCAL_LOBSTER_IDENTITY}}` | This instance's identity name in bot-talk |
| `{{REMOTE_LOBSTER_IDENTITY}}` | The remote Lobster's identity name |
| `{{TWENTY_CRM_API_URL}}` | Twenty CRM GraphQL endpoint |

## Available templates

| Template | Job | Description |
|---|---|---|
| `bot-talk-poller.md.template` | `bot-talk-poller` | Hourly baseline poller for bot-talk messages |
| `bot-talk-poller-fast.md.template` | `bot-talk-poller-fast` | 2-minute fast poller (hot-mode only) |

## How to instantiate a template

1. Copy the template and fill in placeholders:
   ```bash
   sed \
     -e 's/{{TELEGRAM_CHAT_ID}}/1234567890/g' \
     -e 's|{{LOBSTERTALK_HOST}}|http://your-server:4242|g' \
     -e 's/{{LOCAL_LOBSTER_IDENTITY}}/MyLobster/g' \
     -e 's/{{REMOTE_LOBSTER_IDENTITY}}/TheirLobster/g' \
     scheduled-tasks/tasks/bot-talk-poller.md.template \
     > /tmp/bot-talk-poller-instantiated.md
   ```

2. Register via MCP `create_scheduled_job`, passing the instantiated content as the
   context. The MCP tool writes the result to
   `~/lobster-workspace/scheduled-jobs/tasks/<job-name>.md`.

The live task files in `~/lobster-workspace/scheduled-jobs/tasks/` are the canonical
operational versions. They are never committed to the public repo.
