#!/bin/bash
# Wrapper for systemd: runs SSH pre-check then dispatches lobstertalk-incoming-handler
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$SCRIPT_DIR/lobstertalk-incoming-check-dispatch.sh" lobstertalk-incoming-handler
