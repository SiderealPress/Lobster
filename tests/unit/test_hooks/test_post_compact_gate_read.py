"""
Unit tests for the Read always-allow path in hooks/post-compact-gate.py.

When a Read tool call is detected, the gate must return exit 0 and allow
the call through — even if the compact-pending sentinel is fresh and the gate
would otherwise block.

This path is critical to avoid a circular dependency (issue #1950):
the gate blocked Read, but the dispatcher needed to read the bootup file
(sys.dispatcher.bootup.md) to learn the confirmation token. Blocking Read
before the token was known forced a two-step dance: fail WFM once to get the
token in the error message, then call WFM again with the token. Whitelisting
Read breaks this cycle — the token can now live only in the bootup file,
which is readable through the gate on the first attempt.
"""

import importlib.util
import json
import sys
from io import StringIO
from pathlib import Path
from unittest.mock import patch

_HOOKS_DIR = Path(__file__).parents[3] / "hooks"
_HOOK_PATH = _HOOKS_DIR / "post-compact-gate.py"

# Relative path (from HOME) where the hook looks for the sentinel.
SENTINEL_REL = Path("messages") / "config" / "compact-pending"

READ_TOOL = "Read"


def _make_sentinel(home: Path) -> Path:
    """Create a fresh sentinel file at the expected location under home."""
    sentinel = home / SENTINEL_REL
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.touch()
    return sentinel


