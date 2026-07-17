#!/bin/bash
#===============================================================================
# Shared jq-safe JSON object builder (Linear BIS-724)
#
# Canonical single implementation of _json_build_message(): builds a flat
# JSON object from a list of jq argument flags, always routing values through
# jq --arg/--argjson so quotes, newlines, backslashes, and unicode in the
# values are escaped into valid JSON. Before this file existed, this exact
# `jq -n --arg ... '{...}'` shape was reimplemented independently in 4
# scripts (periodic-self-check.sh, alert.sh, check-agent-outputs.sh,
# daily-update-check.sh x2) — issue #2004 / PR #2016 / PR #2031 fixed a
# broken-escaping bug at each site separately before this consolidation.
#
# NOTE: This is the single source of truth for jq-arg-safe JSON message
# construction. Do NOT reimplement the `jq -n --arg ... '{...}'` pattern
# inline in a new script; source this file and call _json_build_message
# instead.
#
# The 4 call sites build different message shapes (different field sets:
# inbox messages, outbox messages, with/without a "type" field, etc.), so
# this helper does not hardcode a fixed schema. Instead it is a thin,
# documented wrapper around jq's `$ARGS.named` object — every --arg/--argjson
# flag you pass becomes one key in the resulting flat JSON object, in
# whatever shape the caller needs.
#
# Usage:
#   source "$(dirname "$0")/lib/json_message.sh"
#   _json_build_message \
#       --arg id "$MSG_ID" \
#       --arg source "system" \
#       --argjson chat_id 0 \
#       --arg text "$TEXT" \
#       --arg timestamp "$TIMESTAMP" \
#     > "$OUTPUT_FILE"
#
# Use --arg for string values (the common case — always safely escaped).
# Use --argjson for values that are already valid JSON literals (numbers,
# booleans, 0/1 chat_id placeholders, etc.) — never interpolate those
# directly into a filter string.
#===============================================================================

# _json_build_message [--arg NAME VALUE | --argjson NAME VALUE] ...
#
# Prints a flat JSON object to stdout with one key per NAME passed, using
# jq's $ARGS.named so the caller never has to hand-write an object literal
# (and therefore can never forget to route a value through --arg/--argjson).
_json_build_message() {
    jq -n "$@" '$ARGS.named'
}
