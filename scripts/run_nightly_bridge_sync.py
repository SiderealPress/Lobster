#!/usr/bin/env python3
"""
Nightly bridge-sync CLI: push canonical memory into the user model DB.

Replaces the inline Python that was previously hand-copied into step 10 of
`.claude/agents/nightly-consolidation.md` (and its private-overlay copy).
That inline code had two independent bugs, both filed and reproduced live:

- #2180 (ModuleNotFoundError): `sys.path.insert(0, 'src')` followed by an
  import of `user_model.bridges` rooted at the `mcp` package name resolves
  `mcp` to the *installed* MCP-SDK package (which has no `user_model`
  submodule), not to `src/mcp/` — because `src/mcp` has no `__init__.py` and
  loses import precedence to the installed package. This script sidesteps
  the collision entirely by putting `src/mcp` itself on `sys.path` and
  importing `user_model.*` with no `mcp`-package prefix (see
  `_import_user_model()` below).

- #2181 (TypeError): opening the DB with a bare, unconfigured sqlite3
  connection leaves `row_factory` as `None`. `user_model.db` helpers (e.g.
  `get_active_narrative_arcs()`) do dict-style row access (`row["id"]`),
  which raises `TypeError: tuple indices must be integers or slices, not str`
  without `row_factory = sqlite3.Row`. This script always opens the
  connection via `user_model.db.open_db()`, which sets that correctly (see
  `open_connection()` below).

It also preserves the fix from the earlier bug (PR #2136): `--canonical-root`
(read root for projects/*.md and priorities.md) and `--workspace-root` (write
root for the user-model DB and _context.md) are two independent, required
arguments — never a single shared path used for both reads and writes.

Usage:
    uv run python scripts/run_nightly_bridge_sync.py \\
        --canonical-root ~/lobster-user-config \\
        --workspace-root ~/lobster-workspace
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any


def _import_user_model():
    """
    Import user_model.db / user_model.bridges without the `mcp.`-prefix
    collision described in #2180.

    `src/mcp` has no `__init__.py` (namespace package), so it loses import
    precedence to the installed MCP-SDK `mcp` package if `import mcp...` is
    used after only `src` is on sys.path. Putting `src/mcp` itself (not
    `src`) on sys.path and importing `user_model.*` directly avoids the `mcp`
    name entirely.
    """
    src_mcp_dir = Path(__file__).resolve().parent.parent / "src" / "mcp"
    src_mcp_str = str(src_mcp_dir)
    if src_mcp_str not in sys.path:
        sys.path.insert(0, src_mcp_str)

    from user_model.bridges import (  # type: ignore[import-not-found]
        sync_priorities_to_attention,
        sync_projects_to_arcs,
        write_context_cache,
    )
    from user_model.db import open_db  # type: ignore[import-not-found]

    return open_db, sync_projects_to_arcs, sync_priorities_to_attention, write_context_cache


def open_connection(db_path: Path) -> sqlite3.Connection:
    """
    Open the user-model DB via user_model.db.open_db(), which sets
    conn.row_factory = sqlite3.Row (required by the bridge functions'
    dict-style row access — guards against #2181).
    """
    open_db, _, _, _ = _import_user_model()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return open_db(db_path)


def run(
    canonical_root: Path,
    workspace_root: Path,
    db_path: Path | None = None,
) -> dict[str, Any]:
    """
    Run the full bridge pass:
      - sync canonical_root's projects/*.md into narrative arcs
      - sync canonical_root's priorities.md into attention items
      - write workspace_root's user-model/_context.md

    canonical_root and workspace_root are independent: canonical_root is
    read-only (projects/priorities source), workspace_root owns the DB and
    the generated _context.md. They may point at the same directory, but
    must never be silently collapsed into one shared path (the bug PR #2136
    fixed).
    """
    _, sync_projects_to_arcs, sync_priorities_to_attention, write_context_cache = (
        _import_user_model()
    )

    resolved_db_path = db_path or (workspace_root / "data" / "memory.db")
    conn = open_connection(resolved_db_path)

    summary: dict[str, Any] = {}
    try:
        try:
            summary["projects"] = sync_projects_to_arcs(conn, str(canonical_root))
        except Exception as e:  # noqa: BLE001 - summarized, not swallowed silently
            summary["projects"] = {"error": str(e)}

        try:
            summary["priorities"] = sync_priorities_to_attention(conn, str(canonical_root))
        except Exception as e:  # noqa: BLE001
            summary["priorities"] = {"error": str(e)}

        try:
            write_context_cache(conn, str(workspace_root))
            summary["context_cache"] = "written"
        except Exception as e:  # noqa: BLE001
            summary["context_cache"] = {"error": str(e)}

        conn.commit()
    finally:
        conn.close()

    return summary


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Sync canonical memory (projects, priorities) into the user-model DB "
        "and regenerate _context.md."
    )
    parser.add_argument(
        "--canonical-root",
        required=True,
        type=Path,
        help="Root directory containing memory/canonical/{projects/,priorities.md} "
        "(the read source). E.g. ~/lobster-user-config.",
    )
    parser.add_argument(
        "--workspace-root",
        required=True,
        type=Path,
        help="Root directory that owns data/memory.db and receives the written "
        "user-model/_context.md (the write target). E.g. ~/lobster-workspace.",
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        default=None,
        help="Override the DB path. Defaults to <workspace-root>/data/memory.db.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    canonical_root = args.canonical_root.expanduser().resolve()
    workspace_root = args.workspace_root.expanduser().resolve()
    db_path = args.db_path.expanduser().resolve() if args.db_path else None

    result = run(canonical_root=canonical_root, workspace_root=workspace_root, db_path=db_path)
    print(json.dumps(result, indent=2))

    had_error = any(isinstance(v, dict) and "error" in v for v in result.values())
    return 1 if had_error else 0


if __name__ == "__main__":
    raise SystemExit(main())
