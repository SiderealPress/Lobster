#!/bin/bash
#===============================================================================
# Docker Worker Launcher
#
# Launches a detached Docker container running claude -p for substantial tasks.
# Results are written directly to the host inbox via bind mount.
#
# Usage: docker-worker.sh <job_name> <chat_id> <source> <max_turns> "<prompt>"
#
# Example:
#   docker-worker.sh "review-auth" 12345 "telegram" 15 "Review the auth system..."
#===============================================================================

set -o pipefail

#===============================================================================
# Parse arguments
#===============================================================================
if [[ $# -lt 5 ]]; then
    echo "Usage: $0 <job_name> <chat_id> <source> <max_turns> <prompt>" >&2
    exit 1
fi

JOB_NAME="$1"
CHAT_ID="$2"
SOURCE="$3"
MAX_TURNS="$4"
PROMPT="$5"

#===============================================================================
# Source config for API key
#===============================================================================
# Deliberately NOT config.env: config.env is loaded as an EnvironmentFile by
# nearly every Lobster systemd unit, including lobster-claude.service (the
# dispatcher itself). A key placed there leaks into the dispatcher's own auth
# environment -- this happened once already with a shared ANTHROPIC_API_KEY
# (disabled 2026-08-05 after it was found silently overriding the dispatcher's
# setup-token OAuth credential). docker-worker.sh gets its own dedicated,
# never-EnvironmentFile'd config file instead. See config/docker-worker.env.example.
CONFIG_ENV="${LOBSTER_CONFIG_DIR:-$HOME/lobster-config}/docker-worker.env"

if [[ ! -f "$CONFIG_ENV" ]]; then
    echo "ERROR: Config file not found: $CONFIG_ENV" >&2
    echo "  Copy config/docker-worker.env.example to $CONFIG_ENV and fill in a" >&2
    echo "  dedicated ANTHROPIC_API_KEY (do not reuse the dispatcher's shared key)." >&2
    exit 1
fi

ANTHROPIC_API_KEY=$(grep '^ANTHROPIC_API_KEY=' "$CONFIG_ENV" | cut -d'=' -f2-)

if [[ -z "$ANTHROPIC_API_KEY" ]]; then
    echo "ERROR: ANTHROPIC_API_KEY not set in $CONFIG_ENV" >&2
    echo "  Provision a dedicated key at https://console.anthropic.com/settings/keys" >&2
    echo "  and set it in $CONFIG_ENV -- see config/docker-worker.env.example." >&2
    exit 1
fi

#===============================================================================
# Lazy-build image if not present
#===============================================================================
IMAGE_NAME="lobster-worker:latest"

if ! sudo docker image inspect "$IMAGE_NAME" > /dev/null 2>&1; then
    echo "Building Docker image $IMAGE_NAME..."
    LOBSTER_DIR="${LOBSTER_INSTALL_DIR:-$HOME/lobster}"
    sudo docker build -t "$IMAGE_NAME" -f "$LOBSTER_DIR/docker/Dockerfile.worker" "$LOBSTER_DIR/"
    if [[ $? -ne 0 ]]; then
        echo "ERROR: Docker build failed" >&2
        exit 1
    fi
fi

#===============================================================================
# Launch detached container
#===============================================================================
CONTAINER_NAME="lobster-worker-${JOB_NAME}-$(date +%s)"

# Prevent CLAUDECODE from leaking into Docker container via explicit -e flags
# or docker's default environment passthrough.
unset CLAUDECODE CLAUDE_CODE_ENTRYPOINT 2>/dev/null || true

container_id=$(sudo docker run -d --rm \
    --name "$CONTAINER_NAME" \
    --memory=2g --cpus=2 \
    --network=host \
    -e ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" \
    -e WORKER_JOB_NAME="$JOB_NAME" \
    -e WORKER_CHAT_ID="$CHAT_ID" \
    -e WORKER_SOURCE="$SOURCE" \
    -e WORKER_MAX_TURNS="$MAX_TURNS" \
    -e WORKER_PROMPT="$PROMPT" \
    -v "${LOBSTER_MESSAGES:-$HOME/messages}/inbox:/home/worker/messages/inbox" \
    "$IMAGE_NAME" 2>&1)

rc=$?
if [[ $rc -ne 0 ]]; then
    echo "ERROR: Docker run failed: $container_id" >&2
    exit 1
fi

echo "Worker launched: container=$CONTAINER_NAME id=${container_id:0:12}"
