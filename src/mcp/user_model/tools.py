"""
MCP Tool handlers for the User Model subsystem.

Provides 9 MCP tools:
  model_observe      — record observations from messages
  model_query        — structured query over the model
  model_preferences  — context-resolved preference list
  model_reflect      — trigger heuristic synthesis pass
  model_correct      — apply user correction
  model_inspect      — deep-read a specific entity
  model_attention    — get scored attention stack
  model_infer        — context-aware prediction of user state and needs
  model_user_context — get user profile context for system prompt injection

Tool definitions (for inbox_server.py list_tools) are exported as
USER_MODEL_TOOLS (list of Tool dicts).

Handler functions return list[TextContent] compatible with MCP SDK.
"""

import json
import sqlite3
from typing import Any

from .db import set_metadata_value
from .introspection import (
    correct_preference,
    get_attention,
    get_resolved_preferences,
    inspect_entity,
    query_model,
    reflect,
)
from .markdown_sync import sync_all
from .observation import observe_message
from .preference_graph import add_preference
from .schema import NodeFlexibility, NodeSource, NodeType


# ---------------------------------------------------------------------------
# Tool definitions (injected into inbox_server.list_tools)
# ---------------------------------------------------------------------------

USER_MODEL_TOOL_DEFINITIONS = [
    {
        "name": "model_observe",
        "description": (
            "Record an observation about the user from a message or interaction. "
            "Extracts signals (sentiment, topic, energy, corrections, preferences) "
            "and persists them for inference. Call this after significant user interactions."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "message_text": {
                    "type": "string",
                    "description": "The user's message text to extract signals from.",
                },
                "message_id": {
                    "type": "string",
                    "description": "Unique ID of the source message.",
                },
                "context": {
                    "type": "string",
                    "description": "Active context(s) at observation time (e.g. 'coding,work').",
                    "default": "",
                },
                "observation": {
                    "type": "string",
                    "description": "Optional: explicit observation to record (bypasses auto-extraction).",
                },
                "observation_type": {
                    "type": "string",
                    "description": "Type of explicit observation: preference, sentiment, topic, energy, correction.",
                    "default": "preference",
                },
                "confidence": {
                    "type": "number",
                    "description": "Confidence in the observation (0.0–1.0). Default: 0.7",
                    "default": 0.7,
                },
            },
            "required": ["message_text", "message_id"],
        },
    },
    {
        "name": "model_query",
        "description": (
            "Structured query over the user model. Use this for precise filtering that "
            "file reads can't express. Query types: preferences, observations, emotions, "
            "arcs, patterns, attention, contradictions, blind_spots, meta."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query_type": {
                    "type": "string",
                    "description": "What to query: preferences | observations | emotions | arcs | patterns | attention | contradictions | blind_spots | meta",
                },
                "filters": {
                    "type": "object",
                    "description": "Optional filters (e.g. {node_type: 'value', min_confidence: 0.7, hours: 24})",
                    "default": {},
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum results to return. Default: 20.",
                    "default": 20,
                },
            },
            "required": ["query_type"],
        },
    },
    {
        "name": "model_preferences",
        "description": (
            "Get inheritance-resolved preferences for given contexts. "
            "Handles graph traversal, overrides, and conflict resolution. "
            "Use when you need precise preference data for a decision. "
            "For general context-reading, grep ~/lobster-workspace/user-model/preferences/ instead."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "contexts": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of active contexts (e.g. ['work', 'coding', 'morning']). Empty = all contexts.",
                    "default": [],
                },
                "min_confidence": {
                    "type": "number",
                    "description": "Minimum confidence threshold (0.0–1.0). Default: 0.5",
                    "default": 0.5,
                },
            },
        },
    },
    {
        "name": "model_reflect",
        "description": (
            "Trigger a heuristic synthesis pass: detect contradictions, refresh "
            "attention stack, sync markdown files. Run this after significant interactions "
            "or when the model needs updating. The full LLM synthesis runs nightly."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "focus": {
                    "type": "string",
                    "description": "Optional focus area for reflection (e.g. 'work habits', 'communication').",
                },
                "sync_files": {
                    "type": "boolean",
                    "description": "If true, also sync the markdown file layer. Default: true.",
                    "default": True,
                },
            },
        },
    },
    {
        "name": "model_correct",
        "description": (
            "Apply a user correction to the model. Use when the user explicitly says "
            "something is wrong or wants to update their profile. Sets confidence to 1.0 "
            "and marks source as 'corrected' — corrections never decay."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "node_id": {
                    "type": "string",
                    "description": "ID of the preference node to correct. Get IDs from model_query.",
                },
                "corrected_description": {
                    "type": "string",
                    "description": "The corrected description of this preference.",
                },
                "corrected_strength": {
                    "type": "number",
                    "description": "Optional: new strength value (0.0–1.0).",
                },
            },
            "required": ["node_id", "corrected_description"],
        },
    },
    {
        "name": "model_inspect",
        "description": (
            "Deep-read a specific entity in the user model, including its full history, "
            "graph edges, and metadata. Use when you need complete information about a "
            "specific preference node or other entity."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "entity_id": {
                    "type": "string",
                    "description": "ID of the entity to inspect.",
                },
                "entity_type": {
                    "type": "string",
                    "description": "Type of entity: preference (default).",
                    "default": "preference",
                },
            },
            "required": ["entity_id"],
        },
    },
    {
        "name": "model_attention",
        "description": (
            "Get the dynamically scored attention stack — what the user should focus on "
            "right now. Returns items sorted by priority score, factoring in urgency, "
            "importance, value alignment, and recency."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "contexts": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Active contexts to filter by. Empty = all contexts.",
                    "default": [],
                },
                "max_items": {
                    "type": "integer",
                    "description": "Maximum items to return. Default: 10.",
                    "default": 10,
                },
            },
        },
    },
    {
        "name": "model_infer",
        "description": (
            "Context-aware prediction of user state and needs. Returns mood estimate "
            "(VAD), response style hint, likely next request type, and optional value "
            "alignment score. Results are cached for 30 minutes."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "contexts": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Active contexts (e.g. ['work', 'coding']). Empty = general.",
                    "default": [],
                },
                "recent_message": {
                    "type": "string",
                    "description": "The most recent user message for intent detection.",
                },
                "task_description": {
                    "type": "string",
                    "description": "Optional task description for value alignment scoring.",
                },
                "force_refresh": {
                    "type": "boolean",
                    "description": "If true, bypass cache and recompute. Default: false.",
                    "default": False,
                },
            },
        },
    },
    {
        "name": "model_user_context",
        "description": (
            "Get the user's profile context for system prompt injection. "
            "With deep=false (default), returns a compact ~150 token header. "
            "With deep=true, returns full profile, goals, and preferences."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "deep": {
                    "type": "boolean",
                    "description": "If true, return full profile data. Default: false.",
                    "default": False,
                },
            },
        },
    },
]


