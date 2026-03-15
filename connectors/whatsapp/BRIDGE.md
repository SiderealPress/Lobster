# WhatsApp Bridge

The WhatsApp bridge is implemented as a standalone Node.js service located at:

```
$LOBSTER_PROJECTS/whatsapp-bridge/
```

Full path (default): `/home/admin/lobster-workspace/projects/whatsapp-bridge/`

## Architecture

The bridge is intentionally kept outside this repo because:

1. It has its own Docker container lifecycle (requires Chromium)
2. Session data (`session/`, `.wwebjs_auth/`) must never be committed to git
3. It can be upgraded independently of the Lobster core

## Key files

| File | Purpose |
|------|---------|
| `index.js` | Main bridge — streams message events to stdout as newline-delimited JSON |
| `commands.js` | Watches `~/messages/wa-commands/` for send-command JSON files |
| `Dockerfile` | Node 18 + Chromium for containerized deployment |
| `docker-compose.yml` | Mounts session volume and wa-commands directory |

## Message event format (stdout)

```json
{
  "id": "<msg-id>",
  "body": "Hello",
  "from": "15551234567@c.us",
  "fromMe": false,
  "isGroup": false,
  "author": null,
  "timestamp": 1700000000,
  "mentionedIds": [],
  "hasMedia": false,
  "type": "chat"
}
```

## Send command format

Drop a JSON file in `~/messages/wa-commands/`:

```json
{ "to": "15551234567@c.us", "body": "Hello from Lobster" }
```

The file is deleted after processing.

## Running

```bash
# From the bridge directory
cd $LOBSTER_PROJECTS/whatsapp-bridge
npm install
node index.js          # start bridge (streams events to stdout)
node commands.js       # start command processor (separate process)

# Or with Docker
docker compose up --build
```

## Related

- BIS-45: WhatsApp connector — parent feature
- BIS-46: This bridge implementation (Slice 1)
