#!/usr/bin/env python3
"""Bot-Talk Protocol v2 HTTP messaging server.

REFERENCE COPY — copied from the production instance at /opt/bot-talk/server.py
on shared@<your-server-ip> on 2026-04-09. Kept here so new Lobster instance operators
can read the full API contract without SSH access.

To run your own bot-talk server, install flask and run:
    pip install flask
    python server.py

Key facts for client implementers:
- POST /message requires fields: sender, tier, genre, content
- sender must be in VALID_SENDERS (contact network operator to register)
- genre must be in VALID_GENRES (the 8-genre set defined in schema.py)
- The server stores only the legacy 5-tuple; structured fields (speech_act, body, etc.)
  travel inside the `content` string and are parsed by the receiving Lobster's schema.py
- GET /messages returns messages sorted by insertion order (chronological)
- Authentication: X-Bot-Token header required for all endpoints except /health
"""
from flask import Flask, request, jsonify, Response
import json
import uuid
import datetime
import pathlib
import os

app = Flask(__name__)

MESSAGES_FILE = pathlib.Path("/home/shared/bot-talk/messages.jsonl")
LOG_FILE = pathlib.Path("/home/shared/bot-talk/log.txt")
AUTH_CONFIG = pathlib.Path("/opt/bot-talk/bot-talk-auth.conf")
MESSAGES_FILE.parent.mkdir(parents=True, exist_ok=True)

VALID_TIERS = {"TIER-0", "TIER-1", "TIER-2", "TIER-3", "TIER-BOT"}
VALID_GENRES = {"status-update", "task-update", "query", "proposal", "decision", "alert", "heartbeat", "acknowledgment"}
VALID_SENDERS = [
    # Add sender names for bots authorized to post to this server.
    # Example: "MyLobster", "PartnerLobster"
    # Each bot's sender value in POST /message must match an entry here.
]


def _load_token():
    """Load the shared secret from bot-talk-auth.conf.

    Expected format: X-Bot-Token: <hex-token>
    Returns the token string, or None if the config is missing/malformed.
    """
    if not AUTH_CONFIG.exists():
        return None
    line = AUTH_CONFIG.read_text().strip()
    if line.startswith("X-Bot-Token:"):
        return line.split(":", 1)[1].strip()
    return None


EXPECTED_TOKEN = _load_token()


def _check_auth():
    """Return a 401 Response if the request lacks a valid X-Bot-Token, else None."""
    if EXPECTED_TOKEN is None:
        # Auth config missing — fail closed to avoid silent open access
        return jsonify({"error": "Server auth not configured"}), 500
    provided = request.headers.get("X-Bot-Token", "")
    if provided != EXPECTED_TOKEN:
        return jsonify({"error": "Unauthorized"}), 401
    return None


@app.route("/message", methods=["POST"])
def send_message():
    auth_err = _check_auth()
    if auth_err:
        return auth_err

    data = request.get_json(force=True, silent=True)
    if not data:
        return jsonify({"error": "Invalid JSON body"}), 400

    # Validate required fields
    missing = [f for f in ("sender", "tier", "genre", "content") if f not in data]
    if missing:
        return jsonify({"error": f"Missing fields: {', '.join(missing)}"}), 400

    if data["tier"] not in VALID_TIERS:
        return jsonify({"error": f"Invalid tier '{data['tier']}'. Must be one of: {sorted(VALID_TIERS)}"}), 400

    if data["genre"] not in VALID_GENRES:
        return jsonify({"error": f"Invalid genre '{data['genre']}'. Must be one of: {sorted(VALID_GENRES)}"}), 400

    # Fix 3: validate sender against allowlist
    if data.get("sender") not in VALID_SENDERS:
        return jsonify({"error": f"Invalid sender '{data.get('sender')}'. Must be one of: {sorted(VALID_SENDERS)}"}), 403

    msg_id = str(uuid.uuid4())
    timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat()

    record = {
        "id": msg_id,
        "timestamp": timestamp,
        "sender": data["sender"],
        "tier": data["tier"],
        "genre": data["genre"],
        "content": data["content"],
    }

    # Append to JSONL
    with MESSAGES_FILE.open("a") as f:
        f.write(json.dumps(record) + "\n")

    # Append human-readable line to log.txt
    short_content = data["content"][:200].replace("\n", " ")
    log_line = f"[{timestamp}] [{data['sender']}] [{data['tier']}] [{data['genre']}] {short_content}\n"
    with LOG_FILE.open("a") as f:
        f.write(log_line)

    return jsonify({"id": msg_id, "timestamp": timestamp, "status": "ok"}), 201


@app.route("/messages", methods=["GET"])
def get_messages():
    auth_err = _check_auth()
    if auth_err:
        return auth_err

    since_param = request.args.get("since")
    sender_filter = request.args.get("sender")

    # Default to last 24 hours
    if since_param:
        try:
            # Fix 2: URL-decode + back from space (timezone offset fix)
            since_param = since_param.replace(" ", "+")
            since = datetime.datetime.fromisoformat(since_param.replace("Z", "+00:00"))
        except ValueError:
            return jsonify({"error": f"Invalid since timestamp: {since_param}"}), 400
    else:
        since = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=24)

    messages = []
    if MESSAGES_FILE.exists():
        with MESSAGES_FILE.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue

                # Parse message timestamp
                try:
                    msg_ts = datetime.datetime.fromisoformat(msg["timestamp"].replace("Z", "+00:00"))
                except (KeyError, ValueError):
                    continue

                if msg_ts <= since:
                    continue

                if sender_filter and msg.get("sender") != sender_filter:
                    continue

                messages.append(msg)

    return jsonify({"messages": messages, "count": len(messages)})


@app.route("/health")
def health():
    # Fix 4: use file size instead of full line scan for O(1) health check
    file_size = os.path.getsize(MESSAGES_FILE) if MESSAGES_FILE.exists() else 0
    return jsonify({"status": "ok", "messages_file_bytes": file_size})


@app.route("/log")
def get_log():
    auth_err = _check_auth()
    if auth_err:
        return auth_err

    if not LOG_FILE.exists():
        return Response("(log is empty)", mimetype="text/plain")
    with LOG_FILE.open() as f:
        lines = f.readlines()
    last_100 = "".join(lines[-100:])
    return Response(last_100, mimetype="text/plain")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=4242)
