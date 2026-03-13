"""
Bridges: connect the user model to canonical memory, projects, and priorities.

Reads workspace files (priorities.md, projects/*.md, people/*.md) and syncs
them into the user model as narrative arcs, attention items, and context.
Runs during nightly consolidation only — never on the hot message path.

Also writes a pre-computed _context.md for fast reads by the dispatcher.

Depends on: schema.py, db.py, narrative.py, prediction.py only.
"""

import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from .db import (
    get_active_narrative_arcs,
    get_all_preference_nodes,
    get_attention_stack,
    get_emotional_baseline,
    get_model_metadata,
    upsert_attention_item,
    upsert_narrative_arc,
)
from .schema import (
    AttentionCategory,
    AttentionItem,
    NarrativeArc,
)


# ---------------------------------------------------------------------------
# Canonical memory readers
# ---------------------------------------------------------------------------

def _read_file_safe(path: Path) -> str:
    """Read a file, returning empty string on any error."""
    try:
        if path.exists():
            return path.read_text(encoding="utf-8")
    except Exception:
        pass
    return ""


def _parse_priority_items(content: str) -> list[dict[str, Any]]:
    """
    Parse priorities.md into structured items.
    Expects markdown with numbered items and descriptions.
    """
    items = []
    current: dict[str, Any] | None = None

    for line in content.splitlines():
        # Match numbered priority: "1. **Project Name** — description"
        # or "- **Item** — description"
        m = re.match(r'^\s*(?:\d+\.|-)\s+\*\*(.+?)\*\*\s*(?:—|:|-)\s*(.*)', line)
        if m:
            if current:
                items.append(current)
            current = {
                "title": m.group(1).strip(),
                "description": m.group(2).strip(),
                "details": [],
            }
            continue

        # Indented continuation lines
        if current and line.strip().startswith("-"):
            current["details"].append(line.strip().lstrip("- "))

    if current:
        items.append(current)

    return items


def _parse_project_file(content: str, filename: str) -> dict[str, Any]:
    """Parse a project status markdown file into structured data."""
    result: dict[str, Any] = {
        "name": filename.replace(".md", "").replace("-", " ").title(),
        "status": "active",
        "description": "",
        "themes": [],
    }

    lines = content.splitlines()
    for i, line in enumerate(lines):
        # Title from first heading
        if line.startswith("# ") and not result.get("_has_title"):
            result["name"] = line.lstrip("# ").strip()
            result["_has_title"] = True
            continue

        # Status line
        lower = line.lower()
        if "status:" in lower:
            status_text = line.split(":", 1)[1].strip().lower()
            if any(w in status_text for w in ["active", "in progress", "ongoing"]):
                result["status"] = "active"
            elif any(w in status_text for w in ["paused", "blocked", "stalled"]):
                result["status"] = "paused"
            elif any(w in status_text for w in ["done", "complete", "finished"]):
                result["status"] = "resolved"

        # Description from first non-heading paragraph
        if not result["description"] and line.strip() and not line.startswith("#"):
            result["description"] = line.strip()

    result.pop("_has_title", None)
    return result


# ---------------------------------------------------------------------------
# Arc sync: projects → narrative arcs
# ---------------------------------------------------------------------------

def sync_projects_to_arcs(
    conn: sqlite3.Connection,
    workspace_path: str | None = None,
) -> dict[str, Any]:
    """
    Read project files from canonical memory and sync them as narrative arcs.
    Creates new arcs for new projects, updates existing ones.
    Returns summary of changes.
    """
    ws = Path(workspace_path) if workspace_path else Path.home() / "lobster-workspace"
    projects_dir = ws / "memory" / "canonical" / "projects"

    if not projects_dir.exists():
        return {"synced": 0, "created": 0, "updated": 0}

    # Index existing arcs by title for dedup
    existing_arcs = get_active_narrative_arcs(conn)
    arc_by_title: dict[str, NarrativeArc] = {}
    for arc in existing_arcs:
        arc_by_title[arc.title.lower()] = arc

    created = 0
    updated = 0

    for md_file in sorted(projects_dir.glob("*.md")):
        content = _read_file_safe(md_file)
        if not content.strip():
            continue

        proj = _parse_project_file(content, md_file.name)
        title = proj["name"]
        key = title.lower()

        if key in arc_by_title:
            # Update existing arc
            existing = arc_by_title[key]
            arc = NarrativeArc(
                id=existing.id,
                title=title,
                description=proj["description"] or existing.description,
                themes=proj.get("themes") or existing.themes,
                status=proj["status"],
                started_at=existing.started_at,
                last_updated=datetime.utcnow(),
                resolution=existing.resolution,
            )
            upsert_narrative_arc(conn, arc)
            updated += 1
        else:
            # Create new arc from project
            arc = NarrativeArc(
                id=None,
                title=title,
                description=proj["description"],
                themes=proj.get("themes", []),
                status=proj["status"],
                started_at=datetime.utcnow(),
                last_updated=datetime.utcnow(),
            )
            upsert_narrative_arc(conn, arc)
            created += 1

    return {"synced": created + updated, "created": created, "updated": updated}


# ---------------------------------------------------------------------------
# Priority sync: priorities.md → attention items
# ---------------------------------------------------------------------------

