#!/bin/bash
#===============================================================================
# Shared dispatcher-exclusion SQL fragment (Linear BIS-723)
#
# Single source of truth for the shell side of the dispatcher-exclusion check
# against agent_sessions.db. Before this file existed, the fragment below was
# copy-pasted directly into periodic-self-check.sh's sqlite3 query, alongside
# four independent Python reimplementations of the same check
# (session_store.py x2, inbox_server.py x2) — the whole family of duplicates
# had to be fixed four separate times (issue #781, PR #2099, PR #2103).
#
# Cross-reference: these constants MUST match DISPATCHER_AGENT_TYPE /
# DISPATCHER_AGENT_ID / DISPATCHER_EXCLUSION_SQL in src/utils/agent_types.py.
# Bash and Python cannot literally share one function, so if you change either
# excluded value here, change it in src/utils/agent_types.py too (and vice
# versa).
#
# Usage:
#   source "$(dirname "$0")/lib/agent_sessions.sh"
#   sqlite3 "$DB_PATH" \
#     "SELECT COUNT(*) FROM agent_sessions WHERE status IN ('running','starting') AND $DISPATCHER_EXCLUSION_SQL"
#
# ## Belt-and-suspenders `id` fallback (issue #2226)
#
# `agent_type` is an optional parameter on the `session_start` MCP tool. A
# dispatcher bootup call that omits it leaves the row's `agent_type` column
# NULL, which COALESCE turns into '' — and '' != 'dispatcher' is TRUE, so the
# row was NOT excluded, producing a permanent false-positive "[1 agents
# pending]" self-check every 3 minutes with zero real subagents running
# (issue #2226). The `id != '...'` clause below excludes the dispatcher's row
# by its static agent_id (always 'lobster-dispatcher', per
# .claude/sys.dispatcher.bootup.md's session_start bootup instructions) as a
# second, independent signal — mirroring the belt-and-suspenders pattern
# scripts/agent-monitor.py's `_is_dispatcher_agent()` already used.
#===============================================================================

# The agent_type value written when the dispatcher registers its own session.
# Must match DISPATCHER_AGENT_TYPE in src/utils/agent_types.py.
DISPATCHER_AGENT_TYPE="dispatcher"

# The static agent_id the dispatcher always registers itself under. Must
# match DISPATCHER_AGENT_ID in src/utils/agent_types.py.
DISPATCHER_AGENT_ID="lobster-dispatcher"

# SQL WHERE-clause fragment excluding dispatcher rows from a query over
# agent_sessions. COALESCE guards against agent_type being NULL (legacy rows
# predating the column, or subagents that never set it). The `id !=` clause
# is the belt-and-suspenders fallback (issue #2226): it excludes the
# dispatcher's row by its static agent_id even when agent_type itself is NULL.
# Must match DISPATCHER_EXCLUSION_SQL in src/utils/agent_types.py.
DISPATCHER_EXCLUSION_SQL="(COALESCE(agent_type, '') != '${DISPATCHER_AGENT_TYPE}' AND id != '${DISPATCHER_AGENT_ID}')"
