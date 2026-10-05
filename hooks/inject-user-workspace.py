#!/usr/bin/env python3
"""PreToolUse hook (matcher: Agent): inject per-user workspace rules into subagent
prompts and enforce the workspace's model policy.

Why: standing per-user rules (fonts, colors, "box" protocol, Opus-for-decks)
lived only in the dispatcher's memory and were routinely dropped when the
dispatcher composed subagent prompts. This hook makes the injection mechanical.

## Design decision (verified 2026-10-05 on Claude Code 2.1.289)

Default mode is ``rewrite`` (design A). A live nested ``claude -p`` test with an
isolated project settings.json proved that a PreToolUse hook returning
``hookSpecificOutput.updatedInput`` rewrites the Agent tool input:
  - prompt: the subagent's first user message contained the injected token;
  - model: a call requesting ``model: haiku`` ran as ``claude-opus-5-5`` after the
    hook rewrote ``model`` to ``opus`` (confirmed in the subagent transcript).
``block`` mode (design B) is kept as a fallback for harness versions that ignore
updatedInput: it exits 2 and tells the dispatcher what the prompt is missing.

Mode: env ``LOBSTER_WORKSPACE_INJECT_MODE=rewrite|block`` (default ``rewrite``).

## Inputs

Workspaces: ``$LOBSTER_WORKSPACES_DIR`` (default ``~/lobster-user-config/workspaces``),
one dir per workspace containing ``workspace.json``::

    {"slug": "alex-rivera", "chat_ids": ["C0TEST00001", "1000000001"],
     "rules_file": "RULES.md", "box_state_file": "box-state.json",
     "model_for": {"deck": "opus", ...},
     "model_trigger_keywords": ["deck", "slide", ...]}

``box-state.json``: ``{"active_box": "...", "since": "..."}``. If
``boxes/<active_box>.md`` exists it is appended to the injected block.

The prompt's ``chat_id`` is read from YAML frontmatter (same format accepted by
require-task-id-in-prompt.py). Prompts without frontmatter pass through.

## Behaviour

- Injection block, inserted right after the frontmatter's closing ``---``::

      <!-- workspace-rules: <slug> -->
      ## Active box: <active_box> (since <since>)
      <RULES.md>
      <!-- /workspace-rules -->

  Idempotent: skipped when ``<!-- workspace-rules: <slug> -->`` is already present.
- Model: if prompt+description matches any trigger keyword (word boundary,
  optional plural "s"), ``model_for`` is non-empty, current model does not start
  with "opus" (missing counts as not-opus), and no ``model_override_reason: <x>``
  line exists in the prompt -> model is set to "opus".
- Log: one line per matched call to ``$LOBSTER_WORKSPACE_INJECT_LOG``
  (default ``~/lobster-workspace/logs/workspace-inject.log``).
- Never crashes: any error -> warning on stderr, exit 0 (call proceeds unchanged).

Rollback: remove the inject-user-workspace.py entry from the PreToolUse
``Agent`` hooks in ~/.claude/settings.json.
"""
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

AGENT_TOOLS = ("Agent", "Task")
OPUS = "opus"


def _workspaces_dir() -> Path:
    return Path(os.environ.get(
        "LOBSTER_WORKSPACES_DIR",
        str(Path.home() / "lobster-user-config" / "workspaces"),
    ))


def _log_path() -> Path:
    return Path(os.environ.get(
        "LOBSTER_WORKSPACE_INJECT_LOG",
        str(Path.home() / "lobster-workspace" / "logs" / "workspace-inject.log"),
    ))


def _mode() -> str:
    mode = os.environ.get("LOBSTER_WORKSPACE_INJECT_MODE", "rewrite").strip().lower()
    return mode if mode in ("rewrite", "block") else "rewrite"


def _warn(msg: str) -> None:
    print(f"Warning: inject-user-workspace: {msg}", file=sys.stderr)


# --------------------------------------------------------------------------- parsing

def split_frontmatter(prompt: str):
    """Return (frontmatter_text_incl_delimiters, body, fields) or None if absent.

    frontmatter_text ends right after the closing '---' line (including its newline).
    """
    stripped = prompt.lstrip()
    lead = prompt[: len(prompt) - len(stripped)]
    if not stripped.startswith("---"):
        return None
    rest = stripped[3:]
    end = rest.find("\n---")
    if end == -1:
        return None
    block = rest[:end]
    close_end = 3 + end + len("\n---")
    nl = stripped.find("\n", close_end)
    close_end = len(stripped) if nl == -1 else nl + 1
    fields = {}
    for line in block.splitlines():
        key, sep, value = line.strip().partition(":")
        if sep and key.strip():
            fields[key.strip()] = value.strip().strip("'\"")
    return lead + stripped[:close_end], stripped[close_end:], fields


