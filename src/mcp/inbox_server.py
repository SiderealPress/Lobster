#!/usr/bin/env python3
"""
Lobster Inbox MCP Server

Provides tools for Claude Code to interact with the message queue:
- check_inbox: Get new messages from all sources
- send_reply: Send a reply back to the original source
- list_sources: List available message sources
- get_message: Get a specific message by ID
- mark_processed: Mark a message as processed
"""

import asyncio
import json
import os
import sys
import time
import threading
import httpx
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# MCP SDK
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

# Directories
BASE_DIR = Path.home() / "messages"
INBOX_DIR = BASE_DIR / "inbox"
OUTBOX_DIR = BASE_DIR / "outbox"
PROCESSED_DIR = BASE_DIR / "processed"
CONFIG_DIR = BASE_DIR / "config"
AUDIO_DIR = BASE_DIR / "audio"
SENT_DIR = BASE_DIR / "sent"
TASKS_FILE = BASE_DIR / "tasks.json"
TASK_OUTPUTS_DIR = BASE_DIR / "task-outputs"

# Heartbeat file for health monitoring
HEARTBEAT_FILE = Path.home() / "lobster-workspace" / "logs" / "claude-heartbeat"

# Scheduled Tasks Directories
SCHEDULED_TASKS_DIR = Path.home() / "lobster" / "scheduled-tasks"
SCHEDULED_JOBS_FILE = SCHEDULED_TASKS_DIR / "jobs.json"
SCHEDULED_TASKS_TASKS_DIR = SCHEDULED_TASKS_DIR / "tasks"
SCHEDULED_TASKS_LOGS_DIR = SCHEDULED_TASKS_DIR / "logs"

# Ensure directories exist
for d in [INBOX_DIR, OUTBOX_DIR, PROCESSED_DIR, SENT_DIR, CONFIG_DIR, AUDIO_DIR, TASK_OUTPUTS_DIR,
          SCHEDULED_TASKS_DIR, SCHEDULED_TASKS_TASKS_DIR, SCHEDULED_TASKS_LOGS_DIR]:
    d.mkdir(parents=True, exist_ok=True)

# OpenAI configuration for Whisper transcription
# Try environment first, then fall back to config file
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
if not OPENAI_API_KEY:
    config_file = Path.home() / "lobster" / "config" / "config.env"
    if config_file.exists():
        for line in config_file.read_text().splitlines():
            if line.strip().startswith("OPENAI_API_KEY="):
                OPENAI_API_KEY = line.split("=", 1)[1].strip().strip('"').strip("'")
                break

# Initialize tasks file if needed
if not TASKS_FILE.exists():
    TASKS_FILE.write_text(json.dumps({"tasks": [], "next_id": 1}, indent=2))

# Initialize scheduled jobs file if needed
if not SCHEDULED_JOBS_FILE.exists():
    SCHEDULED_JOBS_FILE.write_text(json.dumps({"jobs": {}}, indent=2))

# Source configurations
SOURCES = {
    "telegram": {
        "name": "Telegram",
        "enabled": True,
    },
    "slack": {
        "name": "Slack",
        "enabled": True,
    },
}

server = Server("lobster-inbox")


def touch_heartbeat():
    """Touch heartbeat file to signal Claude is alive and processing."""
    try:
        HEARTBEAT_FILE.parent.mkdir(parents=True, exist_ok=True)
        HEARTBEAT_FILE.touch()
    except Exception:
        pass  # Don't fail on heartbeat errors