def _load_hook(monkeypatch, tmp_path: Path):
    """Load post-compact-gate.py with HOME and LOBSTER_MAIN_SESSION overridden."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LOBSTER_MAIN_SESSION", "1")

    spec = importlib.util.spec_from_file_location("post_compact_gate", _HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    if str(_HOOKS_DIR) not in sys.path:
        sys.path.insert(0, str(_HOOKS_DIR))
    spec.loader.exec_module(mod)
    return mod


def _make_hook_input(
    tool_name: str,
    tool_input: dict | None = None,
    agent_id: str | None = None,
) -> dict:
    payload = {"tool_name": tool_name, "tool_input": tool_input or {}}
    if agent_id is not None:
        payload["agent_id"] = agent_id
    return payload


def _run_hook(mod, hook_input: dict) -> tuple[int, str, str]:
    """Run mod.main() with hook_input as stdin. Returns (exit_code, stdout, stderr)."""
    stdout_cap = StringIO()
    stderr_cap = StringIO()
    stdin_data = json.dumps(hook_input)
    exit_code = None

    with (
        patch("sys.stdin", StringIO(stdin_data)),
        patch("sys.stdout", stdout_cap),
        patch("sys.stderr", stderr_cap),
    ):
        try:
            mod.main()
        except SystemExit as e:
            exit_code = e.code

    return exit_code, stdout_cap.getvalue(), stderr_cap.getvalue()


def _make_dispatcher_session_file(tmp_path: Path, session_id: str) -> Path:
    """Write the hook marker file so is_dispatcher_session returns True."""
    config_dir = tmp_path / "messages" / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    marker = config_dir / "dispatcher-session-id"
    marker.write_text(session_id)
    return marker


# ---------------------------------------------------------------------------
# Read always-allow path (issue #1950)
# ---------------------------------------------------------------------------


class TestReadAlwaysAllow:
    """Read is allowed through the gate even when sentinel is fresh.

    Failure mode: if this path is missing, the dispatcher is caught in a
    circular dependency after compaction — the gate blocks reads, but the
    dispatcher needs to read the bootup file to discover the confirmation token.
    Whitelisting Read breaks this cycle.
    """

    def test_read_allowed_when_sentinel_fresh(self, monkeypatch, tmp_path):
        """Read exits 0 with no deny output even if compact-pending is fresh.

        This is the core regression test for issue #1950. A fresh sentinel
        causes the gate to block any other tool, but Read must pass through
        unconditionally because it is non-destructive.
        """
        _make_sentinel(tmp_path)
        _make_dispatcher_session_file(tmp_path, "sess-disp-read")

        mod = _load_hook(monkeypatch, tmp_path)
        mod.SENTINEL_FILE = tmp_path / SENTINEL_REL

        import session_role
        monkeypatch.setattr(
            session_role, "DISPATCHER_SESSION_FILE",
            tmp_path / "messages" / "config" / "dispatcher-session-id",
        )

        hook_input = _make_hook_input(
            tool_name=READ_TOOL,
            tool_input={"file_path": "/home/lobster/lobster-workspace/.claude/sys.dispatcher.bootup.md"},
        )
        exit_code, stdout, stderr = _run_hook(mod, hook_input)

        assert exit_code == 0, (
            f"Read should exit 0 even with fresh sentinel, got {exit_code}. "
            f"stderr: {stderr!r}"
        )
        assert stdout.strip() == "", (
            f"Read should produce no deny output (empty stdout), got: {stdout!r}"
        )

    def test_read_allowed_and_sentinel_remains(self, monkeypatch, tmp_path):
        """Read does not delete the sentinel — it only passes through.

        After Read is allowed, the sentinel must still be present so that
        subsequent non-Read / non-ToolSearch / non-wait_for_messages calls
        are still blocked.
        """
        sentinel = _make_sentinel(tmp_path)
        _make_dispatcher_session_file(tmp_path, "sess-disp-read-nodel")

        mod = _load_hook(monkeypatch, tmp_path)
        mod.SENTINEL_FILE = tmp_path / SENTINEL_REL

        import session_role
        monkeypatch.setattr(
            session_role, "DISPATCHER_SESSION_FILE",
            tmp_path / "messages" / "config" / "dispatcher-session-id",
        )

        hook_input = _make_hook_input(
            tool_name=READ_TOOL,
            tool_input={"file_path": "/home/lobster/lobster-workspace/.claude/sys.dispatcher.bootup.md"},
        )
        _run_hook(mod, hook_input)

        assert sentinel.exists(), (
            "Sentinel must NOT be deleted by a Read call — only wait_for_messages "
            "with the confirmation token clears the sentinel."
        )

    def test_read_allowed_any_file_path(self, monkeypatch, tmp_path):
        """Read is allowed for any file path, not just specific bootup files.

        The whitelist is unconditional: all Read calls pass through regardless
        of the file being read. Reads are inherently non-destructive.
        """
        _make_sentinel(tmp_path)
        _make_dispatcher_session_file(tmp_path, "sess-disp-read-anypath")

        mod = _load_hook(monkeypatch, tmp_path)
        mod.SENTINEL_FILE = tmp_path / SENTINEL_REL

        import session_role
        monkeypatch.setattr(
            session_role, "DISPATCHER_SESSION_FILE",
            tmp_path / "messages" / "config" / "dispatcher-session-id",
        )

        for file_path in [
            "/etc/hostname",
            "/home/lobster/lobster-workspace/CLAUDE.md",
            "/tmp/some-random-file.txt",
        ]:
            hook_input = _make_hook_input(
                tool_name=READ_TOOL,
                tool_input={"file_path": file_path},
            )
            exit_code, stdout, _ = _run_hook(mod, hook_input)
            assert exit_code == 0, f"Read({file_path!r}) should exit 0 with fresh sentinel"
            assert stdout.strip() == "", f"Read({file_path!r}) should produce no deny output"

    def test_write_still_blocked_when_sentinel_fresh(self, monkeypatch, tmp_path):
        """Confirm that Write (a state-mutating tool) is still blocked with a fresh sentinel.

        This is a sanity check that the Read allow-path does not accidentally
        disable the gate for all file-related tools. Write can modify state,
        so it must remain blocked until WFM clears the sentinel.
        """
        _make_sentinel(tmp_path)
        _make_dispatcher_session_file(tmp_path, "sess-disp-read-sanity")

        mod = _load_hook(monkeypatch, tmp_path)
        mod.SENTINEL_FILE = tmp_path / SENTINEL_REL

        import session_role
        monkeypatch.setattr(
            session_role, "DISPATCHER_SESSION_FILE",
            tmp_path / "messages" / "config" / "dispatcher-session-id",
        )

        hook_input = _make_hook_input(tool_name="Write")
        exit_code, stdout, stderr = _run_hook(mod, hook_input)

        assert exit_code == 0, f"Hook exited non-zero: {stderr}"
        assert stdout.strip() != "", (
            "Write tool should produce a deny decision when sentinel is fresh, "
            f"got empty stdout. stderr: {stderr!r}"
        )
        output = json.loads(stdout)
        decision = output.get("hookSpecificOutput", {}).get("permissionDecision", "")
        assert decision == "deny", (
            f"Expected permissionDecision=deny for Write tool, got: {decision!r}"
        )
