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
# Cross-reference: this constant MUST match DISPATCHER_AGENT_TYPE /
# DISPATCHER_EXCLUSION_SQL in src/utils/agent_types.py. Bash and Python cannot
# literally share one function, so if you change the excluded agent_type
# value here, change it in src/utils/agent_types.py too (and vice versa).
#
# Usage:
#   source "$(dirname "$0")/lib/agent_sessions.sh"
#   sqlite3 "$DB_PATH" \
#     "SELECT COUNT(*) FROM agent_sessions WHERE status IN ('running','starting') AND $DISPATCHER_EXCLUSION_SQL"
#===============================================================================

# The agent_type value written when the dispatcher registers its own session.
# Must match DISPATCHER_AGENT_TYPE in src/utils/agent_types.py.
DISPATCHER_AGENT_TYPE="dispatcher"

# SQL WHERE-clause fragment excluding dispatcher rows from a query over
# agent_sessions. COALESCE guards against agent_type being NULL (legacy rows
# predating the column, or subagents that never set it).
# Must match DISPATCHER_EXCLUSION_SQL in src/utils/agent_types.py.
DISPATCHER_EXCLUSION_SQL="COALESCE(agent_type, '') != '${DISPATCHER_AGENT_TYPE}'"
