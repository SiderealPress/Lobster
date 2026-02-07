# Future Integrations

This document contains plans for future messaging integrations that are not yet implemented.

## Signal Integration

**Status:** Planned, not implemented

### Requirements
- signal-cli v0.13.4+
- Signal account with phone number
- signal-mcp server

### Setup (when implemented)
```bash
cd ~/mcp-servers/signal
SIGNAL_CLI_VERSION="0.13.4"
wget -q "https://github.com/AsamK/signal-cli/releases/download/v${SIGNAL_CLI_VERSION}/signal-cli-${SIGNAL_CLI_VERSION}-Linux.tar.gz"
tar xf "signal-cli-${SIGNAL_CLI_VERSION}-Linux.tar.gz"
rm "signal-cli-${SIGNAL_CLI_VERSION}-Linux.tar.gz"
git clone https://github.com/rymurr/signal-mcp.git mcp-server
```

### Configuration
```bash
export SIGNAL_PHONE_NUMBER="+1234567890"
```

---

## Twilio SMS Integration

**Status:** Planned, not implemented

### Requirements
- Twilio account
- Twilio phone number
- @yiyang.1i/sms-mcp-server npm package

### Setup (when implemented)
```bash
npm install -g @yiyang.1i/sms-mcp-server
```

### Configuration
```bash
export TWILIO_ACCOUNT_SID="your_account_sid"
export TWILIO_AUTH_TOKEN="your_auth_token"
export TWILIO_FROM_NUMBER="+1234567890"
```

### MCP Server Config
```json
{
  "twilio-sms": {
    "command": "npx",
    "args": ["-y", "@yiyang.1i/sms-mcp-server"],
    "env": {
      "ACCOUNT_SID": "${TWILIO_ACCOUNT_SID}",
      "AUTH_TOKEN": "${TWILIO_AUTH_TOKEN}",
      "FROM_NUMBER": "${TWILIO_FROM_NUMBER}"
    }
  }
}
```

---

## Telegram User API (Direct)

**Status:** Planned, not implemented

This is different from the current bot-based integration. This would allow Claude to interact with Telegram as a user account.

### Requirements
- Telegram API ID and Hash from https://my.telegram.org/apps
- telegram-mcp server

### Setup (when implemented)
```bash
cd ~/mcp-servers/telegram
git clone https://github.com/chigwell/telegram-mcp.git .
python3 -m venv .venv
source .venv/bin/activate
pip install telethon python-dotenv mcp
```

### Configuration
```bash
export TELEGRAM_API_ID="your_api_id"
export TELEGRAM_API_HASH="your_api_hash"
export TELEGRAM_SESSION_NAME="lobster"
```
