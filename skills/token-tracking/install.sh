#!/bin/bash
# Token tracking skill installer
#
# Adds usage-accumulator.py to PostToolUse hooks in ~/.claude/settings.json
# and schedules the nightly rollup cron job.
#
# Usage: bash ~/lobster/skills/token-tracking/install.sh

set -euo pipefail

LOBSTER_DIR="${LOBSTER_SRC:-$HOME/lobster}"
SETTINGS_FILE="$HOME/.claude/settings.json"
HOOK_CMD="python3 $LOBSTER_DIR/hooks/usage-accumulator.py"
CRON_MARKER="# LOBSTER-TOKEN-ROLLUP"
CRON_ENTRY="5 3 * * * uv run $LOBSTER_DIR/scripts/nightly-token-rollup.py >> $HOME/lobster-workspace/logs/nightly-token-rollup.log 2>&1 $CRON_MARKER"

echo "[token-tracking] Installing hook into $SETTINGS_FILE..."

# Check if hook is already registered
if grep -q "usage-accumulator" "$SETTINGS_FILE" 2>/dev/null; then
    echo "[token-tracking] Hook already registered in settings.json, skipping."
else
    # Use python to safely insert the hook into PostToolUse
    python3 - "$SETTINGS_FILE" "$HOOK_CMD" << 'PYEOF'
import json, sys

settings_file = sys.argv[1]
hook_cmd = sys.argv[2]

with open(settings_file, "r") as f:
    settings = json.load(f)

hooks = settings.setdefault("hooks", {})
post_tool_use = hooks.setdefault("PostToolUse", [])

new_entry = {
    "matcher": "Agent",
    "hooks": [
        {
            "type": "command",
            "command": hook_cmd,
            "timeout": 10
        }
    ]
}

post_tool_use.append(new_entry)

with open(settings_file, "w") as f:
    json.dump(settings, f, indent=4)
    f.write("\n")

print(f"[token-tracking] Hook added to PostToolUse in {settings_file}")
PYEOF
fi

echo "[token-tracking] Scheduling nightly rollup cron job..."
"$LOBSTER_DIR/scripts/cron-manage.sh" add "$CRON_MARKER" "$CRON_ENTRY"
echo "[token-tracking] Done. Restart MCP to activate the hook: ~/lobster/scripts/restart-mcp.sh"
