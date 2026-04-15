## Slack Connector — Telegram Onboarding Flow

Trigger: `/slack-setup`, `/slack connect`, or `/slack configure`

When the user types any of these commands, begin the interactive guided setup over Telegram. No terminal access required. The entire flow happens in chat.

---

### Before starting

Call `slack_onboarding_state(op="get", chat_id=<chat_id>)` to check for an existing in-progress flow. If `step` is not `mode_select` and not `done` or `cancelled`, the user was mid-flow. Say:

> "Looks like you were in the middle of Slack setup. Want to pick up where you left off, or start over?"
> Buttons: `[Continue]` `[Start over]`

If they choose Start over, call `slack_onboarding_state(op="clear", chat_id=<chat_id>)` then begin from Step 1.

---

### Step 1 — Mode selection

Send:

> "Which Slack account type do you want Lobster to use?"
>
> **Bot account** — Lobster connects as a Slack App. Recommended for most teams. No seat consumed.
>
> **Person account** — Lobster connects as a real user. Sees all messages (not just mentions). Uses a paid seat.

Buttons: `[Bot account (recommended)]` `[Person / user seat]`

Save: `slack_onboarding_state(op="set", chat_id=<chat_id>, step="bot_guide_1", mode="bot")` for bot path, or `step="person_token", mode="person"` for person path.

---

### Step 2 (Bot path) — App creation guide, part 1 of 5

Send one step at a time. Wait for "Done" before the next step.

**Step 2a:**

> "Let's create your Slack App. Open this link in a browser:
> https://api.slack.com/apps
>
> Click **Create New App** → **From scratch**. Name it "Lobster" and pick your workspace."

Button: `[Done ✓]`

Save step: `step="bot_guide_2"`

**Step 2b:**

> "Now enable Socket Mode:
>
> In your app settings, click **Socket Mode** → toggle it on → click **Generate an App-Level Token** → name it anything → add scope `connections:write` → click Generate.
>
> Copy the token (starts with `xapp-`). You'll need it shortly."

Button: `[Done ✓]`

Save step: `step="bot_guide_3"`

**Step 2c:**

> "Add Bot Token Scopes:
>
> Go to **OAuth & Permissions** → scroll to **Bot Token Scopes** → click **Add an OAuth Scope** and add all of these:
>
> `channels:history` `channels:read` `groups:history` `groups:read` `im:history` `im:read` `mpim:history` `mpim:read` `chat:write` `users:read` `reactions:read` `files:read`"

Button: `[Done ✓]`

Save step: `step="bot_guide_4"`

**Step 2d:**

> "Subscribe to Bot Events:
>
> Go to **Event Subscriptions** → toggle **Enable Events** → under **Subscribe to Bot Events**, add:
>
> `message.channels` `message.groups` `message.im` `message.mpim` `reaction_added` `app_mention` `file_shared`"

Button: `[Done ✓]`

Save step: `step="bot_guide_5"`

**Step 2e:**

> "Almost there! Install the app:
>
> Go to **OAuth & Permissions** → click **Install to Workspace** → click **Allow**.
>
> Copy the **Bot User OAuth Token** (starts with `xoxb-`)."

Button: `[Done ✓]`

Save step: `step="bot_token"`

---

### Step 3 (Bot path) — Token collection

**Bot token:**

> "Paste your Bot Token (starts with `xoxb-`):"

When the user replies with a token:

1. Note the Telegram message_id of their reply — save it: `slack_onboarding_state(op="set", ..., last_token_message_id=<id>)`
2. Validate format: must start with `xoxb-` and have at least 4 dash-separated parts. If invalid, say:
   > "That doesn't look right — bot tokens start with `xoxb-` and look like `xoxb-TEAM-BOT-SECRET`. Try again:"
3. Call `auth.test` via Slack SDK. If it fails, say:
   > "Slack rejected that token (`<error>`). Double-check you copied the **Bot User OAuth Token** from OAuth & Permissions, then try again:"
4. On success: immediately call `delete_telegram_message(chat_id, last_token_message_id)` to delete the token message. Then say:
   > "✓ Token deleted from chat for security."
5. Save: `slack_onboarding_state(op="set", ..., bot_token=<token>, workspace_name=<name>, step="app_token")`

**App token:**

> "Now paste your App-Level Token (starts with `xapp-`):"

Same pattern:

1. Save message_id
2. Validate format: must start with `xapp-`
3. If invalid: "App tokens start with `xapp-`. Try again:"
4. On success: delete the message, confirm deletion
5. Save: `step="channel_select"`, `app_token=<token>`

---

### Step 4 — Channel selection

Call `list_workspace_channels(bot_token)` to fetch the user's Slack channels.

If fewer than 25 channels, show them as inline buttons (multi-select pattern). If more than 25, ask the user to type channel names separated by commas.

> "Which channels should Lobster monitor? Tap to select (tap again to deselect), then tap **Done** when finished:"

