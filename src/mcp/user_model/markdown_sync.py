"""
Markdown Sync: materialize the SQLite user model to ~/lobster-workspace/user-model/.

This is the file layer — a read-only view of the DB for agents to grep/read.
Sync is idempotent and runs during nightly consolidation (and on demand).

File structure:
~/lobster-workspace/user-model/
├── values/
├── principles/
├── preferences/
├── constraints/
├── emotional-baseline.md
├── active-arcs.md
├── patterns.md
├── blind-spots.md
├── contradictions.md
├── attention.md
└── _index.md

Depends on: schema.py, db.py, emotional_model.py, narrative.py, self_knowledge.py, prediction.py.
"""

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from .db import get_all_preference_nodes, get_model_metadata
from .emotional_model import format_emotional_baseline_markdown
from .narrative import format_active_arcs_markdown
from .prediction import format_attention_markdown
from .schema import NodeType
from .self_knowledge import (
    format_blind_spots_markdown,
    format_contradictions_markdown,
    format_patterns_markdown,
)


def _user_model_dir(workspace_path: str | None) -> Path:
    """Return the path to the user-model directory."""
    if workspace_path:
        return Path(workspace_path) / "user-model"
    return Path.home() / "lobster-workspace" / "user-model"


def _ensure_dirs(base: Path) -> None:
    """Create all required subdirectories."""
    for subdir in ["values", "principles", "preferences", "constraints"]:
        (base / subdir).mkdir(parents=True, exist_ok=True)


def _write_file(path: Path, content: str) -> bool:
    """Atomically write a file. Returns True if content changed."""
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return False  # No change

    tmp = path.parent / f".{path.name}.{os.getpid()}.tmp"
    try:
        tmp.write_text(content, encoding="utf-8")
        tmp.rename(path)
        return True
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except Exception:
                pass


def _format_preference_node_markdown(node: Any) -> str:
    """Format a single preference node as a markdown file."""
    contexts_str = ", ".join(node.contexts) if node.contexts else "universal"
    last_observed = node.last_observed.strftime("%Y-%m-%d") if node.last_observed else "unknown"
    node_type = node.node_type.value if hasattr(node.node_type, "value") else node.node_type
    flexibility = node.flexibility.value if hasattr(node.flexibility, "value") else node.flexibility
    source = node.source.value if hasattr(node.source, "value") else node.source

    lines = [
        f"# {node.name}",
        "",
        f"- **Type:** {node_type}",
        f"- **Strength:** {node.strength:.2f}",
        f"- **Flexibility:** {flexibility}",
        f"- **Contexts:** {contexts_str}",
        f"- **Source:** {source}",
        f"- **Confidence:** {node.confidence:.2f}",
        f"- **Last observed:** {last_observed}",
        f"- **Evidence count:** {node.evidence_count}",
        "",
        node.description or "*No description available.*",
    ]
    return "\n".join(lines)


def _slugify(name: str) -> str:
    """Convert a name to a filename-safe slug."""
    import re
    slug = name.lower().strip()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[\s_]+", "-", slug)
    return slug[:60]  # Truncate for filesystem safety


def sync_preference_nodes(
    conn: Any,
    base: Path,
) -> int:
    """Sync all preference nodes to markdown files. Returns count written."""
    type_to_dir = {
        NodeType.VALUE: base / "values",
        NodeType.PRINCIPLE: base / "principles",
        NodeType.PREFERENCE: base / "preferences",
        NodeType.CONSTRAINT: base / "constraints",
    }

    nodes = get_all_preference_nodes(conn, min_confidence=0.3)
    written = 0

    for node in nodes:
        dir_path = type_to_dir.get(node.node_type)
        if not dir_path:
            continue
        filename = _slugify(node.name) + ".md"
        content = _format_preference_node_markdown(node)
        if _write_file(dir_path / filename, content):
            written += 1

    return written


