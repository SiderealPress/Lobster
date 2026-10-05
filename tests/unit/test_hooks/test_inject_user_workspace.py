"""
Unit tests for hooks/inject-user-workspace.py

The hook is run as a subprocess with real PreToolUse JSON on stdin, against a
temporary workspaces dir (LOBSTER_WORKSPACES_DIR) and log file
(LOBSTER_WORKSPACE_INJECT_LOG).
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HOOK_PATH = Path(__file__).parents[3] / "hooks" / "inject-user-workspace.py"

CHAT_ID = "C0TEST00001"
MARKER = "<!-- workspace-rules: alex-rivera -->"
RULES = "# Alex rules\n1. Never use Arial.\n2. Montserrat headlines.\n"


@pytest.fixture
def ws_root(tmp_path: Path) -> Path:
    root = tmp_path / "workspaces"
    ws = root / "alex-rivera"
    (ws / "boxes").mkdir(parents=True)
    (ws / "workspace.json").write_text(json.dumps({
        "slug": "alex-rivera",
        "chat_ids": [CHAT_ID, "1000000001"],
        "rules_file": "RULES.md",
        "box_state_file": "box-state.json",
        "model_for": {"deck": "opus", "doc": "opus", "proposal": "opus"},
        "model_trigger_keywords": ["deck", "slide", "pptx", "doc", "proposal"],
    }))
    (ws / "RULES.md").write_text(RULES)
    (ws / "box-state.json").write_text(json.dumps(
        {"active_box": "acme", "since": "2026-10-05T22:27:00Z"}))
    (ws / "boxes" / "acme.md").write_text("# Box: Acme\nstatus: active\n")
    # A second, unrelated workspace with malformed JSON must not break lookup.
    (root / "broken").mkdir()
    (root / "broken" / "workspace.json").write_text("{not json")
    return root


def _prompt(body: str, chat_id: str = CHAT_ID, extra: str = "") -> str:
    return f"---\ntask_id: t1\nchat_id: {chat_id}\nsource: slack\n{extra}---\n{body}"


def run_hook(tool_input: dict, ws_root: Path, tmp_path: Path, mode: str | None = None,
             tool_name: str = "Agent", raw: str | None = None):
    env = dict(os.environ)
    env["LOBSTER_WORKSPACES_DIR"] = str(ws_root)
    env["LOBSTER_WORKSPACE_INJECT_LOG"] = str(tmp_path / "inject.log")
    env.pop("LOBSTER_WORKSPACE_INJECT_MODE", None)
    if mode:
        env["LOBSTER_WORKSPACE_INJECT_MODE"] = mode
    stdin = raw if raw is not None else json.dumps({
        "hook_event_name": "PreToolUse",
        "session_id": "sess-test",
        "tool_name": tool_name,
        "tool_input": tool_input,
    })
    proc = subprocess.run([sys.executable, str(HOOK_PATH)], input=stdin,
                          capture_output=True, text=True, env=env, timeout=30)
    out = json.loads(proc.stdout) if proc.stdout.strip() else None
    return proc.returncode, out, proc.stderr


def _updated(out: dict) -> dict:
    hso = out["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    assert hso["permissionDecision"] == "allow"
    return hso["updatedInput"]


def test_non_matching_chat_id_passes_through(ws_root, tmp_path):
    rc, out, _ = run_hook({"prompt": _prompt("Build a deck", chat_id="999"),
                           "description": "deck", "subagent_type": "general-purpose"},
                          ws_root, tmp_path)
    assert rc == 0 and out is None
    assert not (tmp_path / "inject.log").exists()


def test_matching_chat_id_injects_after_frontmatter(ws_root, tmp_path):
    ti = {"prompt": _prompt("Check the calendar."), "description": "calendar",
          "subagent_type": "general-purpose", "run_in_background": True}
    rc, out, _ = run_hook(ti, ws_root, tmp_path)
    assert rc == 0
    upd = _updated(out)
    p = upd["prompt"]
    assert p.startswith("---\ntask_id: t1\nchat_id: C0TEST00001\nsource: slack\n---\n" + MARKER)
    assert "## Active box: acme (since 2026-10-05T22:27:00Z)" in p
    assert f"Workspace files (relative paths below resolve here): {ws_root / 'alex-rivera'}" in p
    assert "# Box: Acme" in p
    assert RULES.strip() in p
    assert p.index("<!-- /workspace-rules -->") < p.index("Check the calendar.")
    assert p.endswith("Check the calendar.")
    # Other fields preserved, model untouched (no keyword).
    assert upd["subagent_type"] == "general-purpose" and upd["run_in_background"] is True
    assert "model" not in upd
    log = (tmp_path / "inject.log").read_text()
    assert "| alex-rivera | C0TEST00001 | injected=yes | model: default->default" in log


def test_idempotent_when_marker_present(ws_root, tmp_path):
    rc, out, _ = run_hook({"prompt": _prompt("Check calendar")}, ws_root, tmp_path)
    once = _updated(out)
    rc, out2, _ = run_hook(once, ws_root, tmp_path)
    assert rc == 0 and out2 is None  # nothing to change
    assert once["prompt"].count(MARKER) == 1
    assert "injected=no" in (tmp_path / "inject.log").read_text()


def test_model_upgraded_to_opus_on_deck_keyword(ws_root, tmp_path):
    rc, out, _ = run_hook({"prompt": _prompt("Build the Acme proposal deck"),
                           "description": "acme build", "model": "sonnet"},
                          ws_root, tmp_path)
    upd = _updated(out)
    assert upd["model"] == "opus"
    assert "model: sonnet->opus" in (tmp_path / "inject.log").read_text()


def test_missing_model_upgraded_when_keyword_in_description(ws_root, tmp_path):
    rc, out, _ = run_hook({"prompt": _prompt("Do the thing"), "description": "fix slides"},
                          ws_root, tmp_path)
    assert _updated(out)["model"] == "opus"


def test_model_untouched_without_keyword(ws_root, tmp_path):
    rc, out, _ = run_hook({"prompt": _prompt("Summarize the call notes"), "model": "sonnet"},
                          ws_root, tmp_path)
    assert _updated(out)["model"] == "sonnet"


def test_keyword_word_boundary(ws_root, tmp_path):
    # "docker" must not match "doc"; "dock" neither.
    rc, out, _ = run_hook({"prompt": _prompt("Restart docker at the dock"), "model": "sonnet"},
                          ws_root, tmp_path)
    assert _updated(out)["model"] == "sonnet"


def test_existing_opus_variant_kept(ws_root, tmp_path):
    rc, out, _ = run_hook({"prompt": _prompt("Build deck"), "model": "opus[1m]"},
                          ws_root, tmp_path)
    assert _updated(out)["model"] == "opus[1m]"


def test_override_reason_respected(ws_root, tmp_path):
    prompt = _prompt("model_override_reason: tiny typo fix\nFix a typo in the deck")
    rc, out, _ = run_hook({"prompt": prompt, "model": "haiku"}, ws_root, tmp_path)
    assert _updated(out)["model"] == "haiku"


def test_legacy_prompt_without_frontmatter_passes(ws_root, tmp_path):
    rc, out, _ = run_hook({"prompt": "Your task_id is: x\nchat_id C0TEST00001 build deck"},
                          ws_root, tmp_path)
    assert rc == 0 and out is None


def test_missing_workspaces_dir_passes(tmp_path):
    rc, out, err = run_hook({"prompt": _prompt("Build deck")}, tmp_path / "nope", tmp_path)
    assert rc == 0 and out is None


def test_non_agent_tool_ignored(ws_root, tmp_path):
    rc, out, _ = run_hook({"command": "echo hi"}, ws_root, tmp_path, tool_name="Bash")
    assert rc == 0 and out is None


def test_malformed_stdin_does_not_crash(ws_root, tmp_path):
    rc, out, err = run_hook({}, ws_root, tmp_path, raw="{garbage")
    assert rc == 0 and out is None
    assert "Warning" in err


def test_missing_rules_and_box_files_still_inject(ws_root, tmp_path):
    ws = ws_root / "alex-rivera"
    (ws / "RULES.md").unlink()
    (ws / "box-state.json").unlink()
    rc, out, err = run_hook({"prompt": _prompt("hello")}, ws_root, tmp_path)
    p = _updated(out)["prompt"]
    assert MARKER in p and "## Active box: none (since unknown)" in p
    assert "Warning" in err


def test_block_mode_exits_2_with_message(ws_root, tmp_path):
    rc, out, err = run_hook({"prompt": _prompt("Build the deck"), "model": "sonnet"},
                            ws_root, tmp_path, mode="block")
    assert rc == 2 and out is None
    assert MARKER in err and "RULES.md" in err and "model must be opus" in err


def test_block_mode_allows_compliant_prompt(ws_root, tmp_path):
    _, out, _ = run_hook({"prompt": _prompt("Build the deck"), "model": "sonnet"},
                         ws_root, tmp_path)
    compliant = _updated(out)
    rc, out2, err = run_hook(compliant, ws_root, tmp_path, mode="block")
    assert rc == 0 and out2 is None
