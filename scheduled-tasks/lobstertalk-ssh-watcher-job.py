#!/usr/bin/env python3
"""LobsterTalk SSH Watcher Job — detects new messages written to shared server's messages.jsonl"""
import json
import os
import uuid
import time
import subprocess
from datetime import datetime, timezone
from pathlib import Path

RUN_ID = str(uuid.uuid4())[:8]
HOME = Path.home()
STATE_FILE = HOME / "lobster-workspace" / "data" / "lobstertalk-ssh-state.json"
LOG_FILE = HOME / "lobster-workspace" / "logs" / "lobstertalk.jsonl"
INBOX_DIR = HOME / "messages" / "inbox"
SSH_KEY = HOME / ".ssh" / "lobsterbotsownkey"
SSH_HOST = os.environ.get("BOT_TALK_SSH_HOST", "")
REMOTE_FILE = os.environ.get("BOT_TALK_SSH_REMOTE_FILE", "~/bot-talk/messages.jsonl")
OWNER_CHAT_ID = 8305714125
JOB_NAME = "lobstertalk-ssh-watcher"

# Self-echo filter: skip messages sent by this Lobster (re-delivered by relay).
# Mirrors the BOT_TALK_SELF_USER filter on the HTTP path (issue #1345).
MY_LOBSTER_NAME: str = (
    os.environ.get("BOT_TALK_SELF_USER")
    or os.environ.get("BOT_TALK_SENDER")
    or os.environ.get("LOBSTER_NAME")
    or "SaharLobster"
)


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"last_mtime": None, "last_size": 0, "last_offset": 0}


def save_state(state):
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.rename(STATE_FILE)


def append_log(entry):
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    if LOG_FILE.exists() and LOG_FILE.stat().st_size > 50 * 1024 * 1024:
        LOG_FILE.rename(LOG_FILE.with_suffix(".jsonl.bak"))
    with LOG_FILE.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def ssh_run(cmd, timeout=30):
    """Run a command on the remote host via SSH."""
    result = subprocess.run(
        ["ssh", "-i", str(SSH_KEY), "-o", "StrictHostKeyChecking=no",
         "-o", "ConnectTimeout=15", SSH_HOST, cmd],
        capture_output=True, text=True, timeout=timeout
    )
    return result.returncode, result.stdout, result.stderr


def write_to_inbox(messages):
    """Write new bot-talk messages to the Lobster inbox."""
    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    routed = 0
    skipped_self = 0
    for msg in messages:
        sender = msg.get("sender", msg.get("from", "unknown"))
        content = msg.get("content", msg.get("text", ""))
        ts = msg.get("timestamp", datetime.now(timezone.utc).isoformat())

        # Self-echo filter: skip messages we sent (fix for issue #1791).
        if sender == MY_LOBSTER_NAME:
            skipped_self += 1
            continue

        msg_id = f"{int(time.time() * 1000)}_bot_talk_{str(uuid.uuid4())[:8]}"
        inbox_msg = {
            "id": msg_id,
            "type": "text",
            "source": "bot-talk",
            "chat_id": OWNER_CHAT_ID,
            "user_name": sender,
            "text": content,
            "timestamp": ts,
            "direction": "INBOUND",
            "from": sender,
            "to": "SaharLobster",
        }
        tmp_path = INBOX_DIR / f"{msg_id}.tmp"
        final_path = INBOX_DIR / f"{msg_id}.json"
        tmp_path.write_text(json.dumps(inbox_msg))
        tmp_path.rename(final_path)
        append_log({"ts": ts, "direction": "INBOUND", "sender": sender, "content": content,
                    "job_run": RUN_ID, "source": "ssh-watcher"})
        routed += 1
    if skipped_self:
        append_log({"ts": datetime.now(timezone.utc).isoformat(), "direction": "INFO",
                    "content": f"Skipped {skipped_self} self-echo message(s).",
                    "job_run": RUN_ID, "source": "ssh-watcher"})
    return routed


def write_task_output(output, status="success"):
    """Write job output via lobster MCP CLI shim."""
    shim = HOME / "lobster" / "scheduled-tasks" / "write-task-output.sh"
    if shim.exists():
        subprocess.run([str(shim), JOB_NAME, output, status],
                       capture_output=True, timeout=15)
    else:
        print(f"[{JOB_NAME}] {status}: {output}")