def sync_index(conn: Any, base: Path) -> bool:
    """Write _index.md — a full graph summary."""
    nodes = get_all_preference_nodes(conn)
    meta = get_model_metadata(conn)

    lines = [
        "# User Model Index",
        "",
        f"*Last synced: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}*",
        "",
        "## Model Stats",
        "",
        f"- Schema version: {meta.schema_version}",
        f"- Total observations: {meta.observation_count}",
        f"- Preference nodes: {meta.preference_node_count}",
        f"- Last observation: {meta.last_observation_at.strftime('%Y-%m-%d') if meta.last_observation_at else 'none'}",
        f"- Last consolidation: {meta.last_consolidation_at.strftime('%Y-%m-%d') if meta.last_consolidation_at else 'none'}",
        "",
        "## Graph Summary",
        "",
    ]

    # Group by type
    by_type: dict[str, list] = {}
    for node in nodes:
        t = node.node_type.value if hasattr(node.node_type, "value") else str(node.node_type)
        by_type.setdefault(t, []).append(node)

    for type_name, type_nodes in sorted(by_type.items()):
        lines.append(f"### {type_name.title()}s ({len(type_nodes)})")
        for n in sorted(type_nodes, key=lambda x: -x.strength)[:10]:
            conf = f"{n.confidence:.2f}"
            lines.append(f"- [{n.name}]({type_name}s/{_slugify(n.name)}.md) — strength {n.strength:.2f}, confidence {conf}")
        lines.append("")

    lines.append("## Quick Navigation")
    lines.append("")
    lines.append("- [Emotional Baseline](emotional-baseline.md)")
    lines.append("- [Active Arcs](active-arcs.md)")
    lines.append("- [Life Patterns](patterns.md)")
    lines.append("- [Blind Spots](blind-spots.md)")
    lines.append("- [Contradictions](contradictions.md)")
    lines.append("- [Attention Stack](attention.md)")

    return _write_file(base / "_index.md", "\n".join(lines))


def sync_all(
    conn: Any,
    workspace_path: str | None = None,
) -> dict[str, Any]:
    """
    Full sync: write all markdown files from the DB.
    Returns summary dict.
    """
    base = _user_model_dir(workspace_path)
    _ensure_dirs(base)

    files_written = 0
    errors = []

    # Preference nodes
    try:
        written = sync_preference_nodes(conn, base)
        files_written += written
    except Exception as e:
        errors.append(f"preference_nodes: {e}")

    # Emotional baseline
    try:
        content = format_emotional_baseline_markdown(conn)
        if _write_file(base / "emotional-baseline.md", content):
            files_written += 1
    except Exception as e:
        errors.append(f"emotional_baseline: {e}")

    # Narrative arcs
    try:
        content = format_active_arcs_markdown(conn)
        if _write_file(base / "active-arcs.md", content):
            files_written += 1
    except Exception as e:
        errors.append(f"active_arcs: {e}")

    # Life patterns
    try:
        content = format_patterns_markdown(conn)
        if _write_file(base / "patterns.md", content):
            files_written += 1
    except Exception as e:
        errors.append(f"patterns: {e}")

    # Blind spots
    try:
        content = format_blind_spots_markdown(conn)
        if _write_file(base / "blind-spots.md", content):
            files_written += 1
    except Exception as e:
        errors.append(f"blind_spots: {e}")

    # Contradictions
    try:
        content = format_contradictions_markdown(conn)
        if _write_file(base / "contradictions.md", content):
            files_written += 1
    except Exception as e:
        errors.append(f"contradictions: {e}")

    # Attention stack
    try:
        content = format_attention_markdown(conn)
        if _write_file(base / "attention.md", content):
            files_written += 1
    except Exception as e:
        errors.append(f"attention: {e}")

    # Index
    try:
        if sync_index(conn, base):
            files_written += 1
    except Exception as e:
        errors.append(f"index: {e}")

    return {
        "base_dir": str(base),
        "files_written": files_written,
        "errors": errors,
        "synced_at": datetime.utcnow().isoformat(),
    }


def detect_user_edits(
    conn: Any,
    workspace_path: str | None = None,
) -> list[dict[str, Any]]:
    """
    Detect user edits to markdown files by comparing mtime to last_consolidation_at.
    Returns list of edited files with their paths.

    User edits to preference files are pulled back into the DB as corrections
    during the next consolidation pass.
    """
    from .db import get_model_metadata
    meta = get_model_metadata(conn)
    last_sync = meta.last_consolidation_at

    base = _user_model_dir(workspace_path)
    if not base.exists():
        return []

    edits = []
    for md_file in base.rglob("*.md"):
        if md_file.name.startswith("_"):
            continue  # Skip index files
        mtime = datetime.utcfromtimestamp(md_file.stat().st_mtime)
        if last_sync and mtime > last_sync:
            edits.append({
                "path": str(md_file),
                "relative": str(md_file.relative_to(base)),
                "mtime": mtime.isoformat(),
            })

    return edits