@server.list_tools()
async def list_tools() -> list[Tool]:
    """List available tools."""
    return [
        Tool(
            name="wait_for_messages",
            description="Block and wait for new messages to arrive. This is the core tool for the always-on loop. Returns immediately if messages exist, otherwise waits until a message arrives or timeout. Use this in your main loop: wait_for_messages -> process -> repeat.",
            inputSchema={
                "type": "object",
                "properties": {
                    "timeout": {
                        "type": "integer",
                        "description": "Maximum seconds to wait. Default 300 (5 minutes). After timeout, returns with a prompt to call again.",
                        "default": 300,
                    },
                },
            },
        ),
        Tool(
            name="check_inbox",
            description="Check for new messages in the inbox from all sources (Telegram, SMS, Signal, etc.). Returns unprocessed messages. For the always-on loop, prefer wait_for_messages which blocks until messages arrive.",
            inputSchema={
                "type": "object",
                "properties": {
                    "source": {
                        "type": "string",
                        "description": "Filter by source (telegram, sms, signal). Leave empty for all sources.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of messages to return. Default 10.",
                        "default": 10,
                    },
                },
            },
        ),
        Tool(
            name="send_reply",
            description="Send a reply to a message. The reply will be routed back to the original source (Telegram, Slack, SMS, etc.). Supports optional inline keyboard buttons for Telegram and thread replies for Slack.",
            inputSchema={
                "type": "object",
                "properties": {
                    "chat_id": {
                        "oneOf": [
                            {"type": "integer"},
                            {"type": "string"}
                        ],
                        "description": "The chat/channel ID to reply to (from the original message). Integer for Telegram, string for Slack.",
                    },
                    "text": {
                        "type": "string",
                        "description": "The reply text to send.",
                    },
                    "source": {
                        "type": "string",
                        "description": "The source to reply via (telegram, slack, sms, signal). Default: telegram.",
                        "default": "telegram",
                    },
                    "thread_ts": {
                        "type": "string",
                        "description": "Slack thread timestamp. If provided, reply will be sent as a thread reply. Get this from the original message's thread_ts or slack_ts field.",
                    },
                    "buttons": {
                        "type": "array",
                        "description": "Optional inline keyboard buttons (Telegram only). Format: [[\"Btn1\", \"Btn2\"], [\"Btn3\"]] for simple buttons (text=callback_data), or [[{\"text\": \"Label\", \"callback_data\": \"value\"}]] for explicit callback data.",
                        "items": {
                            "type": "array",
                            "description": "A row of buttons",
                            "items": {
                                "oneOf": [
                                    {"type": "string", "description": "Simple button (text is also callback_data)"},
                                    {
                                        "type": "object",
                                        "properties": {
                                            "text": {"type": "string", "description": "Button label"},
                                            "callback_data": {"type": "string", "description": "Data sent when pressed"}
                                        },
                                        "required": ["text"]
                                    }
                                ]
                            }
                        }
                    },
                },
                "required": ["chat_id", "text"],
            },
        ),
        Tool(
            name="mark_processed",
            description="Mark a message as processed and move it out of the inbox.",
            inputSchema={
                "type": "object",
                "properties": {
                    "message_id": {
                        "type": "string",
                        "description": "The message ID to mark as processed.",
                    },
                },
                "required": ["message_id"],
            },
        ),
        Tool(
            name="list_sources",
            description="List all available message sources and their status.",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="get_stats",
            description="Get inbox statistics: message counts, sources, etc.",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        # Conversation History Tool
        Tool(
            name="get_conversation_history",
            description="Retrieve past messages from conversation history - both received messages and sent replies. Supports pagination, filtering by chat_id, and text search. Use this to scroll back through previous conversations.",
            inputSchema={
                "type": "object",
                "properties": {
                    "chat_id": {
                        "oneOf": [
                            {"type": "integer"},
                            {"type": "string"}
                        ],
                        "description": "Filter by chat ID to see conversation with a specific user. Leave empty for all conversations.",
                    },
                    "search": {
                        "type": "string",
                        "description": "Search text to filter messages (case-insensitive). Searches in message text content.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of messages to return. Default 20, max 100.",
                        "default": 20,
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Number of messages to skip (for pagination). Default 0. Messages are returned newest-first, so offset=0 gives the most recent messages.",
                        "default": 0,
                    },
                    "direction": {
                        "type": "string",
                        "description": "Filter by direction: 'received' for incoming messages only, 'sent' for outgoing replies only, or 'all' for both. Default 'all'.",
                        "default": "all",
                    },
                    "source": {
                        "type": "string",
                        "description": "Filter by source (telegram, slack, etc.). Leave empty for all sources.",
                    },
                },
            },
        ),
        # Task Management Tools
        Tool(
            name="list_tasks",
            description="List all tasks with their status. Tasks are shared across all Lobster sessions.",
            inputSchema={
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "description": "Filter by status: pending, in_progress, completed, or all (default).",
                        "default": "all",
                    },
                },
            },
        ),
        Tool(
            name="create_task",
            description="Create a new task.",
            inputSchema={
                "type": "object",
                "properties": {
                    "subject": {
                        "type": "string",
                        "description": "Brief title for the task.",
                    },
                    "description": {
                        "type": "string",
                        "description": "Detailed description of what needs to be done.",
                    },
                },
                "required": ["subject"],
            },
        ),
        Tool(
            name="update_task",
            description="Update a task's status or details.",
            inputSchema={
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "integer",
                        "description": "The task ID to update.",
                    },
                    "status": {
                        "type": "string",
                        "description": "New status: pending, in_progress, or completed.",
                    },
                    "subject": {
                        "type": "string",
                        "description": "New subject (optional).",
                    },
                    "description": {
                        "type": "string",
                        "description": "New description (optional).",
                    },
                },
                "required": ["task_id"],
            },
        ),
        Tool(
            name="get_task",
            description="Get details of a specific task.",
            inputSchema={
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "integer",
                        "description": "The task ID to retrieve.",
                    },
                },
                "required": ["task_id"],
            },
        ),
        Tool(
            name="delete_task",
            description="Delete a task.",
            inputSchema={
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "integer",
                        "description": "The task ID to delete.",
                    },
                },
                "required": ["task_id"],
            },
        ),
        Tool(
            name="transcribe_audio",
            description="Transcribe a voice message to text using local whisper.cpp (small model). Use this for messages with type='voice'. Runs entirely locally using whisper.cpp - no cloud API or API key needed.",
            inputSchema={
                "type": "object",
                "properties": {
                    "message_id": {
                        "type": "string",
                        "description": "The message ID of the voice message to transcribe.",
                    },
                },
                "required": ["message_id"],
            },
        ),
        # Headless Browser Fetch Tool
        Tool(
            name="fetch_page",
            description="Fetch a web page using a headless browser (Playwright/Chromium). Renders JavaScript fully before extracting text content. Ideal for Twitter/X links, SPAs, and other JS-heavy pages. Returns cleaned text content, not raw HTML.",
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The URL to fetch. Will be loaded in a headless Chromium browser.",
                    },
                    "wait_seconds": {
                        "type": "number",
                        "description": "Extra seconds to wait after page load for JS rendering. Default 3. Increase for slow-loading pages.",
                        "default": 3,
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Maximum seconds before giving up. Default 30.",
                        "default": 30,
                    },
                },
                "required": ["url"],
            },
        ),
        # Scheduled Jobs Tools
        Tool(
            name="create_scheduled_job",
            description="Create a new scheduled job that runs automatically via cron. Jobs run in separate Claude instances and write outputs to the task-outputs inbox.",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Unique name for the job (lowercase, hyphens allowed, e.g., 'morning-weather').",
                    },
                    "schedule": {
                        "type": "string",
                        "description": "Cron schedule expression (e.g., '0 9 * * *' for 9am daily, '*/30 * * * *' for every 30 mins).",
                    },
                    "context": {
                        "type": "string",
                        "description": "Instructions for the job. Describe what the scheduled task should do.",
                    },
                },
                "required": ["name", "schedule", "context"],
            },
        ),
        Tool(
            name="list_scheduled_jobs",
            description="List all scheduled jobs with their status and schedules.",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="get_scheduled_job",
            description="Get detailed information about a specific scheduled job.",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "The job name to retrieve.",
                    },
                },
                "required": ["name"],
            },
        ),
        Tool(
            name="update_scheduled_job",
            description="Update an existing scheduled job's schedule, context, or enabled status.",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "The job name to update.",
                    },
                    "schedule": {
                        "type": "string",
                        "description": "New cron schedule (optional).",
                    },
                    "context": {
                        "type": "string",
                        "description": "New instructions for the job (optional).",
                    },
                    "enabled": {
                        "type": "boolean",
                        "description": "Enable or disable the job (optional).",
                    },
                },
                "required": ["name"],
            },
        ),
        Tool(
            name="delete_scheduled_job",
            description="Delete a scheduled job and remove it from crontab.",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "The job name to delete.",
                    },
                },
                "required": ["name"],
            },
        ),
        Tool(
            name="check_task_outputs",
            description="Check recent outputs from scheduled tasks. Use this to review what your scheduled jobs have done.",
            inputSchema={
                "type": "object",
                "properties": {
                    "since": {
                        "type": "string",
                        "description": "Only show outputs since this ISO timestamp (optional).",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of outputs to return. Default 10.",
                        "default": 10,
                    },
                    "job_name": {
                        "type": "string",
                        "description": "Filter by job name (optional).",
                    },
                },
            },
        ),
        Tool(
            name="write_task_output",
            description="Write output from a scheduled task. Used by task instances to record their results.",
            inputSchema={
                "type": "object",
                "properties": {
                    "job_name": {
                        "type": "string",
                        "description": "The name of the job writing output.",
                    },
                    "output": {
                        "type": "string",
                        "description": "The output/result to record.",
                    },
                    "status": {
                        "type": "string",
                        "description": "Status: 'success' or 'failed'. Default 'success'.",
                        "default": "success",
                    },
                },
                "required": ["job_name", "output"],
            },
        ),
        # Brain Dump Triage Tools
        Tool(
            name="triage_brain_dump",
            description="Mark a brain dump issue as triaged. Adds 'triaged' label, removes 'raw' label, and adds a triage comment listing extracted action items. Use this after analyzing a brain dump and identifying action items.",
            inputSchema={
                "type": "object",
                "properties": {
                    "owner": {
                        "type": "string",
                        "description": "Repository owner (GitHub username or org).",
                    },
                    "repo": {
                        "type": "string",
                        "description": "Repository name (e.g., 'brain-dumps').",
                    },
                    "issue_number": {
                        "type": "integer",
                        "description": "The brain dump issue number to triage.",
                    },
                    "action_items": {
                        "type": "array",
                        "description": "List of action items extracted from the brain dump. Each item should have 'title' and optional 'description'.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "title": {
                                    "type": "string",
                                    "description": "Short title for the action item.",
                                },
                                "description": {
                                    "type": "string",
                                    "description": "Optional longer description.",
                                },
                            },
                            "required": ["title"],
                        },
                    },
                    "triage_notes": {
                        "type": "string",
                        "description": "Optional notes about the triage (e.g., context matches, patterns noticed).",
                    },
                },
                "required": ["owner", "repo", "issue_number", "action_items"],
            },
        ),
        Tool(
            name="create_action_item",
            description="Create a new GitHub issue as an action item linked to a brain dump. The action item will reference the parent brain dump issue. Returns the created issue number.",
            inputSchema={
                "type": "object",
                "properties": {
                    "owner": {
                        "type": "string",
                        "description": "Repository owner (GitHub username or org).",
                    },
                    "repo": {
                        "type": "string",
                        "description": "Repository name (e.g., 'brain-dumps').",
                    },
                    "brain_dump_issue": {
                        "type": "integer",
                        "description": "The parent brain dump issue number this action comes from.",
                    },
                    "title": {
                        "type": "string",
                        "description": "Title for the action item issue.",
                    },
                    "body": {
                        "type": "string",
                        "description": "Body/description for the action item. Should include context from brain dump.",
                    },
                    "labels": {
                        "type": "array",
                        "description": "Optional labels to apply (e.g., ['urgent', 'project:xyz']).",
                        "items": {"type": "string"},
                    },
                },
                "required": ["owner", "repo", "brain_dump_issue", "title"],
            },
        ),
        Tool(
            name="link_action_to_brain_dump",
            description="Add a comment to the brain dump issue linking to an action item issue. Use this after creating action items to maintain traceability.",
            inputSchema={
                "type": "object",
                "properties": {
                    "owner": {
                        "type": "string",
                        "description": "Repository owner (GitHub username or org).",
                    },
                    "repo": {
                        "type": "string",
                        "description": "Repository name (e.g., 'brain-dumps').",
                    },
                    "brain_dump_issue": {
                        "type": "integer",
                        "description": "The brain dump issue number.",
                    },
                    "action_issue": {
                        "type": "integer",
                        "description": "The action item issue number to link.",
                    },
                    "action_title": {
                        "type": "string",
                        "description": "Title of the action item (for the link comment).",
                    },
                },
                "required": ["owner", "repo", "brain_dump_issue", "action_issue", "action_title"],
            },
        ),
        Tool(
            name="close_brain_dump",
            description="Close a brain dump issue after all action items are created. Adds 'actioned' label, removes 'triaged' label, adds a summary comment, and closes the issue.",
            inputSchema={
                "type": "object",
                "properties": {
                    "owner": {
                        "type": "string",
                        "description": "Repository owner (GitHub username or org).",
                    },
                    "repo": {
                        "type": "string",
                        "description": "Repository name (e.g., 'brain-dumps').",
                    },
                    "issue_number": {
                        "type": "integer",
                        "description": "The brain dump issue number to close.",
                    },
                    "summary": {
                        "type": "string",
                        "description": "Summary of what was done with this brain dump.",
                    },
                    "action_issues": {
                        "type": "array",
                        "description": "List of action item issue numbers created from this brain dump.",
                        "items": {"type": "integer"},
                    },
                },
                "required": ["owner", "repo", "issue_number", "summary"],
            },
        ),
        Tool(
            name="get_brain_dump_status",
            description="Get the current triage status of a brain dump issue. Returns the issue state, labels, and any linked action items found in comments.",
            inputSchema={
                "type": "object",
                "properties": {
                    "owner": {
                        "type": "string",
                        "description": "Repository owner (GitHub username or org).",
                    },
                    "repo": {
                        "type": "string",
                        "description": "Repository name (e.g., 'brain-dumps').",
                    },
                    "issue_number": {
                        "type": "integer",
                        "description": "The brain dump issue number to check.",
                    },
                },
                "required": ["owner", "repo", "issue_number"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    """Handle tool calls."""

    if name == "wait_for_messages":
        return await handle_wait_for_messages(arguments)
    elif name == "check_inbox":
        return await handle_check_inbox(arguments)
    elif name == "send_reply":
        return await handle_send_reply(arguments)
    elif name == "mark_processed":
        return await handle_mark_processed(arguments)
    elif name == "list_sources":
        return await handle_list_sources(arguments)
    elif name == "get_stats":
        return await handle_get_stats(arguments)
    elif name == "get_conversation_history":
        return await handle_get_conversation_history(arguments)
    elif name == "list_tasks":
        return await handle_list_tasks(arguments)
    elif name == "create_task":
        return await handle_create_task(arguments)
    elif name == "update_task":
        return await handle_update_task(arguments)
    elif name == "get_task":
        return await handle_get_task(arguments)
    elif name == "delete_task":
        return await handle_delete_task(arguments)
    elif name == "transcribe_audio":
        return await handle_transcribe_audio(arguments)
    # Headless Browser Fetch
    elif name == "fetch_page":
        return await handle_fetch_page(arguments)
    # Scheduled Jobs Tools
    elif name == "create_scheduled_job":
        return await handle_create_scheduled_job(arguments)
    elif name == "list_scheduled_jobs":
        return await handle_list_scheduled_jobs(arguments)
    elif name == "get_scheduled_job":
        return await handle_get_scheduled_job(arguments)
    elif name == "update_scheduled_job":
        return await handle_update_scheduled_job(arguments)
    elif name == "delete_scheduled_job":
        return await handle_delete_scheduled_job(arguments)
    elif name == "check_task_outputs":
        return await handle_check_task_outputs(arguments)
    elif name == "write_task_output":
        return await handle_write_task_output(arguments)
    # Brain Dump Triage Tools
    elif name == "triage_brain_dump":
        return await handle_triage_brain_dump(arguments)
    elif name == "create_action_item":
        return await handle_create_action_item(arguments)
    elif name == "link_action_to_brain_dump":
        return await handle_link_action_to_brain_dump(arguments)
    elif name == "close_brain_dump":
        return await handle_close_brain_dump(arguments)
    elif name == "get_brain_dump_status":
        return await handle_get_brain_dump_status(arguments)
    else:
        return [TextContent(type="text", text=f"Unknown tool: {name}")]


async def handle_wait_for_messages(args: dict) -> list[TextContent]:
    """Block until new messages arrive in inbox, or return immediately if messages exist."""
    timeout = args.get("timeout", 300)

    # Touch heartbeat at start - signals Claude is alive and waiting for messages
    touch_heartbeat()

    # Check if messages already exist
    existing = list(INBOX_DIR.glob("*.json"))
    if existing:
        # Messages already waiting - return them immediately
        touch_heartbeat()
        return await handle_check_inbox({"limit": 10})

    # No messages - set up inotify watcher and wait
    loop = asyncio.get_event_loop()
    message_arrived = threading.Event()

    class InboxHandler(FileSystemEventHandler):
        def on_created(self, event):
            if not event.is_directory and event.src_path.endswith('.json'):
                message_arrived.set()

    observer = Observer()
    observer.schedule(InboxHandler(), str(INBOX_DIR), recursive=False)
    observer.start()

    try:
        # Wait with periodic heartbeats (every 60 seconds)
        heartbeat_interval = 60
        elapsed = 0

        while elapsed < timeout:
            wait_time = min(heartbeat_interval, timeout - elapsed)

            arrived = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda wt=wait_time: message_arrived.wait(timeout=wt)
            )

            if arrived:
                break

            # Touch heartbeat to show we're still alive
            touch_heartbeat()
            elapsed += wait_time

        if message_arrived.is_set():
            # Small delay to ensure file is fully written
            await asyncio.sleep(0.1)
            touch_heartbeat()
            return await handle_check_inbox({"limit": 10})
        else:
            # Timeout - prompt to call again
            touch_heartbeat()
            return [TextContent(
                type="text",
                text=f"⏰ No messages received in the last {timeout} seconds. Call `wait_for_messages` again to continue waiting."
            )]
    finally:
        observer.stop()
        observer.join(timeout=1)


async def handle_check_inbox(args: dict) -> list[TextContent]:
    """Check for new messages in inbox."""
    source_filter = args.get("source", "").lower()
    limit = args.get("limit", 10)

    messages = []
    for f in sorted(INBOX_DIR.glob("*.json")):
        try:
            with open(f) as fp:
                msg = json.load(fp)
                if source_filter and msg.get("source", "").lower() != source_filter:
                    continue
                msg["_filename"] = f.name
                messages.append(msg)
                if len(messages) >= limit:
                    break
        except Exception as e:
            continue

    if not messages:
        return [TextContent(type="text", text="📭 No new messages in inbox.")]

    # Format messages nicely
    output = f"📬 **{len(messages)} new message(s):**\n\n"
    for msg in messages:
        source = msg.get("source", "unknown").upper()
        user = msg.get("user_name", msg.get("username", "Unknown"))
        text = msg.get("text", "(no text)")
        ts = msg.get("timestamp", "")
        msg_id = msg.get("id", msg.get("_filename", ""))
        chat_id = msg.get("chat_id", "")
        msg_type = msg.get("type", "text")

        output += f"---\n"
        # Add voice indicator if it's a voice message
        if msg_type == "voice":
            output += f"**[{source}]** 🎤 from **{user}**\n"
            if not msg.get("transcription"):
                output += f"⚠️ Voice message needs transcription - use `transcribe_audio`\n"
        else:
            output += f"**[{source}]** from **{user}**\n"
        output += f"Chat ID: `{chat_id}` | Message ID: `{msg_id}`\n"
        output += f"Time: {ts}\n\n"
        output += f"> {text}\n\n"

    output += "---\n"
    output += "Use `send_reply` to respond, `mark_processed` when done."

    return [TextContent(type="text", text=output)]


async def handle_send_reply(args: dict) -> list[TextContent]:
    """Send a reply to a message."""
    chat_id = args.get("chat_id")
    text = args.get("text", "")
    source = args.get("source", "telegram").lower()
    buttons = args.get("buttons")
    thread_ts = args.get("thread_ts")

    if not chat_id or not text:
        return [TextContent(type="text", text="Error: chat_id and text are required.")]

    # Create reply file in outbox
    reply_id = f"{int(time.time() * 1000)}_{source}"
    reply_data = {
        "id": reply_id,
        "source": source,
        "chat_id": chat_id,
        "text": text,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    # Include buttons if provided (Telegram only)
    if buttons and source == "telegram":
        reply_data["buttons"] = buttons

    # Include thread_ts if provided (Slack only)
    if thread_ts and source == "slack":
        reply_data["thread_ts"] = thread_ts

    outbox_file = OUTBOX_DIR / f"{reply_id}.json"
    with open(outbox_file, "w") as f:
        json.dump(reply_data, f, indent=2)

    # Save a copy to sent directory for conversation history
    sent_file = SENT_DIR / f"{reply_id}.json"
    with open(sent_file, "w") as f:
        json.dump(reply_data, f, indent=2)

    button_info = f" with {sum(len(row) for row in buttons)} button(s)" if buttons else ""
    thread_info = f" (thread reply)" if thread_ts and source == "slack" else ""
    return [TextContent(type="text", text=f"✅ Reply queued for {source} (chat {chat_id}){button_info}{thread_info}:\n\n{text[:100]}{'...' if len(text) > 100 else ''}")]


async def handle_mark_processed(args: dict) -> list[TextContent]:
    """Mark a message as processed."""
    message_id = args.get("message_id", "")

    if not message_id:
        return [TextContent(type="text", text="Error: message_id is required.")]

    # Find the message file
    found = None
    for f in INBOX_DIR.glob("*.json"):
        if message_id in f.name:
            found = f
            break
        try:
            with open(f) as fp:
                msg = json.load(fp)
                if msg.get("id") == message_id:
                    found = f
                    break
        except:
            continue

    if not found:
        return [TextContent(type="text", text=f"Message not found: {message_id}")]

    # Move to processed
    dest = PROCESSED_DIR / found.name
    found.rename(dest)

    return [TextContent(type="text", text=f"✅ Message marked as processed: {message_id}")]


async def handle_list_sources(args: dict) -> list[TextContent]:
    """List available message sources."""
    output = "📡 **Message Sources:**\n\n"
    for key, source in SOURCES.items():
        status = "✅ Enabled" if source["enabled"] else "❌ Disabled"
        output += f"- **{source['name']}** ({key}): {status}\n"

    return [TextContent(type="text", text=output)]


async def handle_get_stats(args: dict) -> list[TextContent]:
    """Get inbox statistics."""
    inbox_count = len(list(INBOX_DIR.glob("*.json")))
    outbox_count = len(list(OUTBOX_DIR.glob("*.json")))
    processed_count = len(list(PROCESSED_DIR.glob("*.json")))

    # Count by source
    source_counts = {}
    for f in INBOX_DIR.glob("*.json"):
        try:
            with open(f) as fp:
                msg = json.load(fp)
                src = msg.get("source", "unknown")
                source_counts[src] = source_counts.get(src, 0) + 1
        except:
            continue

    output = "📊 **Inbox Statistics:**\n\n"
    output += f"- Inbox: {inbox_count} messages\n"
    output += f"- Outbox: {outbox_count} pending replies\n"
    output += f"- Processed: {processed_count} total\n\n"

    if source_counts:
        output += "**By Source:**\n"
        for src, count in source_counts.items():
            output += f"- {src}: {count}\n"

    return [TextContent(type="text", text=output)]


# =============================================================================
# Conversation History Handler
# =============================================================================

async def handle_get_conversation_history(args: dict) -> list[TextContent]:
    """Retrieve past messages from conversation history."""
    chat_id_filter = args.get("chat_id")
    search_text = args.get("search", "").lower().strip()
    limit = min(args.get("limit", 20), 100)
    offset = args.get("offset", 0)
    direction = args.get("direction", "all").lower()
    source_filter = args.get("source", "").lower().strip()

    # Collect all messages from processed (received) and sent directories
    all_messages = []

    # Load received messages (from processed directory)
    if direction in ("all", "received"):
        for f in PROCESSED_DIR.glob("*.json"):
            try:
                with open(f) as fp:
                    msg = json.load(fp)
                msg["_direction"] = "received"
                msg["_filename"] = f.name
                all_messages.append(msg)
            except Exception:
                continue

    # Load sent messages (from sent directory)
    if direction in ("all", "sent"):
        for f in SENT_DIR.glob("*.json"):
            try:
                with open(f) as fp:
                    msg = json.load(fp)
                msg["_direction"] = "sent"
                msg["_filename"] = f.name
                all_messages.append(msg)
            except Exception:
                continue

    # Apply filters
    if chat_id_filter is not None:
        # Compare as strings to handle both int and string chat_ids
        chat_id_str = str(chat_id_filter)
        all_messages = [m for m in all_messages if str(m.get("chat_id", "")) == chat_id_str]

    if source_filter:
        all_messages = [m for m in all_messages if m.get("source", "").lower() == source_filter]

    if search_text:
        all_messages = [m for m in all_messages if search_text in m.get("text", "").lower()]

    # Sort by timestamp (newest first)
    def parse_timestamp(msg):
        ts = msg.get("timestamp", "")
        try:
            # Handle various timestamp formats, always return UTC-aware
            if "+" in ts or ts.endswith("Z"):
                return datetime.fromisoformat(ts.replace("Z", "+00:00"))
            else:
                # Naive timestamp - assume UTC
                return datetime.fromisoformat(ts).replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            return datetime.min.replace(tzinfo=timezone.utc)

    all_messages.sort(key=parse_timestamp, reverse=True)

    total_count = len(all_messages)

    # Apply pagination
    paginated = all_messages[offset:offset + limit]

    if not paginated:
        filter_info = []
        if chat_id_filter is not None:
            filter_info.append(f"chat_id={chat_id_filter}")
        if search_text:
            filter_info.append(f"search='{search_text}'")
        if direction != "all":
            filter_info.append(f"direction={direction}")
        if source_filter:
            filter_info.append(f"source={source_filter}")
        filter_str = f" (filters: {', '.join(filter_info)})" if filter_info else ""
        return [TextContent(type="text", text=f"No messages found{filter_str}.")]

    # Format output
    showing_end = min(offset + limit, total_count)
    output = f"**Conversation History** (showing {offset + 1}-{showing_end} of {total_count}):\n\n"

    for msg in paginated:
        direction_icon = "\u2b05\ufe0f" if msg["_direction"] == "received" else "\u27a1\ufe0f"
        direction_label = "RECEIVED" if msg["_direction"] == "received" else "SENT"
        source = msg.get("source", "unknown").upper()
        chat_id = msg.get("chat_id", "")
        ts = msg.get("timestamp", "")
        text = msg.get("text", "(no text)")

        # Format timestamp nicely
        try:
            if "+" in ts or ts.endswith("Z"):
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            else:
                dt = datetime.fromisoformat(ts)
            ts_display = dt.strftime("%Y-%m-%d %H:%M")
        except (ValueError, TypeError):
            ts_display = ts

        # For received messages, show who sent it
        if msg["_direction"] == "received":
            user = msg.get("user_name", msg.get("username", "Unknown"))
            output += f"---\n"
            output += f"{direction_icon} **{direction_label}** [{source}] from **{user}** | Chat: `{chat_id}`\n"
            output += f"Time: {ts_display}\n\n"
            output += f"> {text[:500]}{'...' if len(text) > 500 else ''}\n\n"
        else:
            output += f"---\n"
            output += f"{direction_icon} **{direction_label}** [{source}] to chat `{chat_id}`\n"
            output += f"Time: {ts_display}\n\n"
            output += f"> {text[:500]}{'...' if len(text) > 500 else ''}\n\n"

    # Pagination info
    if total_count > offset + limit:
        next_offset = offset + limit
        output += f"---\n*More messages available. Use `offset={next_offset}` to see the next page.*\n"

    return [TextContent(type="text", text=output)]


# =============================================================================
# Task Management Handlers
# =============================================================================

def load_tasks() -> dict:
    """Load tasks from file."""
    try:
        with open(TASKS_FILE, "r") as f:
            return json.load(f)
    except:
        return {"tasks": [], "next_id": 1}


def save_tasks(data: dict) -> None:
    """Save tasks to file."""
    with open(TASKS_FILE, "w") as f:
        json.dump(data, f, indent=2)


async def handle_list_tasks(args: dict) -> list[TextContent]:
    """List all tasks."""
    status_filter = args.get("status", "all").lower()
    data = load_tasks()
    tasks = data.get("tasks", [])

    if status_filter != "all":
        tasks = [t for t in tasks if t.get("status", "").lower() == status_filter]

    if not tasks:
        return [TextContent(type="text", text="📋 No tasks found.")]

    # Group by status
    pending = [t for t in tasks if t.get("status") == "pending"]
    in_progress = [t for t in tasks if t.get("status") == "in_progress"]
    completed = [t for t in tasks if t.get("status") == "completed"]

    output = "📋 **Tasks:**\n\n"

    if in_progress:
        output += "**🔄 In Progress:**\n"
        for t in in_progress:
            output += f"  #{t['id']} {t['subject']}\n"
        output += "\n"

    if pending:
        output += "**⏳ Pending:**\n"
        for t in pending:
            output += f"  #{t['id']} {t['subject']}\n"
        output += "\n"

    if completed:
        output += "**✅ Completed:**\n"
        for t in completed:
            output += f"  #{t['id']} {t['subject']}\n"
        output += "\n"

    output += f"---\nTotal: {len(tasks)} task(s)"

    return [TextContent(type="text", text=output)]


async def handle_create_task(args: dict) -> list[TextContent]:
    """Create a new task."""
    subject = args.get("subject", "").strip()
    description = args.get("description", "").strip()

    if not subject:
        return [TextContent(type="text", text="Error: subject is required.")]

    data = load_tasks()
    task_id = data.get("next_id", 1)

    task = {
        "id": task_id,
        "subject": subject,
        "description": description,
        "status": "pending",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

    data["tasks"].append(task)
    data["next_id"] = task_id + 1
    save_tasks(data)

    return [TextContent(type="text", text=f"✅ Task #{task_id} created: {subject}")]


async def handle_update_task(args: dict) -> list[TextContent]:
    """Update a task."""
    task_id = args.get("task_id")
    if task_id is None:
        return [TextContent(type="text", text="Error: task_id is required.")]

    data = load_tasks()
    task = None
    for t in data["tasks"]:
        if t["id"] == task_id:
            task = t
            break

    if not task:
        return [TextContent(type="text", text=f"Error: Task #{task_id} not found.")]

    # Update fields
    if "status" in args:
        status = args["status"].lower()
        if status in ["pending", "in_progress", "completed"]:
            task["status"] = status
        else:
            return [TextContent(type="text", text=f"Error: Invalid status '{status}'. Use: pending, in_progress, completed")]

    if "subject" in args:
        task["subject"] = args["subject"]

    if "description" in args:
        task["description"] = args["description"]

    task["updated_at"] = datetime.now(timezone.utc).isoformat()
    save_tasks(data)

    status_emoji = {"pending": "⏳", "in_progress": "🔄", "completed": "✅"}.get(task["status"], "")
    return [TextContent(type="text", text=f"{status_emoji} Task #{task_id} updated: {task['subject']} [{task['status']}]")]


async def handle_get_task(args: dict) -> list[TextContent]:
    """Get task details."""
    task_id = args.get("task_id")
    if task_id is None:
        return [TextContent(type="text", text="Error: task_id is required.")]

    data = load_tasks()
    task = None
    for t in data["tasks"]:
        if t["id"] == task_id:
            task = t
            break

    if not task:
        return [TextContent(type="text", text=f"Error: Task #{task_id} not found.")]

    status_emoji = {"pending": "⏳", "in_progress": "🔄", "completed": "✅"}.get(task["status"], "")

    output = f"📋 **Task #{task['id']}**\n\n"
    output += f"**Subject:** {task['subject']}\n"
    output += f"**Status:** {status_emoji} {task['status']}\n"
    if task.get("description"):
        output += f"\n**Description:**\n{task['description']}\n"
    output += f"\n**Created:** {task.get('created_at', 'N/A')}\n"
    output += f"**Updated:** {task.get('updated_at', 'N/A')}\n"

    return [TextContent(type="text", text=output)]


async def handle_delete_task(args: dict) -> list[TextContent]:
    """Delete a task."""
    task_id = args.get("task_id")
    if task_id is None:
        return [TextContent(type="text", text="Error: task_id is required.")]

    data = load_tasks()
    original_len = len(data["tasks"])
    data["tasks"] = [t for t in data["tasks"] if t["id"] != task_id]

    if len(data["tasks"]) == original_len:
        return [TextContent(type="text", text=f"Error: Task #{task_id} not found.")]

    save_tasks(data)
    return [TextContent(type="text", text=f"🗑️ Task #{task_id} deleted.")]


# =============================================================================
# Audio Transcription Handler (Local Whisper.cpp)
# =============================================================================

# Paths for local whisper.cpp transcription
FFMPEG_PATH = Path.home() / ".local" / "bin" / "ffmpeg"
WHISPER_CPP_PATH = Path.home() / "lobster-workspace" / "whisper.cpp" / "build" / "bin" / "whisper-cli"
WHISPER_MODEL_PATH = Path.home() / "lobster-workspace" / "whisper.cpp" / "models" / "ggml-small.bin"


async def convert_ogg_to_wav(ogg_path: Path, wav_path: Path) -> bool:
    """Convert OGG audio to WAV format using FFmpeg."""
    ffmpeg = str(FFMPEG_PATH) if FFMPEG_PATH.exists() else "ffmpeg"
    cmd = [
        ffmpeg, "-i", str(ogg_path),
        "-ar", "16000",  # 16kHz sample rate
        "-ac", "1",      # Mono
        "-y",            # Overwrite
        str(wav_path)
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    _, stderr = await proc.communicate()

    return proc.returncode == 0


async def run_whisper_cpp(audio_path: Path) -> tuple[bool, str]:
    """Run whisper.cpp CLI on an audio file. Returns (success, transcription_or_error)."""
    if not WHISPER_CPP_PATH.exists():
        return False, f"whisper.cpp not found at {WHISPER_CPP_PATH}"
    if not WHISPER_MODEL_PATH.exists():
        return False, f"Whisper model not found at {WHISPER_MODEL_PATH}"

    cmd = [
        str(WHISPER_CPP_PATH),
        "-m", str(WHISPER_MODEL_PATH),
        "-f", str(audio_path),
        "-l", "en",      # English language
        "-nt",           # No timestamps in output
        "--no-prints",   # Suppress progress output
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await proc.communicate()

    if proc.returncode != 0:
        error_msg = stderr.decode().strip() if stderr else "Unknown error"
        return False, f"whisper.cpp failed: {error_msg}"

    # Parse output - whisper.cpp outputs the transcription to stdout
    transcription = stdout.decode().strip()

    # Remove any remaining timing info if present (lines starting with [)
    lines = [line for line in transcription.split('\n') if not line.strip().startswith('[')]
    transcription = ' '.join(lines).strip()

    return True, transcription


async def handle_transcribe_audio(args: dict) -> list[TextContent]:
    """Transcribe a voice message using local whisper.cpp (small model)."""
    message_id = args.get("message_id", "")

    if not message_id:
        return [TextContent(type="text", text="Error: message_id is required.")]

    # Find the message file
    msg_file = None
    msg_data = None
    for f in INBOX_DIR.glob("*.json"):
        if message_id in f.name:
            msg_file = f
            break
        try:
            with open(f) as fp:
                data = json.load(fp)
                if data.get("id") == message_id:
                    msg_file = f
                    msg_data = data
                    break
        except:
            continue

    if not msg_file:
        # Also check processed directory
        for f in PROCESSED_DIR.glob("*.json"):
            if message_id in f.name:
                msg_file = f
                break
            try:
                with open(f) as fp:
                    data = json.load(fp)
                    if data.get("id") == message_id:
                        msg_file = f
                        msg_data = data
                        break
            except:
                continue

    if not msg_file:
        return [TextContent(type="text", text=f"Error: Message not found: {message_id}")]

    # Load message data if not already loaded
    if not msg_data:
        with open(msg_file) as fp:
            msg_data = json.load(fp)

    # Check if it's a voice message
    if msg_data.get("type") != "voice":
        return [TextContent(type="text", text=f"Error: Message {message_id} is not a voice message.")]

    # Check if already transcribed
    if msg_data.get("transcription"):
        return [TextContent(type="text", text=f"✅ Already transcribed:\n\n{msg_data['transcription']}")]

    # Get the audio file path
    audio_path = Path(msg_data.get("audio_file", ""))
    if not audio_path.exists():
        return [TextContent(type="text", text=f"Error: Audio file not found: {audio_path}")]

    # Local whisper.cpp transcription
    try:
        # Convert OGG to WAV if needed
        if audio_path.suffix.lower() in [".ogg", ".oga", ".opus"]:
            wav_path = audio_path.with_suffix(".wav")
            if not wav_path.exists():
                success = await convert_ogg_to_wav(audio_path, wav_path)
                if not success:
                    return [TextContent(type="text", text="Error: Failed to convert audio to WAV format.")]
            transcribe_path = wav_path
        else:
            transcribe_path = audio_path

        # Run whisper.cpp transcription
        success, result = await run_whisper_cpp(transcribe_path)

        if not success:
            return [TextContent(type="text", text=f"Error: {result}")]

        transcription = result
        if not transcription:
            return [TextContent(type="text", text="Error: Empty transcription returned.")]

        # Update the message file with transcription
        msg_data["transcription"] = transcription
        msg_data["text"] = transcription  # Replace placeholder text
        msg_data["transcribed_at"] = datetime.now(timezone.utc).isoformat()
        msg_data["transcription_model"] = "whisper.cpp-small"

        with open(msg_file, "w") as fp:
            json.dump(msg_data, fp, indent=2)

        return [TextContent(type="text", text=f"🎤 **Transcription complete (whisper.cpp small):**\n\n{transcription}")]

    except Exception as e:
        return [TextContent(type="text", text=f"Error during transcription: {str(e)}")]


# =============================================================================
# Headless Browser Fetch Handler
# =============================================================================

async def handle_fetch_page(args: dict) -> list[TextContent]:
    """Fetch a web page using a headless browser, wait for JS to render, return text content."""
    url = args.get("url", "").strip()
    wait_seconds = args.get("wait_seconds", 3)
    timeout_seconds = args.get("timeout", 30)

    if not url:
        return [TextContent(type="text", text="Error: url is required.")]

    # Ensure URL has a scheme
    if not url.startswith("http://") and not url.startswith("https://"):
        url = "https://" + url

    try:
        from playwright.async_api import async_playwright

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                ]
            )

            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                viewport={"width": 1280, "height": 900},
                java_script_enabled=True,
            )

            page = await context.new_page()

            # Navigate to the URL
            timeout_ms = timeout_seconds * 1000
            try:
                response = await page.goto(
                    url,
                    timeout=timeout_ms,
                    wait_until="domcontentloaded",
                )
            except Exception as nav_err:
                await browser.close()
                return [TextContent(type="text", text=f"Error navigating to {url}: {str(nav_err)}")]

            # Wait additional time for JS rendering
            if wait_seconds > 0:
                await asyncio.sleep(wait_seconds)

            # Try to wait for network to be idle (best effort)
            try:
                await page.wait_for_load_state("networkidle", timeout=min(10000, timeout_ms // 2))
            except Exception:
                pass  # Don't fail if networkidle times out

            # Get the final URL (after redirects)
            final_url = page.url

            # Get page title
            title = await page.title()

            # Extract text content, trying different strategies
            text_content = ""

            # Strategy 1: For Twitter/X, look for specific tweet content
            if "twitter.com" in url or "x.com" in url:
                try:
                    # Wait for tweet content to appear
                    await page.wait_for_selector('[data-testid="tweetText"]', timeout=8000)
                    # Get all tweet texts
                    tweet_elements = await page.query_selector_all('[data-testid="tweetText"]')
                    tweet_texts = []
                    for el in tweet_elements:
                        t = await el.inner_text()
                        if t.strip():
                            tweet_texts.append(t.strip())

                    # Get tweet author
                    author_elements = await page.query_selector_all('[data-testid="User-Name"]')
                    authors = []
                    for el in author_elements:
                        a = await el.inner_text()
                        if a.strip():
                            authors.append(a.strip())

                    if tweet_texts:
                        parts = []
                        for i, tweet in enumerate(tweet_texts[:10]):  # Limit to 10 tweets
                            author = authors[i] if i < len(authors) else ""
                            if author:
                                parts.append(f"{author}\n{tweet}")
                            else:
                                parts.append(tweet)
                        text_content = "\n\n---\n\n".join(parts)
                except Exception:
                    pass  # Fall through to generic extraction

            # Strategy 2: For articles, try to find main content
            if not text_content:
                try:
                    # Try common article selectors
                    for selector in ["article", "main", '[role="main"]', ".post-content", ".article-body", ".entry-content"]:
                        el = await page.query_selector(selector)
                        if el:
                            candidate = await el.inner_text()
                            if len(candidate.strip()) > len(text_content):
                                text_content = candidate.strip()
                except Exception:
                    pass

            # Strategy 3: Fall back to full body text
            if not text_content or len(text_content) < 50:
                try:
                    text_content = await page.inner_text("body")
                except Exception:
                    text_content = ""

            # Get HTTP status
            status_code = response.status if response else "unknown"

            await browser.close()

            # Clean up the text
            if text_content:
                # Remove excessive whitespace/newlines
                import re as re_mod
                text_content = re_mod.sub(r'\n{3,}', '\n\n', text_content)
                text_content = text_content.strip()

                # Truncate if very long
                max_len = 15000
                if len(text_content) > max_len:
                    text_content = text_content[:max_len] + f"\n\n... (truncated, {len(text_content)} total chars)"

            if not text_content:
                return [TextContent(
                    type="text",
                    text=f"Page loaded but no text content extracted.\n\nURL: {final_url}\nStatus: {status_code}\nTitle: {title}"
                )]

            # Build output
            header = f"**{title}**\nURL: {final_url}\nStatus: {status_code}\n\n---\n\n"
            return [TextContent(type="text", text=header + text_content)]

    except ImportError:
        return [TextContent(type="text", text="Error: Playwright is not installed. Run: pip install playwright && python -m playwright install chromium")]
    except Exception as e:
        return [TextContent(type="text", text=f"Error fetching page: {str(e)}")]


# =============================================================================
# Scheduled Jobs Handlers
# =============================================================================

import subprocess
import re


def load_scheduled_jobs() -> dict:
    """Load scheduled jobs from file."""
    try:
        with open(SCHEDULED_JOBS_FILE, "r") as f:
            return json.load(f)
    except:
        return {"jobs": {}}


def save_scheduled_jobs(data: dict) -> None:
    """Save scheduled jobs to file."""
    with open(SCHEDULED_JOBS_FILE, "w") as f:
        json.dump(data, f, indent=2)


def validate_cron_schedule(schedule: str) -> tuple[bool, str]:
    """Validate a cron schedule expression. Returns (is_valid, error_message)."""
    parts = schedule.strip().split()
    if len(parts) != 5:
        return False, f"Cron schedule must have 5 parts (minute hour day month weekday), got {len(parts)}"

    # Basic validation for each field
    field_names = ["minute", "hour", "day", "month", "weekday"]
    field_ranges = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 7)]

    for i, (part, name, (min_val, max_val)) in enumerate(zip(parts, field_names, field_ranges)):
        # Allow *, */n, n, n-m, n,m,o patterns
        if part == "*":
            continue
        if part.startswith("*/"):
            try:
                step = int(part[2:])
                if step < 1:
                    return False, f"Invalid step value in {name}: {part}"
            except ValueError:
                return False, f"Invalid step value in {name}: {part}"
            continue

        # Handle comma-separated values and ranges
        for subpart in part.split(","):
            if "-" in subpart:
                try:
                    start, end = subpart.split("-")
                    start, end = int(start), int(end)
                    if not (min_val <= start <= max_val and min_val <= end <= max_val):
                        return False, f"Range out of bounds in {name}: {subpart}"
                except ValueError:
                    return False, f"Invalid range in {name}: {subpart}"
            else:
                try:
                    val = int(subpart)
                    if not (min_val <= val <= max_val):
                        return False, f"Value out of range in {name}: {val} (must be {min_val}-{max_val})"
                except ValueError:
                    return False, f"Invalid value in {name}: {subpart}"

    return True, ""


def cron_to_human(schedule: str) -> str:
    """Convert cron schedule to human-readable format."""
    parts = schedule.strip().split()
    if len(parts) != 5:
        return schedule

    minute, hour, day, month, weekday = parts

    # Common patterns
    if schedule == "* * * * *":
        return "Every minute"
    if minute.startswith("*/"):
        mins = minute[2:]
        if hour == "*" and day == "*" and month == "*" and weekday == "*":
            return f"Every {mins} minutes"
    if hour.startswith("*/"):
        hrs = hour[2:]
        if minute == "0" and day == "*" and month == "*" and weekday == "*":
            return f"Every {hrs} hours"
    if day == "*" and month == "*" and weekday == "*":
        if minute != "*" and hour != "*":
            return f"Daily at {hour}:{minute.zfill(2)}"
    if weekday != "*" and day == "*" and month == "*":
        days = {"0": "Sun", "1": "Mon", "2": "Tue", "3": "Wed", "4": "Thu", "5": "Fri", "6": "Sat", "7": "Sun"}
        day_name = days.get(weekday, weekday)
        if minute != "*" and hour != "*":
            return f"Every {day_name} at {hour}:{minute.zfill(2)}"

    return schedule


def validate_job_name(name: str) -> tuple[bool, str]:
    """Validate a job name. Returns (is_valid, error_message)."""
    if not name:
        return False, "Job name cannot be empty"
    if not re.match(r'^[a-z0-9][a-z0-9-]*[a-z0-9]$|^[a-z0-9]$', name):
        return False, "Job name must be lowercase alphanumeric with hyphens, cannot start/end with hyphen"
    if len(name) > 50:
        return False, "Job name must be 50 characters or less"
    return True, ""


def sync_crontab() -> tuple[bool, str]:
    """Sync jobs.json to crontab. Returns (success, message)."""
    sync_script = SCHEDULED_TASKS_DIR / "sync-crontab.sh"
    try:
        result = subprocess.run(
            [str(sync_script)],
            capture_output=True,
            text=True,
            timeout=10
        )
        if result.returncode == 0:
            return True, result.stdout
        else:
            return False, result.stderr or "Sync failed"
    except subprocess.TimeoutExpired:
        return False, "Sync script timed out"
    except Exception as e:
        return False, str(e)


async def handle_create_scheduled_job(args: dict) -> list[TextContent]:
    """Create a new scheduled job."""
    name = args.get("name", "").strip().lower()
    schedule = args.get("schedule", "").strip()
    context = args.get("context", "").strip()

    # Validate name
    valid, error = validate_job_name(name)
    if not valid:
        return [TextContent(type="text", text=f"Error: {error}")]

    # Validate schedule
    valid, error = validate_cron_schedule(schedule)
    if not valid:
        return [TextContent(type="text", text=f"Error: Invalid cron schedule - {error}")]

    if not context:
        return [TextContent(type="text", text="Error: context is required")]

    # Check if job already exists
    data = load_scheduled_jobs()
    if name in data.get("jobs", {}):
        return [TextContent(type="text", text=f"Error: Job '{name}' already exists. Use update_scheduled_job to modify it.")]

    # Create task markdown file
    now = datetime.now(timezone.utc)
    task_file = SCHEDULED_TASKS_TASKS_DIR / f"{name}.md"
    schedule_human = cron_to_human(schedule)

    task_content = f"""# {name.replace('-', ' ').title()}

**Job**: {name}
**Schedule**: {schedule_human} (`{schedule}`)
**Created**: {now.strftime('%Y-%m-%d %H:%M UTC')}

## Context

You are running as a scheduled task. The main Lobster instance created this job.

## Instructions

{context}

## Output

When you complete your task, call `write_task_output` with:
- job_name: "{name}"
- output: Your results/summary
- status: "success" or "failed"

Keep output concise. The main Lobster instance will review this later.
"""

    task_file.write_text(task_content)

    # Add to jobs.json
    data["jobs"][name] = {
        "name": name,
        "schedule": schedule,
        "schedule_human": schedule_human,
        "task_file": f"tasks/{name}.md",
        "created_at": now.isoformat(),
        "updated_at": now.isoformat(),
        "enabled": True,
        "last_run": None,
        "last_status": None,
    }
    save_scheduled_jobs(data)

    # Sync to crontab
    success, msg = sync_crontab()
    if not success:
        return [TextContent(type="text", text=f"Job created but crontab sync failed: {msg}")]

    return [TextContent(type="text", text=f"Created scheduled job '{name}'\nSchedule: {schedule_human} (`{schedule}`)\nTask file: {task_file}")]


async def handle_list_scheduled_jobs(args: dict) -> list[TextContent]:
    """List all scheduled jobs."""
    data = load_scheduled_jobs()
    jobs = data.get("jobs", {})

    if not jobs:
        return [TextContent(type="text", text="No scheduled jobs configured.\n\nUse `create_scheduled_job` to create one.")]

    output = "**Scheduled Jobs:**\n\n"

    for name, job in sorted(jobs.items()):
        status_icon = "" if job.get("enabled", True) else " (disabled)"
        schedule = job.get("schedule_human", job.get("schedule", ""))
        last_run = job.get("last_run", "never")
        last_status = job.get("last_status", "-")

        if last_run and last_run != "never":
            try:
                # Parse and format nicely
                dt = datetime.fromisoformat(last_run.replace("Z", "+00:00"))
                last_run = dt.strftime("%Y-%m-%d %H:%M")
            except:
                pass

        output += f"**{name}**{status_icon}\n"
        output += f"  Schedule: {schedule}\n"
        output += f"  Last run: {last_run} ({last_status})\n\n"

    output += f"---\nTotal: {len(jobs)} job(s)"
    return [TextContent(type="text", text=output)]


async def handle_get_scheduled_job(args: dict) -> list[TextContent]:
    """Get details of a scheduled job."""
    name = args.get("name", "").strip().lower()

    if not name:
        return [TextContent(type="text", text="Error: name is required")]

    data = load_scheduled_jobs()
    job = data.get("jobs", {}).get(name)

    if not job:
        return [TextContent(type="text", text=f"Error: Job '{name}' not found")]

    # Read task file content
    task_file = SCHEDULED_TASKS_TASKS_DIR / f"{name}.md"
    task_content = ""
    if task_file.exists():
        task_content = task_file.read_text()

    output = f"**Job: {name}**\n\n"
    output += f"**Schedule**: {job.get('schedule_human', '')} (`{job.get('schedule', '')}`)\n"
    output += f"**Enabled**: {'Yes' if job.get('enabled', True) else 'No'}\n"
    output += f"**Created**: {job.get('created_at', 'N/A')}\n"
    output += f"**Updated**: {job.get('updated_at', 'N/A')}\n"
    output += f"**Last Run**: {job.get('last_run', 'never')}\n"
    output += f"**Last Status**: {job.get('last_status', '-')}\n\n"
    output += f"---\n\n**Task File** (`{task_file}`):\n\n```markdown\n{task_content}\n```"

    return [TextContent(type="text", text=output)]


async def handle_update_scheduled_job(args: dict) -> list[TextContent]:
    """Update a scheduled job."""
    name = args.get("name", "").strip().lower()

    if not name:
        return [TextContent(type="text", text="Error: name is required")]

    data = load_scheduled_jobs()
    job = data.get("jobs", {}).get(name)

    if not job:
        return [TextContent(type="text", text=f"Error: Job '{name}' not found")]

    updated = []

    # Update schedule if provided
    if "schedule" in args and args["schedule"]:
        new_schedule = args["schedule"].strip()
        valid, error = validate_cron_schedule(new_schedule)
        if not valid:
            return [TextContent(type="text", text=f"Error: Invalid cron schedule - {error}")]
        job["schedule"] = new_schedule
        job["schedule_human"] = cron_to_human(new_schedule)
        updated.append(f"schedule -> {new_schedule}")

    # Update enabled if provided
    if "enabled" in args:
        job["enabled"] = bool(args["enabled"])
        updated.append(f"enabled -> {job['enabled']}")

    # Update context if provided
    if "context" in args and args["context"]:
        new_context = args["context"].strip()
        task_file = SCHEDULED_TASKS_TASKS_DIR / f"{name}.md"

        # Rewrite task file
        now = datetime.now(timezone.utc)
        task_content = f"""# {name.replace('-', ' ').title()}

**Job**: {name}
**Schedule**: {job.get('schedule_human', '')} (`{job.get('schedule', '')}`)
**Created**: {job.get('created_at', 'N/A')}
**Updated**: {now.strftime('%Y-%m-%d %H:%M UTC')}

## Context

You are running as a scheduled task. The main Lobster instance created this job.

## Instructions

{new_context}

## Output

When you complete your task, call `write_task_output` with:
- job_name: "{name}"
- output: Your results/summary
- status: "success" or "failed"

Keep output concise. The main Lobster instance will review this later.
"""
        task_file.write_text(task_content)
        updated.append("context (task file rewritten)")

    if not updated:
        return [TextContent(type="text", text="No changes specified. Provide schedule, context, or enabled.")]

    job["updated_at"] = datetime.now(timezone.utc).isoformat()
    save_scheduled_jobs(data)

    # Sync to crontab
    success, msg = sync_crontab()
    sync_status = "" if success else f"\n(Warning: crontab sync failed: {msg})"

    return [TextContent(type="text", text=f"Updated job '{name}':\n- " + "\n- ".join(updated) + sync_status)]


async def handle_delete_scheduled_job(args: dict) -> list[TextContent]:
    """Delete a scheduled job."""
    name = args.get("name", "").strip().lower()

    if not name:
        return [TextContent(type="text", text="Error: name is required")]

    data = load_scheduled_jobs()
    if name not in data.get("jobs", {}):
        return [TextContent(type="text", text=f"Error: Job '{name}' not found")]

    # Remove from jobs.json
    del data["jobs"][name]
    save_scheduled_jobs(data)

    # Delete task file
    task_file = SCHEDULED_TASKS_TASKS_DIR / f"{name}.md"
    if task_file.exists():
        task_file.unlink()

    # Sync to crontab
    success, msg = sync_crontab()
    sync_status = "" if success else f"\n(Warning: crontab sync failed: {msg})"

    return [TextContent(type="text", text=f"Deleted job '{name}'" + sync_status)]


async def handle_check_task_outputs(args: dict) -> list[TextContent]:
    """Check recent task outputs."""
    since = args.get("since")
    limit = args.get("limit", 10)
    job_name_filter = args.get("job_name", "").strip().lower()

    # Get all output files
    output_files = sorted(TASK_OUTPUTS_DIR.glob("*.json"), reverse=True)

    if not output_files:
        return [TextContent(type="text", text="No task outputs yet.\n\nOutputs will appear here when scheduled jobs complete.")]

    outputs = []
    since_dt = None
    if since:
        try:
            since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
        except:
            pass

    for f in output_files:
        if len(outputs) >= limit:
            break

        try:
            with open(f) as fp:
                data = json.load(fp)

            # Filter by job name
            if job_name_filter and data.get("job_name", "").lower() != job_name_filter:
                continue

            # Filter by time
            if since_dt:
                try:
                    output_dt = datetime.fromisoformat(data.get("timestamp", "").replace("Z", "+00:00"))
                    if output_dt < since_dt:
                        continue
                except:
                    pass

            data["_filename"] = f.name
            outputs.append(data)

        except Exception:
            continue

    if not outputs:
        filter_msg = ""
        if job_name_filter:
            filter_msg = f" for job '{job_name_filter}'"
        if since:
            filter_msg += f" since {since}"
        return [TextContent(type="text", text=f"No task outputs found{filter_msg}.")]

    result = f"**Recent Task Outputs** ({len(outputs)}):\n\n"

    for out in outputs:
        job = out.get("job_name", "unknown")
        ts = out.get("timestamp", "")
        status = out.get("status", "unknown")
        output = out.get("output", "(no output)")
        duration = out.get("duration_seconds")

        status_icon = "" if status == "success" else ""
        duration_str = f" ({duration}s)" if duration else ""

        # Format timestamp nicely
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            ts = dt.strftime("%Y-%m-%d %H:%M")
        except:
            pass

        result += f"---\n"
        result += f"**{job}** {status_icon} {ts}{duration_str}\n\n"
        result += f"> {output[:500]}{'...' if len(output) > 500 else ''}\n\n"

    return [TextContent(type="text", text=result)]


async def handle_write_task_output(args: dict) -> list[TextContent]:
    """Write output from a scheduled task."""
    job_name = args.get("job_name", "").strip().lower()
    output = args.get("output", "").strip()
    status = args.get("status", "success").lower()

    if not job_name:
        return [TextContent(type="text", text="Error: job_name is required")]
    if not output:
        return [TextContent(type="text", text="Error: output is required")]

    if status not in ["success", "failed"]:
        status = "success"

    now = datetime.now(timezone.utc)
    timestamp_str = now.strftime("%Y%m%d-%H%M%S")

    output_data = {
        "job_name": job_name,
        "timestamp": now.isoformat(),
        "status": status,
        "output": output,
    }

    output_file = TASK_OUTPUTS_DIR / f"{timestamp_str}-{job_name}.json"
    with open(output_file, "w") as f:
        json.dump(output_data, f, indent=2)

    return [TextContent(type="text", text=f"Output recorded for job '{job_name}'")]


# =============================================================================
# Brain Dump Triage Handlers
# =============================================================================

# Brain dump triage workflow labels
BRAIN_DUMP_LABELS = {
    "raw": "raw",           # New brain dump, not yet triaged
    "triaged": "triaged",   # Brain dump has been analyzed and action items identified
    "actioned": "actioned", # All action items have been created
    "closed": "closed",     # Brain dump is fully processed
}


async def run_gh_command(args: list[str]) -> tuple[bool, str, str]:
    """Run a gh CLI command. Returns (success, stdout, stderr)."""
    cmd = ["gh"] + args
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await proc.communicate()
    return (
        proc.returncode == 0,
        stdout.decode().strip() if stdout else "",
        stderr.decode().strip() if stderr else ""
    )


async def ensure_label_exists(owner: str, repo: str, label: str, color: str = "0e8a16", description: str = "") -> bool:
    """Ensure a label exists in the repository. Creates it if missing."""
    # Check if label exists
    success, _, _ = await run_gh_command([
        "label", "view", label,
        "--repo", f"{owner}/{repo}",
        "--json", "name"
    ])
    if success:
        return True

    # Create label
    cmd = ["label", "create", label, "--repo", f"{owner}/{repo}", "--color", color]
    if description:
        cmd.extend(["--description", description])
    success, _, stderr = await run_gh_command(cmd)
    return success


async def handle_triage_brain_dump(args: dict) -> list[TextContent]:
    """Mark a brain dump issue as triaged with action items listed."""
    owner = args.get("owner", "").strip()
    repo = args.get("repo", "").strip()
    issue_number = args.get("issue_number")
    action_items = args.get("action_items", [])
    triage_notes = args.get("triage_notes", "").strip()

    if not owner or not repo:
        return [TextContent(type="text", text="Error: owner and repo are required.")]
    if not issue_number:
        return [TextContent(type="text", text="Error: issue_number is required.")]

    # Ensure labels exist
    await ensure_label_exists(owner, repo, "raw", "d4c5f9", "New brain dump, not yet processed")
    await ensure_label_exists(owner, repo, "triaged", "0e8a16", "Brain dump has been triaged")
    await ensure_label_exists(owner, repo, "actioned", "1d76db", "All action items created")
    await ensure_label_exists(owner, repo, "action-item", "fbca04", "Action item from brain dump")

    # Build triage comment
    comment_lines = ["## Triage Complete", ""]

    if action_items:
        comment_lines.append(f"**{len(action_items)} action item(s) identified:**")
        comment_lines.append("")
        for i, item in enumerate(action_items, 1):
            title = item.get("title", "Untitled")
            desc = item.get("description", "")
            comment_lines.append(f"{i}. **{title}**")
            if desc:
                comment_lines.append(f"   - {desc}")
        comment_lines.append("")
        comment_lines.append("Action items will be created as separate issues and linked back here.")
    else:
        comment_lines.append("No action items identified - this brain dump is for reference only.")

    if triage_notes:
        comment_lines.append("")
        comment_lines.append("### Notes")
        comment_lines.append(triage_notes)

    comment_lines.append("")
    comment_lines.append("---")
    comment_lines.append(f"*Triaged at {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}*")

    comment_body = "\n".join(comment_lines)

    # Add comment
    success, stdout, stderr = await run_gh_command([
        "issue", "comment", str(issue_number),
        "--repo", f"{owner}/{repo}",
        "--body", comment_body
    ])
    if not success:
        return [TextContent(type="text", text=f"Error adding triage comment: {stderr}")]

    # Remove 'raw' label if present
    await run_gh_command([
        "issue", "edit", str(issue_number),
        "--repo", f"{owner}/{repo}",
        "--remove-label", "raw"
    ])

    # Add 'triaged' label
    success, _, stderr = await run_gh_command([
        "issue", "edit", str(issue_number),
        "--repo", f"{owner}/{repo}",
        "--add-label", "triaged"
    ])
    if not success:
        return [TextContent(type="text", text=f"Error adding triaged label: {stderr}")]

    return [TextContent(
        type="text",
        text=f"Brain dump #{issue_number} triaged.\n- {len(action_items)} action item(s) identified\n- Label updated: raw -> triaged\n- Triage comment added"
    )]


async def handle_create_action_item(args: dict) -> list[TextContent]:
    """Create an action item issue linked to a brain dump."""
    owner = args.get("owner", "").strip()
    repo = args.get("repo", "").strip()
    brain_dump_issue = args.get("brain_dump_issue")
    title = args.get("title", "").strip()
    body = args.get("body", "").strip()
    labels = args.get("labels", [])

    if not owner or not repo:
        return [TextContent(type="text", text="Error: owner and repo are required.")]
    if not brain_dump_issue:
        return [TextContent(type="text", text="Error: brain_dump_issue is required.")]
    if not title:
        return [TextContent(type="text", text="Error: title is required.")]

    # Ensure action-item label exists
    await ensure_label_exists(owner, repo, "action-item", "fbca04", "Action item from brain dump")

    # Build issue body
    issue_body_lines = []
    if body:
        issue_body_lines.append(body)
        issue_body_lines.append("")

    issue_body_lines.append("---")
    issue_body_lines.append(f"**Source:** Brain dump #{brain_dump_issue}")
    issue_body_lines.append("")
    issue_body_lines.append(f"*Created from brain dump triage*")

    issue_body = "\n".join(issue_body_lines)

    # Create the issue
    cmd = [
        "issue", "create",
        "--repo", f"{owner}/{repo}",
        "--title", title,
        "--body", issue_body,
        "--label", "action-item"
    ]

    # Add additional labels
    for label in labels:
        if label and label != "action-item":
            cmd.extend(["--label", label])

    success, stdout, stderr = await run_gh_command(cmd)
    if not success:
        return [TextContent(type="text", text=f"Error creating action item: {stderr}")]

    # Parse issue number from URL (gh returns URL like https://github.com/owner/repo/issues/123)
    action_issue_number = None
    if stdout:
        # Extract issue number from URL
        parts = stdout.rstrip("/").split("/")
        if parts:
            try:
                action_issue_number = int(parts[-1])
            except ValueError:
                pass

    if not action_issue_number:
        return [TextContent(
            type="text",
            text=f"Action item created but could not parse issue number.\nURL: {stdout}"
        )]

    return [TextContent(
        type="text",
        text=f"Action item created: #{action_issue_number}\n- Title: {title}\n- Linked to brain dump #{brain_dump_issue}\n- URL: {stdout}"
    )]


async def handle_link_action_to_brain_dump(args: dict) -> list[TextContent]:
    """Add a comment to brain dump linking to an action item."""
    owner = args.get("owner", "").strip()
    repo = args.get("repo", "").strip()
    brain_dump_issue = args.get("brain_dump_issue")
    action_issue = args.get("action_issue")
    action_title = args.get("action_title", "").strip()

    if not owner or not repo:
        return [TextContent(type="text", text="Error: owner and repo are required.")]
    if not brain_dump_issue:
        return [TextContent(type="text", text="Error: brain_dump_issue is required.")]
    if not action_issue:
        return [TextContent(type="text", text="Error: action_issue is required.")]

    # Build link comment
    title_part = f": {action_title}" if action_title else ""
    comment_body = f"Action item created: #{action_issue}{title_part}"

    # Add comment
    success, _, stderr = await run_gh_command([
        "issue", "comment", str(brain_dump_issue),
        "--repo", f"{owner}/{repo}",
        "--body", comment_body
    ])
    if not success:
        return [TextContent(type="text", text=f"Error adding link comment: {stderr}")]

    return [TextContent(
        type="text",
        text=f"Linked action item #{action_issue} to brain dump #{brain_dump_issue}"
    )]


async def handle_close_brain_dump(args: dict) -> list[TextContent]:
    """Close a brain dump issue with summary."""
    owner = args.get("owner", "").strip()
    repo = args.get("repo", "").strip()
    issue_number = args.get("issue_number")
    summary = args.get("summary", "").strip()
    action_issues = args.get("action_issues", [])

    if not owner or not repo:
        return [TextContent(type="text", text="Error: owner and repo are required.")]
    if not issue_number:
        return [TextContent(type="text", text="Error: issue_number is required.")]
    if not summary:
        return [TextContent(type="text", text="Error: summary is required.")]

    # Ensure labels exist
    await ensure_label_exists(owner, repo, "actioned", "1d76db", "All action items created")
    await ensure_label_exists(owner, repo, "closed", "000000", "Brain dump fully processed")

    # Build closure comment
    comment_lines = ["## Brain Dump Processed", ""]
    comment_lines.append(summary)
    comment_lines.append("")

    if action_issues:
        comment_lines.append("### Action Items Created")
        for issue_num in action_issues:
            comment_lines.append(f"- #{issue_num}")
        comment_lines.append("")

    comment_lines.append("---")
    comment_lines.append(f"*Closed at {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}*")

    comment_body = "\n".join(comment_lines)

    # Add closure comment
    success, _, stderr = await run_gh_command([
        "issue", "comment", str(issue_number),
        "--repo", f"{owner}/{repo}",
        "--body", comment_body
    ])
    if not success:
        return [TextContent(type="text", text=f"Error adding closure comment: {stderr}")]

    # Update labels: remove triaged, add actioned
    await run_gh_command([
        "issue", "edit", str(issue_number),
        "--repo", f"{owner}/{repo}",
        "--remove-label", "triaged"
    ])

    await run_gh_command([
        "issue", "edit", str(issue_number),
        "--repo", f"{owner}/{repo}",
        "--add-label", "actioned"
    ])

    # Close the issue
    success, _, stderr = await run_gh_command([
        "issue", "close", str(issue_number),
        "--repo", f"{owner}/{repo}",
        "--reason", "completed"
    ])
    if not success:
        return [TextContent(type="text", text=f"Error closing issue: {stderr}")]

    action_count = len(action_issues) if action_issues else 0
    return [TextContent(
        type="text",
        text=f"Brain dump #{issue_number} closed.\n- {action_count} action item(s) created\n- Label: actioned\n- Status: closed (completed)"
    )]


async def handle_get_brain_dump_status(args: dict) -> list[TextContent]:
    """Get the current status of a brain dump issue."""
    owner = args.get("owner", "").strip()
    repo = args.get("repo", "").strip()
    issue_number = args.get("issue_number")

    if not owner or not repo:
        return [TextContent(type="text", text="Error: owner and repo are required.")]
    if not issue_number:
        return [TextContent(type="text", text="Error: issue_number is required.")]

    # Get issue details
    success, stdout, stderr = await run_gh_command([
        "issue", "view", str(issue_number),
        "--repo", f"{owner}/{repo}",
        "--json", "title,state,labels,comments"
    ])
    if not success:
        return [TextContent(type="text", text=f"Error fetching issue: {stderr}")]

    try:
        issue_data = json.loads(stdout)
    except json.JSONDecodeError:
        return [TextContent(type="text", text=f"Error parsing issue data: {stdout}")]

    title = issue_data.get("title", "Unknown")
    state = issue_data.get("state", "unknown")
    labels = [l.get("name", "") for l in issue_data.get("labels", [])]
    comments = issue_data.get("comments", [])

    # Determine workflow status
    workflow_status = "unknown"
    if "actioned" in labels or state.lower() == "closed":
        workflow_status = "completed"
    elif "triaged" in labels:
        workflow_status = "triaged"
    elif "raw" in labels:
        workflow_status = "raw"
    else:
        workflow_status = "untagged"

    # Find linked action items from comments
    action_items = []
    for comment in comments:
        body = comment.get("body", "")
        # Look for patterns like "Action item created: #123" or "#{number}"
        import re
        matches = re.findall(r"Action item created: #(\d+)", body)
        action_items.extend([int(m) for m in matches])

    output_lines = [
        f"## Brain Dump #{issue_number}",
        "",
        f"**Title:** {title}",
        f"**State:** {state}",
        f"**Workflow Status:** {workflow_status}",
        f"**Labels:** {', '.join(labels) if labels else 'none'}",
        "",
    ]

    if action_items:
        output_lines.append(f"**Linked Action Items:** {len(action_items)}")
        for item in action_items:
            output_lines.append(f"- #{item}")
    else:
        output_lines.append("**Linked Action Items:** none")

    return [TextContent(type="text", text="\n".join(output_lines))]


async def main():
    """Run the MCP server."""
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
