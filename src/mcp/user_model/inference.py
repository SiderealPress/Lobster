"""
Nightly Inference Pipeline: LLM synthesis, decay application, consolidation.

This module orchestrates the nightly pass that:
1. Consumes unprocessed observations
2. Applies preference decay
3. Detects new contradictions
4. Refreshes the attention stack
5. Syncs the markdown file layer
6. Updates last_consolidation_at metadata

Called by: a scheduled job (nightly cron) or triggered via model_reflect.
Depends on: all other user_model modules.
"""

import sqlite3
from datetime import datetime
from typing import Any

from .db import (
    get_unprocessed_observations,
    mark_observations_processed,
    set_metadata_value,
)
from .markdown_sync import sync_all
from .observation import observe_message
from .prediction import refresh_attention_stack
from .preference_graph import apply_decay
from .self_knowledge import detect_contradictions, persist_new_contradictions


def run_consolidation(
    conn: sqlite3.Connection,
    workspace_path: str | None = None,
    days_since_last_run: int = 1,
) -> dict[str, Any]:
    """
    Run the full nightly consolidation pipeline.

    Steps:
    1. Apply preference decay
    2. Detect and persist new contradictions
    3. Process pending observations (mark as processed)
    4. Refresh attention stack
    5. Sync markdown file layer
    6. Update last_consolidation_at

    Returns a summary dict.
    """
    summary: dict[str, Any] = {
        "started_at": datetime.utcnow().isoformat(),
        "steps": [],
    }

    # Step 1: Apply preference decay
    try:
        decayed = apply_decay(conn, days_since_last_run=days_since_last_run)
        summary["steps"].append({"step": "decay", "nodes_affected": decayed})
    except Exception as e:
        summary["steps"].append({"step": "decay", "error": str(e)})

    # Step 2: Detect contradictions
    try:
        new_contradictions = detect_contradictions(conn)
        if new_contradictions:
            ids = persist_new_contradictions(conn, new_contradictions)
            summary["steps"].append({
                "step": "contradiction_detection",
                "new_contradictions": len(ids),
            })
        else:
            summary["steps"].append({
                "step": "contradiction_detection",
                "new_contradictions": 0,
            })
    except Exception as e:
        summary["steps"].append({"step": "contradiction_detection", "error": str(e)})

    # Step 3: Process pending observations
    try:
        pending = get_unprocessed_observations(conn, limit=500)
        if pending:
            obs_ids = [o.id for o in pending if o.id]
            mark_observations_processed(conn, obs_ids)
            summary["steps"].append({
                "step": "observation_processing",
                "processed": len(obs_ids),
            })
        else:
            summary["steps"].append({"step": "observation_processing", "processed": 0})
    except Exception as e:
        summary["steps"].append({"step": "observation_processing", "error": str(e)})

    # Step 4: Refresh attention stack
    try:
        att_ids = refresh_attention_stack(conn)
        summary["steps"].append({
            "step": "attention_refresh",
            "items": len(att_ids),
        })
    except Exception as e:
        summary["steps"].append({"step": "attention_refresh", "error": str(e)})

    # Step 5: Sync markdown file layer
    if workspace_path:
        try:
            sync_results = sync_all(conn, workspace_path)
            summary["steps"].append({
                "step": "markdown_sync",
                "files_written": sync_results.get("files_written", 0),
            })
        except Exception as e:
            summary["steps"].append({"step": "markdown_sync", "error": str(e)})

    # Step 6: Update metadata
    now_iso = datetime.utcnow().isoformat()
    set_metadata_value(conn, "last_consolidation_at", now_iso)
    summary["completed_at"] = now_iso

    return summary


def process_observation_batch(
    conn: sqlite3.Connection,
    batch_size: int = 50,
) -> int:
    """
    Process a batch of unprocessed observations.
    Returns the number processed.
    """
    pending = get_unprocessed_observations(conn, limit=batch_size)
    if not pending:
        return 0

    obs_ids = [o.id for o in pending if o.id]
    mark_observations_processed(conn, obs_ids)
    return len(obs_ids)
