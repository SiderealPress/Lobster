#!/bin/bash
#===============================================================================
# DEPRECATED — This entrypoint is no longer used.
#
# The staging container now runs systemd as PID 1 (CMD ["/lib/systemd/systemd"]).
# Services are managed by systemd:
#   - lobster-router.service     (Telegram bot)
#   - lobster-mcp-local.service  (MCP HTTP server)
#   - lobster-claude.service     (Claude Code persistent session)
#
# Runtime setup (config.env, install --container-setup, MCP registration) is
# handled by lobster-container-init.service, which runs before the above services.
#
# This file is kept for reference only. It is not used by the Dockerfile.
#===============================================================================
echo "ERROR: This entrypoint is deprecated. The container uses systemd as PID 1." >&2
exit 1
