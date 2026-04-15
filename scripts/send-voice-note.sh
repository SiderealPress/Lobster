#!/usr/bin/env bash
# send-voice-note.sh — Send a TTS voice note to a Telegram chat
#
# Usage:
#   scripts/send-voice-note.sh <chat_id> "<text>"
#
# Examples:
#   scripts/send-voice-note.sh ADMIN_CHAT_ID_REDACTED "Good morning! Here is your briefing."
#   scripts/send-voice-note.sh ADMIN_CHAT_ID_REDACTED "$(cat /tmp/message.txt)"
#
# Requires:
#   - piper binary at /usr/local/bin/piper or ~/.local/bin/piper
#   - en_US-lessac-medium.onnx model in ~/lobster-workspace/piper-models/
#   - ffmpeg available in PATH
#   - TELEGRAM_BOT_TOKEN environment variable (or sourced from config)
#   - Lobster installed at ~/lobster/
#
# The script generates an OGG/Opus voice file using piper + ffmpeg, then sends
# it via the Telegram Bot API using curl. It does not rely on the running
# Lobster dispatcher — useful for scripted/cron usage.
#
# Exit codes:
#   0 — voice note sent successfully
#   1 — argument error (missing chat_id or text)
#   2 — TTS synthesis failed (piper or ffmpeg error)
#   3 — Telegram API send failed
#   4 — configuration error (missing token or binary)

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

INSTALL_DIR="${LOBSTER_INSTALL_DIR:-$HOME/lobster}"
WORKSPACE_DIR="${LOBSTER_WORKSPACE:-$HOME/lobster-workspace}"
CONFIG_DIR="${LOBSTER_CONFIG_DIR:-$HOME/lobster-user-config}"

PIPER_BIN="${PIPER_BIN:-}"
if [ -z "$PIPER_BIN" ]; then
    if [ -x "/usr/local/bin/piper" ]; then
        PIPER_BIN="/usr/local/bin/piper"
    elif [ -x "$HOME/.local/bin/piper" ]; then
        PIPER_BIN="$HOME/.local/bin/piper"
    elif command -v piper &>/dev/null; then
        PIPER_BIN="$(command -v piper)"
    fi
fi

PIPER_MODELS_DIR="${PIPER_MODELS_DIR:-$WORKSPACE_DIR/piper-models}"
PIPER_MODEL="${PIPER_MODEL:-$PIPER_MODELS_DIR/en_US-lessac-medium.onnx}"
TELEGRAM_API_URL="https://api.telegram.org"

# Load TELEGRAM_BOT_TOKEN from config if not set
if [ -z "${TELEGRAM_BOT_TOKEN:-}" ]; then
    config_file="${LOBSTER_CONFIG_FILE:-$HOME/lobster-user-config/config.env}"
    if [ -f "$config_file" ]; then
        # shellcheck disable=SC1090
        source "$config_file" 2>/dev/null || true
    fi
fi

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

if [ $# -lt 2 ]; then
    echo "Usage: $0 <chat_id> \"<text>\"" >&2
    echo "Example: $0 ADMIN_CHAT_ID_REDACTED \"Good morning! Here is your briefing.\"" >&2
    exit 1
fi

CHAT_ID="$1"
TEXT="$2"

if [ -z "$CHAT_ID" ]; then
    echo "Error: chat_id cannot be empty" >&2
    exit 1
fi

if [ -z "$TEXT" ]; then
    echo "Error: text cannot be empty" >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Pre-flight checks
# ---------------------------------------------------------------------------

if [ -z "${TELEGRAM_BOT_TOKEN:-}" ]; then
    echo "Error: TELEGRAM_BOT_TOKEN is not set. Source your config or export the variable." >&2
    exit 4
fi

if [ -z "$PIPER_BIN" ]; then
    echo "Error: piper binary not found. Install via install.sh or set PIPER_BIN." >&2
    exit 4
fi

if [ ! -f "$PIPER_MODEL" ]; then
    echo "Error: piper model not found at $PIPER_MODEL" >&2
    echo "Install via install.sh or set PIPER_MODEL=/path/to/model.onnx" >&2
    exit 4
fi

if ! command -v ffmpeg &>/dev/null; then
    echo "Error: ffmpeg not found in PATH. Install ffmpeg." >&2
    exit 4
fi

# ---------------------------------------------------------------------------
# TTS synthesis
# ---------------------------------------------------------------------------

TMP_DIR="$(mktemp -d --tmpdir lobster-tts-XXXXXX)"
WAV_FILE="$TMP_DIR/voice.wav"
OGG_FILE="$TMP_DIR/voice.ogg"

cleanup() {
    rm -rf "$TMP_DIR"
}
trap cleanup EXIT

echo "Synthesizing voice note ($(echo -n "$TEXT" | wc -c) chars)..."

# Run piper: text → WAV
if ! echo "$TEXT" | "$PIPER_BIN" \
        --model "$PIPER_MODEL" \
        --output_file "$WAV_FILE" 2>&1; then
    echo "Error: piper synthesis failed" >&2
    exit 2
fi

if [ ! -s "$WAV_FILE" ]; then
    echo "Error: piper produced an empty WAV file" >&2
    exit 2
fi

# Convert WAV → OGG/Opus (Telegram voice format)
if ! ffmpeg -y -i "$WAV_FILE" \
        -c:a libopus -b:a 24k -ar 24000 -ac 1 \
        -vbr on -compression_level 10 \
        "$OGG_FILE" 2>&1 | tail -3; then
    echo "Error: ffmpeg OGG conversion failed" >&2
    exit 2
fi

if [ ! -s "$OGG_FILE" ]; then
    echo "Error: ffmpeg produced an empty OGG file" >&2
    exit 2
fi

OGG_SIZE="$(wc -c < "$OGG_FILE")"
echo "Voice note generated: ${OGG_SIZE} bytes"

# ---------------------------------------------------------------------------
# Send via Telegram Bot API
# ---------------------------------------------------------------------------

echo "Sending voice note to chat ${CHAT_ID}..."

RESPONSE="$(curl -s -X POST \
    "${TELEGRAM_API_URL}/bot${TELEGRAM_BOT_TOKEN}/sendVoice" \
    -F "chat_id=${CHAT_ID}" \
    -F "voice=@${OGG_FILE};type=audio/ogg")"

if echo "$RESPONSE" | python3 -c "import sys, json; d=json.load(sys.stdin); sys.exit(0 if d.get('ok') else 1)" 2>/dev/null; then
    echo "Voice note sent successfully to chat ${CHAT_ID}."
    exit 0
else
    echo "Error: Telegram API returned an error:" >&2
    echo "$RESPONSE" >&2
    exit 3
fi