def find_workspace(chat_id: str, root: Path):
    """Return (workspace_dir, config) whose chat_ids contains chat_id, else None."""
    if not root.is_dir():
        return None
    for ws_file in sorted(root.glob("*/workspace.json")):
        try:
            cfg = json.loads(ws_file.read_text())
        except Exception as e:  # bad JSON in one workspace must not break others
            _warn(f"cannot read {ws_file}: {e}")
            continue
        if chat_id in [str(c) for c in cfg.get("chat_ids", [])]:
            return ws_file.parent, cfg
    return None


def _read_text(path: Path) -> str:
    try:
        return path.read_text()
    except Exception as e:
        _warn(f"cannot read {path}: {e}")
        return ""


def build_block(ws_dir: Path, cfg: dict, slug: str) -> str:
    box = {}
    box_path = ws_dir / cfg.get("box_state_file", "box-state.json")
    if box_path.exists():
        try:
            box = json.loads(box_path.read_text())
        except Exception as e:
            _warn(f"bad box state {box_path}: {e}")
    active = box.get("active_box") or "none"
    since = box.get("since") or "unknown"
    rules = _read_text(ws_dir / cfg.get("rules_file", "RULES.md")).rstrip()
    parts = [f"<!-- workspace-rules: {slug} -->", f"## Active box: {active} (since {since})",
             f"Workspace files (relative paths below resolve here): {ws_dir}"]
    box_file = ws_dir / "boxes" / f"{active}.md"
    if active != "none" and box_file.exists():
        parts.append(_read_text(box_file).rstrip())
    parts.append(rules)
    parts.append("<!-- /workspace-rules -->")
    return "\n".join(p for p in parts if p) + "\n"


def wants_opus(cfg: dict, prompt: str, description: str, model) -> bool:
    if not cfg.get("model_for"):
        return False
    if str(model or "").lower().startswith(OPUS):
        return False
    if re.search(r"(?m)^\s*model_override_reason:\s*\S", prompt):
        return False
    text = f"{prompt}\n{description}".lower()
    keywords = [k for k in cfg.get("model_trigger_keywords", []) if str(k).strip()]
    return any(re.search(rf"\b{re.escape(str(k).lower())}s?\b", text) for k in keywords)


def plan(tool_input: dict, root: Path):
    """Pure-ish core: return None (no workspace) or a dict describing changes."""
    prompt = tool_input.get("prompt") or ""
    fm = split_frontmatter(prompt)
    if fm is None:
        return None
    head, body, fields = fm
    chat_id = fields.get("chat_id", "")
    if not chat_id:
        return None
    found = find_workspace(chat_id, root)
    if found is None:
        return None
    ws_dir, cfg = found
    slug = cfg.get("slug") or ws_dir.name
    marker = f"<!-- workspace-rules: {slug} -->"
    already = marker in prompt
    new_prompt = prompt if already else head + build_block(ws_dir, cfg, slug) + body
    old_model = tool_input.get("model")
    upgrade = wants_opus(cfg, prompt, tool_input.get("description") or "", old_model)
    return {
        "slug": slug,
        "chat_id": chat_id,
        "marker": marker,
        "injected": not already,
        "prompt": new_prompt,
        "old_model": old_model,
        "new_model": OPUS if upgrade else old_model,
    }


def _log(p: dict) -> None:
    try:
        path = _log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        line = (f"{ts} | {p['slug']} | {p['chat_id']} | injected={'yes' if p['injected'] else 'no'}"
                f" | model: {p['old_model'] or 'default'}->{p['new_model'] or 'default'}\n")
        with path.open("a") as f:
            f.write(line)
    except Exception as e:
        _warn(f"cannot write log: {e}")


def main() -> int:
    try:
        data = json.load(sys.stdin)
        if data.get("tool_name") not in AGENT_TOOLS:
            return 0
        tool_input = data.get("tool_input") or {}
        if not isinstance(tool_input, dict):
            return 0
        p = plan(tool_input, _workspaces_dir())
    except Exception as e:
        _warn(f"skipped due to error: {e}")
        return 0
    if p is None:
        return 0

    model_change = p["new_model"] != p["old_model"]
    if not p["injected"] and not model_change:
        _log(p)
        return 0

    if _mode() == "block":
        missing = []
        if p["injected"]:
            missing.append(f"prompt must contain {p['marker']} block with RULES.md and active box "
                           f"(insert right after the frontmatter)")
        if model_change:
            missing.append("model must be opus for deck/doc work "
                           "(or add a 'model_override_reason: <why>' line)")
        print(f"BLOCKED: workspace '{p['slug']}' (chat_id {p['chat_id']}): " + "; ".join(missing),
              file=sys.stderr)
        return 2

    updated = dict(tool_input)
    updated["prompt"] = p["prompt"]
    if model_change:
        updated["model"] = p["new_model"]
    _log(p)
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "allow",
        "permissionDecisionReason": f"workspace rules injected for {p['slug']}",
        "updatedInput": updated,
    }}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
