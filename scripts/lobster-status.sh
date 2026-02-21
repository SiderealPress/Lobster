#!/bin/bash
#
# Lobster Status - Check if Lobster is running and show lifecycle state
#

SESSION_NAME="lobster"
MESSAGES_DIR="${LOBSTER_MESSAGES:-$HOME/messages}"
STATE_FILE="$MESSAGES_DIR/config/lobster-state.json"

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

echo "=== Lobster Status ==="
echo ""

# Lifecycle state
if [[ -f "$STATE_FILE" ]]; then
    LIFECYCLE_MODE=$(python3 -c "
import json
try:
    d = json.load(open('$STATE_FILE'))
    print(d.get('mode', 'unknown'))
except: print('unknown')
" 2>/dev/null || echo "unknown")
    LIFECYCLE_DETAIL=$(python3 -c "
import json
try:
    d = json.load(open('$STATE_FILE'))
    print(d.get('detail', ''))
except: print('')
" 2>/dev/null)
    LIFECYCLE_UPDATED=$(python3 -c "
import json
try:
    d = json.load(open('$STATE_FILE'))
    print(d.get('updated_at', ''))
except: print('')
" 2>/dev/null)

    case "$LIFECYCLE_MODE" in
        active)   echo -e "Lifecycle:      ${GREEN}ACTIVE${NC}" ;;
        hibernate) echo -e "Lifecycle:      ${CYAN}HIBERNATING${NC}" ;;
        starting|waking) echo -e "Lifecycle:      ${YELLOW}STARTING${NC}" ;;
        restarting) echo -e "Lifecycle:      ${YELLOW}RESTARTING${NC}" ;;
        backoff)  echo -e "Lifecycle:      ${YELLOW}BACKOFF${NC}" ;;
        stopped)  echo -e "Lifecycle:      ${RED}STOPPED${NC}" ;;
        *)        echo -e "Lifecycle:      ${YELLOW}UNKNOWN ($LIFECYCLE_MODE)${NC}" ;;
    esac

    [[ -n "$LIFECYCLE_DETAIL" ]] && echo "  Detail:  $LIFECYCLE_DETAIL"
    [[ -n "$LIFECYCLE_UPDATED" ]] && echo "  Updated: $LIFECYCLE_UPDATED"
else
    echo -e "Lifecycle:      ${YELLOW}NO STATE FILE${NC} (first run or old-style wrapper)"
fi
echo ""

# Check tmux session
if tmux -L lobster has-session -t "$SESSION_NAME" 2>/dev/null; then
    echo -e "Tmux Session:   ${GREEN}RUNNING${NC}"
    echo "  Attach: tmux -L lobster attach -t $SESSION_NAME"
else
    echo -e "Tmux Session:   ${RED}NOT RUNNING${NC}"
    echo "  Start:  ~/lobster/scripts/start-lobster.sh"
fi

# Check wrapper process
if pgrep -f "claude-persistent.sh" > /dev/null 2>&1; then
    echo -e "Wrapper:        ${GREEN}RUNNING${NC} (persistent)"
elif pgrep -f "claude-wrapper" > /dev/null 2>&1; then
    echo -e "Wrapper:        ${YELLOW}RUNNING${NC} (old-style)"
else
    echo -e "Wrapper:        ${RED}NOT RUNNING${NC}"
fi

# Check Claude process
if pgrep -f "claude.*--dangerously-skip-permissions" > /dev/null 2>&1; then
    echo -e "Claude Process: ${GREEN}RUNNING${NC}"
else
    if [[ "$LIFECYCLE_MODE" == "hibernate" ]]; then
        echo -e "Claude Process: ${CYAN}NOT RUNNING${NC} (expected: hibernating)"
    else
        echo -e "Claude Process: ${RED}NOT RUNNING${NC}"
    fi
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
INBOX_COUNT=$(find "$MESSAGES_DIR/inbox" -maxdepth 1 -name "*.json" 2>/dev/null | wc -l)
echo "Inbox Messages: $INBOX_COUNT"

# Check outbox
OUTBOX_COUNT=$(find "$MESSAGES_DIR/outbox" -maxdepth 1 -name "*.json" 2>/dev/null | wc -l)
echo "Pending Replies: $OUTBOX_COUNT"

echo ""
