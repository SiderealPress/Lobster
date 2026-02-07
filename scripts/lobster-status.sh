#!/bin/bash
#
# Lobster Status - Check if Lobster is running
#

SESSION_NAME="lobster"

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo "=== Lobster Status ==="
echo ""

# Check tmux session
if tmux -L lobster has-session -t "$SESSION_NAME" 2>/dev/null; then
    echo -e "Claude Session: ${GREEN}RUNNING${NC}"
    echo "  Attach: tmux -L lobster attach -t $SESSION_NAME"
else
    echo -e "Claude Session: ${RED}NOT RUNNING${NC}"
    echo "  Start:  ~/lobster/scripts/start-lobster.sh"
fi

echo ""

# Check telegram bot
if systemctl is-active --quiet lobster-router; then
    echo -e "Telegram Bot:   ${GREEN}RUNNING${NC}"
else
    echo -e "Telegram Bot:   ${RED}NOT RUNNING${NC}"
    echo "  Start:  sudo systemctl start lobster-router"
fi

echo ""

# Check inbox
INBOX_COUNT=$(ls -1 ~/messages/inbox/*.json 2>/dev/null | wc -l)
echo "Inbox Messages: $INBOX_COUNT"

# Check outbox
OUTBOX_COUNT=$(ls -1 ~/messages/outbox/*.json 2>/dev/null | wc -l)
echo "Pending Replies: $OUTBOX_COUNT"

echo ""