# ---------------------------------------------------------------------------
# Handler functions
# ---------------------------------------------------------------------------

def handle_model_observe(conn: sqlite3.Connection, args: dict, workspace_path: str | None = None) -> str:
    """Handle model_observe tool call."""
    message_text = args.get("message_text", "")
    message_id = args.get("message_id", "")
    context = args.get("context", "")
    explicit_observation = args.get("observation")
    observation_type = args.get("observation_type", "preference")
    confidence = float(args.get("confidence", 0.7))

    if not message_text or not message_id:
        return json.dumps({"error": "message_text and message_id are required"})

    if explicit_observation:
        # Record an explicit observation directly
        from .db import insert_observation
        from .schema import Observation, ObservationSignalType
        from datetime import datetime

        type_map = {
            "preference": ObservationSignalType.PREFERENCE,
            "sentiment": ObservationSignalType.SENTIMENT,
            "topic": ObservationSignalType.TOPIC,
            "energy": ObservationSignalType.ENERGY,
            "correction": ObservationSignalType.CORRECTION,
            "emotion": ObservationSignalType.EMOTION,
            "timing": ObservationSignalType.TIMING,
        }
        sig_type = type_map.get(observation_type, ObservationSignalType.PREFERENCE)
        obs = Observation(
            id=None,
            message_id=message_id,
            signal_type=sig_type,
            content=explicit_observation,
            confidence=confidence,
            context=context,
            observed_at=datetime.utcnow(),
        )
        obs_id = insert_observation(conn, obs)
        result = {"success": True, "mode": "explicit", "obs_id": obs_id, "signal_type": observation_type}
    else:
        # Auto-extract signals from message text
        obs_ids = observe_message(
            conn,
            message_text=message_text,
            message_id=message_id,
            context=context,
        )
        result = {
            "success": True,
            "mode": "auto_extracted",
            "signals_extracted": len(obs_ids),
            "obs_ids": obs_ids,
        }

    return json.dumps(result)