Show channels as buttons, one per row (or 2-per-row for short names), plus a `[Done selecting]` button.

Handle toggle: if a channel_id is already in `selected_channels`, remove it; otherwise add it. Update `available_channels` and `selected_channels` in state on each tap.

When user taps `[Done selecting]`:

- If no channels selected: "You didn't select any channels. Tap at least one, then Done:"
- Otherwise: save `step="channel_modes"`, proceed to Step 5

Save: `slack_onboarding_state(op="set", ..., selected_channels=[...], step="channel_modes")`

---

### Step 5 — Mode per channel

For each selected channel (in order), ask:

> "How should Lobster behave in **#channel-name**?"
>
> Buttons: `[Monitor only]` `[Respond to mentions]` `[Full participant]`

Mode meanings (include on first channel, skip for subsequent):

- **Monitor only** — log all messages, never reply
- **Respond to mentions** — log everything, reply when @mentioned
- **Full participant** — log everything, reply to all messages

After the user picks a mode for each channel, save that channel's mode and move to the next.

When all channels are done: `step="confirm"` (or `step="person_confirm"` for person path)

Save: `slack_onboarding_state(op="set", ..., channel_modes={"C123": "monitor", ...}, step="confirm")`

---

### Step 6 — Confirmation

Show a summary:

> "Here's what I'll configure:
>
> **Workspace:** `<workspace_name>`
> **Mode:** Bot account
> **Bot Token:** `xoxb-****<last4>`
> **App Token:** `xapp-****<last4>`
>
> **Channels:**
> • #general — Monitor only
> • #dev — Respond to mentions
>
> Confirm and activate?"

Buttons: `[Confirm and activate]` `[Cancel]`

On **Confirm**:

1. Call `write_channels_config(channel_selections)` — writes `channels.yaml`
2. Call `write_bot_config(bot_token, app_token)` or `write_person_config(person_token)` — writes `config.env`
3. Call `restart_ingress_service()` — restarts `lobster-slack-connector`
4. Call `slack_onboarding_state(op="set", ..., step="done")`
5. Send:

> "All done! Slack is connected.
>
> Lobster is now monitoring your selected channels. Use `/slack-status` to check the connection.
>
> To invite Lobster to a private channel: `/invite @Lobster` in Slack."

On **Cancel**: see Cancellation section below.

---

### Person-seat path

After mode selection (person), skip the app creation guide and go straight to:

> "You'll need a **User OAuth Token** (starts with `xoxp-`) for the Lobster Slack account.
>
> Create a Slack App at https://api.slack.com/apps, add **User Token Scopes** (not bot): `channels:history channels:read groups:history groups:read im:history im:read mpim:history channels:write users:read reactions:read files:read`, then install it **while logged in as the Lobster user account**.
>
> Note: person mode uses a paid Slack seat and Lobster will appear as a team member."

When they provide the token:

1. Must start with `xoxp-`
2. Validate via `auth.test` — shows the user name
3. Delete the token message immediately
4. Save `person_token`, `step="person_channel_select"`

Then proceed through channel selection and mode selection as in the bot path.

On confirmation, call `write_person_config(person_token)` instead of `write_bot_config`.

---

### Cancellation

If the user types `/cancel` at any point during the flow:

1. Call `slack_onboarding_state(op="clear", chat_id=<chat_id>)`
2. Reply: "Setup cancelled. Type `/slack-setup` anytime to start again."

---

### Error handling

| Situation | Message |
|---|---|
| Token format wrong | "That doesn't look right — [expected format]. Try again:" |
| `auth.test` fails (`invalid_auth`) | "Slack rejected that token. Check you copied the right token." |
| `auth.test` fails (network error) | "Couldn't reach Slack — check your internet connection and try again." |
| `list_workspace_channels` returns empty | "I couldn't fetch your channel list. Make sure the bot is installed in your workspace. Try `/slack-setup` again." |
| `write_channels_config` fails | "Couldn't write channels config — let me know and we'll sort it out. Error: `<message>`" |
| `restart_ingress_service` fails | "Tokens saved, but couldn't restart the service. Run `systemctl restart lobster-slack-connector` manually." |

---

### State machine summary

```
mode_select
  └─► bot_guide_1 → bot_guide_2 → bot_guide_3 → bot_guide_4 → bot_guide_5
        └─► bot_token → app_token → channel_select → channel_modes → confirm → done
  └─► person_token → person_channel_select → channel_modes → person_confirm → done
```

At any step: `/cancel` → cancelled (clears state)

---

### Token security rules

- **NEVER** log, display, or include raw token values in any message to the user beyond the deletion confirmation
- **ALWAYS** delete the Telegram message containing the token immediately after reading it
- When showing tokens in summaries, use `mask_token()` format: `xoxb-****abcd`
- If `delete_telegram_message` fails, still proceed but warn: "Couldn't auto-delete your token message — please delete it manually for security."
