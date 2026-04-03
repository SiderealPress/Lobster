## Slack Connector — Domain Reference

### Socket Mode vs Events API

Slack offers two primary mechanisms for receiving events:

**Socket Mode** (used by this skill): The app opens a WebSocket connection to Slack's servers. Events are pushed over the socket in real time. No public URL or webhook endpoint is required — ideal for self-hosted setups behind NAT/firewalls. Requires an app-level token (`xapp-`). Tradeoff: the connection must be maintained continuously; if it drops, events are buffered by Slack for up to ~30 seconds before being lost. The `slack-bolt` library handles reconnection automatically.

**Events API**: Slack sends HTTP POST requests to a public endpoint you configure. More resilient to brief outages (Slack retries for up to 3 hours with exponential backoff). Requires a publicly reachable HTTPS URL. More complex to set up for self-hosted environments.

For Lobster's use case (single-server, self-hosted, no public URL), Socket Mode is the clear choice.

### Channel types

| Type | API prefix | Description |
|------|-----------|-------------|
| Public channel | `C` | Open to all workspace members |
| Private channel | `G` | Invite-only, not visible to non-members |
| DM (im) | `D` | Direct message between two users |
| Group DM (mpim) | `G` | Multi-party direct message |

Bot tokens can only access channels the bot has been invited to. User tokens can access any channel the user is a member of.

### Timestamp format (`ts`)

Slack uses a unique timestamp format: `"1234567890.123456"` — Unix epoch seconds with a microsecond suffix. This serves as both a timestamp and a unique message ID within a channel. Thread replies reference the parent message's `ts` as `thread_ts`.

### Thread model

- A message becomes a thread parent when it receives its first reply
- Replies carry both their own `ts` and the parent's `thread_ts`
- `reply_count` and `latest_reply` are available on the parent message
- Thread replies do not appear in the channel timeline unless explicitly broadcast (`subtype: "thread_broadcast"`)

### Rate limits

Slack API methods are grouped into tiers with different rate limits:

| Tier | Rate limit | Examples |
|------|-----------|----------|
| Tier 1 | 1 req/min | `admin.*`, `migration.*` |
| Tier 2 | 20 req/min | `conversations.list`, `users.list` |
| Tier 3 | 50 req/min | `conversations.history`, `conversations.replies` |
| Tier 4 | 100 req/min | `chat.postMessage`, `reactions.add` |

Rate limit responses return HTTP 429 with a `Retry-After` header (seconds). The `slack-sdk` handles retry automatically when configured with `retry_handlers`.

### Bot vs user token capabilities

| Capability | Bot token (`xoxb-`) | User token (`xoxp-`) |
|-----------|---------------------|---------------------|
| Read public channels (joined) | ✓ | ✓ |
| Read private channels (joined) | ✓ | ✓ |
| Read all public channels | ✗ | ✓ |
| Post messages | ✓ | ✓ |
| Read DMs | Only bot DMs | All user DMs |
| Access user profile | ✓ (basic) | ✓ (full) |
| Admin actions | ✗ | ✓ (if admin) |
| Socket Mode | ✓ (via app token) | ✗ |

The `account_type` preference controls which token mode is used. Bot mode is the default and recommended approach. Person mode (user token) is available for use cases requiring broader access but carries higher risk — the token has the full permissions of the user account.