def handle_model_query(conn: sqlite3.Connection, args: dict) -> str:
    """Handle model_query tool call."""
    query_type = args.get("query_type", "")
    filters = args.get("filters", {})
    limit = int(args.get("limit", 20))

    if not query_type:
        return json.dumps({"error": "query_type is required"})

    result = query_model(conn, query_type, filters, limit)
    return json.dumps(result, default=str)


def handle_model_preferences(conn: sqlite3.Connection, args: dict) -> str:
    """Handle model_preferences tool call."""
    contexts = args.get("contexts", [])
    min_confidence = float(args.get("min_confidence", 0.5))
    result = get_resolved_preferences(conn, contexts, min_confidence)
    return json.dumps(result, default=str)


def handle_model_reflect(
    conn: sqlite3.Connection, args: dict, workspace_path: str | None = None
) -> str:
    """Handle model_reflect tool call."""
    focus = args.get("focus")
    sync_files = bool(args.get("sync_files", True))

    result = reflect(conn, focus=focus)

    if sync_files and workspace_path:
        try:
            sync_result = sync_all(conn, workspace_path)
            result["markdown_sync"] = sync_result
        except Exception as e:
            result["markdown_sync_error"] = str(e)

    return json.dumps(result, default=str)


def handle_model_correct(conn: sqlite3.Connection, args: dict) -> str:
    """Handle model_correct tool call."""
    node_id = args.get("node_id", "").strip()
    corrected_description = args.get("corrected_description", "").strip()
    corrected_strength = args.get("corrected_strength")

    if not node_id or not corrected_description:
        return json.dumps({"error": "node_id and corrected_description are required"})

    if corrected_strength is not None:
        corrected_strength = float(corrected_strength)
        corrected_strength = max(0.0, min(1.0, corrected_strength))

    result = correct_preference(conn, node_id, corrected_description, corrected_strength)
    return json.dumps(result, default=str)


def handle_model_inspect(conn: sqlite3.Connection, args: dict) -> str:
    """Handle model_inspect tool call."""
    entity_id = args.get("entity_id", "").strip()
    entity_type = args.get("entity_type", "preference")

    if not entity_id:
        return json.dumps({"error": "entity_id is required"})

    result = inspect_entity(conn, entity_id, entity_type)
    return json.dumps(result, default=str)


def handle_model_attention(conn: sqlite3.Connection, args: dict) -> str:
    """Handle model_attention tool call."""
    contexts = args.get("contexts", [])
    max_items = int(args.get("max_items", 10))

    result = get_attention(conn, contexts, max_items)
    return json.dumps(result, default=str)


def handle_model_infer(conn: sqlite3.Connection, args: dict) -> str:
    """Handle model_infer tool call."""
    from .infer import run_inference

    contexts = args.get("contexts", [])
    recent_message = args.get("recent_message")
    task_description = args.get("task_description")
    force_refresh = bool(args.get("force_refresh", False))

    result = run_inference(
        conn,
        contexts=contexts,
        recent_message=recent_message,
        task_description=task_description,
        force_refresh=force_refresh,
    )
    return json.dumps(result, default=str)


def handle_model_user_context(conn: sqlite3.Connection, args: dict) -> str:
    """Handle model_user_context tool call."""
    from .profile import get_compact_context, read_all_profiles

    deep = bool(args.get("deep", False))

    if deep:
        profiles = read_all_profiles()
        return json.dumps(profiles, default=str)
    else:
        context = get_compact_context()
        return json.dumps({"context": context})


# ---------------------------------------------------------------------------
# Dispatch table
# ---------------------------------------------------------------------------

def dispatch(
    tool_name: str,
    args: dict,
    conn: sqlite3.Connection,
    workspace_path: str | None = None,
) -> str:
    """
    Dispatch a tool call to the appropriate handler.
    Returns a JSON string result.
    """
    handlers = {
        "model_observe": lambda: handle_model_observe(conn, args, workspace_path),
        "model_query": lambda: handle_model_query(conn, args),
        "model_preferences": lambda: handle_model_preferences(conn, args),
        "model_reflect": lambda: handle_model_reflect(conn, args, workspace_path),
        "model_correct": lambda: handle_model_correct(conn, args),
        "model_inspect": lambda: handle_model_inspect(conn, args),
        "model_attention": lambda: handle_model_attention(conn, args),
        "model_infer": lambda: handle_model_infer(conn, args),
        "model_user_context": lambda: handle_model_user_context(conn, args),
    }

    handler = handlers.get(tool_name)
    if not handler:
        return json.dumps({"error": f"Unknown user model tool: {tool_name}"})

    try:
        return handler()
    except Exception as e:
        return json.dumps({"error": str(e), "tool": tool_name})
