## Slack Connector — Onboarding

When the user first activates the slack-connector skill, guide them through setup:

1. **Token check** — Verify `LOBSTER_SLACK_BOT_TOKEN` and `LOBSTER_SLACK_APP_TOKEN` are set in `~/lobster-config/config.env`. If missing, explain how to create a Slack App with Socket Mode enabled and obtain both tokens.

2. **Channel selection** — Ask which channels to monitor. Create the initial `channels.yaml` from their response. Default to logging all public channels the bot is invited to.

3. **Ingress preferences** — Confirm default logging settings: messages, reactions, files, and edits are logged; deletes are not. Explain they can change these later via skill preferences.

4. **First run** — Run `install.sh` to install dependencies and start the ingress worker. Confirm the Socket Mode connection is established and messages are being logged.

5. **Verification** — After a few minutes, run `/slack-status` to show the user that messages are flowing and logs are accumulating.
