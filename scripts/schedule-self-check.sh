#!/bin/bash
#===============================================================================
# Schedule Self-Check Reminder
#
# Called automatically by Claude Code PostToolUse hook after Task tool runs.
# Schedules a self-check reminder 3 minutes in the future via `at`.
#===============================================================================

echo "$HOME/lobster/scripts/self-check-reminder.sh" | at now + 3 minutes 2>/dev/null