def main():
    if not SSH_HOST:
        write_task_output("BOT_TALK_SSH_HOST env var not set — cannot connect to bot-talk server", "failed")
        return

    is_first_run = not STATE_FILE.exists()
    state = load_state()
    last_offset = state.get("last_offset", 0)
    last_mtime = state.get("last_mtime")
    last_size = state.get("last_size", 0)
    debug_mode = os.environ.get("LOBSTER_DEBUG", "").lower() == "true"

    # Step 1: Get current file stat
    rc, stat_out, stat_err = ssh_run(f"stat -c '%s %Y' {REMOTE_FILE} 2>/dev/null || echo 'MISSING'")
    if rc != 0 or stat_out.strip() == "MISSING" or not stat_out.strip():
        msg = f"SSH stat failed: rc={rc} err={stat_err.strip()[:200]}"
        append_log({"ts": datetime.now(timezone.utc).isoformat(), "direction": "ERROR",
                    "content": msg, "job_run": RUN_ID})
        write_task_output(f"SSH failed: {stat_err.strip()[:200]}", "failed")
        return

    parts = stat_out.strip().split()
    if len(parts) < 2:
        write_task_output(f"Unexpected stat output: {stat_out.strip()[:100]}", "failed")
        return

    current_size = int(parts[0])
    current_mtime = parts[1]

    # First run: calibrate offset to end of file, don't route historical messages
    if is_first_run:
        state["last_mtime"] = current_mtime
        state["last_size"] = current_size
        state["last_offset"] = current_size
        save_state(state)
        write_task_output(f"First run: calibrated to current end of file ({current_size} bytes). Future runs will only see new messages.", "success")
        return

    # Step 2: Check if anything changed
    if current_mtime == last_mtime and current_size == last_size:
        # Silent — nothing changed
        write_task_output("No change detected.", "success")
        return

    if current_size <= last_offset:
        # File was truncated/rotated — reset offset
        last_offset = 0
        state["last_offset"] = 0

    if current_size == last_offset:
        # No new bytes despite mtime change
        state["last_mtime"] = current_mtime
        state["last_size"] = current_size
        save_state(state)
        write_task_output("File touched but no new bytes.", "success")
        return

    # Step 3: Read new bytes from offset
    bytes_to_read = current_size - last_offset
    rc, new_content, read_err = ssh_run(
        f"tail -c +{last_offset + 1} {REMOTE_FILE} 2>/dev/null | head -c {bytes_to_read}"
    )
    if rc != 0:
        write_task_output(f"SSH read failed: {read_err.strip()[:200]}", "failed")
        return

    # Step 4: Parse new JSONL lines
    new_messages = []
    actual_bytes_read = len(new_content.encode("utf-8"))
    for line in new_content.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
            new_messages.append(msg)
        except json.JSONDecodeError:
            pass  # Skip malformed lines

    if not new_messages:
        # Update state even if no parseable messages
        state["last_mtime"] = current_mtime
        state["last_size"] = current_size
        state["last_offset"] = last_offset + actual_bytes_read
        save_state(state)
        write_task_output("File changed but no parseable messages.", "success")
        return

    # Step 5: Route to inbox
    routed = write_to_inbox(new_messages)

    # Step 6: Re-stat after reading so last_size reflects actual post-read size
    # (fixes issue #1780 Bug 1: race condition when messages arrive during read).
    rc_post, stat_post_out, _ = ssh_run(f"stat -c '%s %Y' {REMOTE_FILE} 2>/dev/null")
    if rc_post == 0 and stat_post_out.strip():
        post_parts = stat_post_out.strip().split()
        if len(post_parts) >= 2:
            current_size = int(post_parts[0])
            current_mtime = post_parts[1]

    # Step 7: Update state
    state["last_mtime"] = current_mtime
    state["last_size"] = current_size
    state["last_offset"] = last_offset + actual_bytes_read
    save_state(state)

    summary = f"Found {len(new_messages)} new message(s), routed {routed} to inbox."
    append_log({"ts": datetime.now(timezone.utc).isoformat(), "direction": "INFO",
                "content": summary, "job_run": RUN_ID})

    if debug_mode:
        # Send debug notification
        for m in new_messages[:3]:
            sender = m.get("sender", m.get("from", "unknown"))
            content = m.get("content", m.get("text", ""))
            print(f"[SSH-WATCHER DEBUG] {sender}: {content[:200]}")

    write_task_output(summary, "success")


if __name__ == "__main__":
    main()