def sync_priorities_to_attention(
    conn: sqlite3.Connection,
    workspace_path: str | None = None,
) -> dict[str, Any]:
    """
    Read priorities.md and inject top items into the attention stack.
    These supplement (not replace) the organic attention items from observations.
    """
    ws = Path(workspace_path) if workspace_path else Path.home() / "lobster-workspace"
    priorities_file = ws / "memory" / "canonical" / "priorities.md"

    content = _read_file_safe(priorities_file)
    if not content.strip():
        return {"injected": 0}

    items = _parse_priority_items(content)
    injected = 0

    for rank, item in enumerate(items[:5]):  # Top 5 only
        urgency = max(0.3, 1.0 - rank * 0.15)
        importance = max(0.4, 1.0 - rank * 0.1)
        score = 0.35 * urgency + 0.35 * importance + 0.20 * 0.7 + 0.10 * 0.8

        att = AttentionItem(
            id=None,
            title=item["title"],
            description=item["description"][:200],
            category=AttentionCategory.IMPORTANT if rank < 3 else AttentionCategory.MONITORING,
            score=min(1.0, score),
            context="priorities",
            source="canonical_priorities",
            metadata={"rank": rank + 1},
            created_at=datetime.utcnow(),
        )
        upsert_attention_item(conn, att)
        injected += 1

    return {"injected": injected}


# ---------------------------------------------------------------------------
# Context cache: pre-compute a compact context file for fast dispatcher reads
# ---------------------------------------------------------------------------

def write_context_cache(
    conn: sqlite3.Connection,
    workspace_path: str | None = None,
) -> str:
    """
    Write a pre-computed _context.md with the user's current state.
    The dispatcher reads this at session start instead of querying the DB.
    Returns the content written.
    """
    ws = Path(workspace_path) if workspace_path else Path.home() / "lobster-workspace"
    um_dir = ws / "user-model"
    um_dir.mkdir(parents=True, exist_ok=True)

    sections = []

    # Active preferences (high confidence)
    try:
        nodes = get_all_preference_nodes(conn, min_confidence=0.6)
        values = [n for n in nodes if n.node_type.value == "value" and n.confidence >= 0.7]
        prefs = [n for n in nodes if n.node_type.value == "preference" and n.confidence >= 0.6]
        constraints = [n for n in nodes if n.node_type.value == "constraint"]

        if values:
            sections.append("## Core Values")
            for v in sorted(values, key=lambda x: -x.strength)[:5]:
                sections.append(f"- **{v.name}** ({v.strength:.0%} strength)")

        if constraints:
            sections.append("\n## Hard Constraints")
            for c in constraints[:5]:
                sections.append(f"- {c.name}: {c.description[:80]}")

        if prefs:
            sections.append("\n## Active Preferences")
            for p in sorted(prefs, key=lambda x: -x.strength)[:7]:
                sections.append(f"- {p.name} ({p.source.value}, {p.confidence:.0%} confidence)")
    except Exception:
        pass

    # Emotional baseline
    try:
        baseline = get_emotional_baseline(conn, days=30)
        if baseline:
            v, a, d = baseline
            mood = "positive" if v > 0.2 else "negative" if v < -0.2 else "neutral"
            energy = "high" if a > 0.6 else "low" if a < 0.4 else "moderate"
            sections.append(f"\n## Current State")
            sections.append(f"- Mood baseline: {mood} (valence {v:+.2f})")
            sections.append(f"- Energy: {energy} (arousal {a:.2f})")
    except Exception:
        pass

    # Active arcs (projects)
    try:
        arcs = get_active_narrative_arcs(conn)
        if arcs:
            sections.append("\n## Active Projects/Arcs")
            for arc in arcs[:5]:
                sections.append(f"- **{arc.title}** ({arc.status})")
    except Exception:
        pass

    # Top attention items
    try:
        stack = get_attention_stack(conn, limit=5)
        if stack:
            sections.append("\n## Attention Stack (Top 5)")
            for item in stack:
                sections.append(f"- [{item.category.value}] {item.title} (score {item.score:.2f})")
    except Exception:
        pass

    if not sections:
        content = "# User Model Context\n\n*No data yet — model is still learning.*\n"
    else:
        header = "# User Model Context\n"
        header += f"*Auto-generated {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')} — do not edit*\n"
        content = header + "\n".join(sections) + "\n"

    # Atomic write
    out_path = um_dir / "_context.md"
    tmp = out_path.parent / f".{out_path.name}.tmp"
    try:
        tmp.write_text(content, encoding="utf-8")
        tmp.rename(out_path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except Exception:
                pass

    return content


# ---------------------------------------------------------------------------
# Full bridge pass (called from consolidation)
# ---------------------------------------------------------------------------

def run_bridges(
    conn: sqlite3.Connection,
    workspace_path: str | None = None,
) -> dict[str, Any]:
    """
    Run all bridge operations. Called during nightly consolidation.
    Returns combined summary.
    """
    summary: dict[str, Any] = {}

    try:
        summary["projects"] = sync_projects_to_arcs(conn, workspace_path)
    except Exception as e:
        summary["projects"] = {"error": str(e)}

    try:
        summary["priorities"] = sync_priorities_to_attention(conn, workspace_path)
    except Exception as e:
        summary["priorities"] = {"error": str(e)}

    try:
        write_context_cache(conn, workspace_path)
        summary["context_cache"] = "written"
    except Exception as e:
        summary["context_cache"] = {"error": str(e)}

    return summary
