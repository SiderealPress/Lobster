"""
Tests for compact-bootup-stub injection (issue #1954).

When inject-bootup-context.py runs on a dispatcher SessionStart and detects
startup_cause == "compaction", it should inject the minimal compact stub
(sys.compact-dispatcher.bootup.md) instead of the full dispatcher bootup file.
User config files are also skipped in this path.

When startup_cause == "restart" (or any non-compaction value), the full
dispatcher bootup and user config files are injected as before.

Named constants (from spec):
  COMPACT_DISPATCHER_BOOTUP_FILENAME = "sys.compact-dispatcher.bootup.md"
    — the filename of the stub injected on compaction starts.

Behaviors tested:
  - On compaction start: compact stub is injected, full bootup is not
  - On compaction start: user config files (base, dispatcher) are skipped
  - On restart start: full dispatcher bootup is injected, compact stub is not
  - On restart start: user config files are injected as usual
  - When compact stub file is absent: falls back to full bootup (graceful degradation)
"""

from __future__ import annotations

import importlib.util
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

_HOOKS_DIR = Path(__file__).parents[3] / "hooks"
_HOOK_PATH = _HOOKS_DIR / "inject-bootup-context.py"

# The stub filename is a named constant shared between implementation and tests.
COMPACT_DISPATCHER_BOOTUP_FILENAME = "sys.compact-dispatcher.bootup.md"

# COMPACTION_CAUSE_WINDOW_SECONDS — max age for a "compaction" entry to be trusted.
# Import from hook to avoid duplicating the constant here.
def _get_window_seconds() -> int:
    spec = importlib.util.spec_from_file_location("_inject_constants", _HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.COMPACTION_CAUSE_WINDOW_SECONDS

COMPACTION_CAUSE_WINDOW_SECONDS = _get_window_seconds()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _PatchEnv:
    """Context manager to temporarily set / restore environment variables."""

    def __init__(self, env: dict):
        self._env = env
        self._saved: dict = {}

    def __enter__(self):
        for k, v in self._env.items():
            self._saved[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return self

    def __exit__(self, *_):
        for k, saved_v in self._saved.items():
            if saved_v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = saved_v


def _make_bootup_files(claude_dir: Path) -> tuple[Path, Path, Path]:
    """Write dispatcher, subagent, and compact stub bootup files."""
    claude_dir.mkdir(parents=True, exist_ok=True)
    dispatcher_bootup = claude_dir / "sys.dispatcher.bootup.md"
    subagent_bootup = claude_dir / "sys.subagent.bootup.md"
    compact_stub = claude_dir / COMPACT_DISPATCHER_BOOTUP_FILENAME
    dispatcher_bootup.write_text("# DISPATCHER BOOTUP FULL\n")
    subagent_bootup.write_text("# SUBAGENT BOOTUP\n")
    compact_stub.write_text("# COMPACT DISPATCHER STUB\n")
    return dispatcher_bootup, subagent_bootup, compact_stub


def _write_startup_cause(cause_file: Path, cause: str, age_seconds: int = 1) -> None:
    """Write a startup cause file with a timestamp of the given age."""
    ts = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    cause_file.write_text(
        json.dumps({"cause": cause, "ts": ts.strftime("%Y-%m-%dT%H:%M:%SZ")})
    )


def _load_and_run_hook(
    *,
    tmp_path: Path,
    workspace: Path,
    startup_flag_pid: int | None,  # None means no flag file
    dispatcher_bootup: Path,
    compact_stub: Path | None,
    cause_file: Path,
    subagent_bootup: Path | None = None,
    user_base_bootup: Path | None = None,
    user_dispatcher_bootup: Path | None = None,
    session_id: str = "test-session-uuid",
) -> tuple[str, str]:
    """Load inject-bootup-context.py, override paths, run main(), return (stdout, stderr)."""
    import uuid as _uuid

    cause_file_str = str(cause_file)
    env = {
        "HOME": str(tmp_path),
        "LOBSTER_WORKSPACE": str(workspace),
        "LOBSTER_STARTUP_CAUSE_FILE_OVERRIDE": cause_file_str,
    }
    unique_name = f"inject_compact_{_uuid.uuid4().hex}"
    with _PatchEnv(env):
        spec = importlib.util.spec_from_file_location(unique_name, _HOOK_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

    # Set up the startup flag.
    flag_path = workspace / "data" / "dispatcher-startup-flag"
    if startup_flag_pid is not None:
        flag_path.write_text(str(startup_flag_pid))
    else:
        flag_path.unlink(missing_ok=True)

    # Override module-level paths.
    mod.STARTUP_FLAG_FILE = flag_path
    mod.DISPATCHER_BOOTUP = dispatcher_bootup
    mod.SUBAGENT_BOOTUP = subagent_bootup if subagent_bootup is not None else tmp_path / "no-subagent"
    mod.STARTUP_CAUSE_FILE = cause_file

    if compact_stub is not None:
        mod.COMPACT_DISPATCHER_BOOTUP = compact_stub
    else:
        # Force the attribute to a non-existent path to test fallback behavior.
        mod.COMPACT_DISPATCHER_BOOTUP = tmp_path / "no-compact-stub"

    # User config paths.
    if user_base_bootup is not None:
        mod.USER_BASE_BOOTUP = user_base_bootup
    else:
        mod.USER_BASE_BOOTUP = tmp_path / "no-user-base"

    if user_dispatcher_bootup is not None:
        mod.USER_DISPATCHER_BOOTUP = user_dispatcher_bootup
    else:
        mod.USER_DISPATCHER_BOOTUP = tmp_path / "no-user-dispatcher"

    mod.USER_SUBAGENT_BOOTUP = tmp_path / "no-user-subagent"
    mod.CONTEXT_INJECTION_LOG = workspace / "logs" / "context-injection.log"

    hook_input = json.dumps({"session_id": session_id})
    import io as _io
    from unittest.mock import patch as _patch

    stdout_buf = _io.StringIO()
    stderr_buf = _io.StringIO()

    with _patch("sys.stdin", _io.StringIO(hook_input)):
        with _patch("sys.stdout", stdout_buf):
            with _patch("sys.stderr", stderr_buf):
                try:
                    mod.main()
                except SystemExit:
                    pass

    return stdout_buf.getvalue(), stderr_buf.getvalue()


# ---------------------------------------------------------------------------
# Tests: compact stub injected on compaction start
# ---------------------------------------------------------------------------


class TestCompactStubInjectedOnCompactionStart:
    """On a compaction-caused dispatcher start, the compact stub replaces the full bootup."""

    def test_compact_stub_injected_when_cause_is_compaction(self, tmp_path):
        """Compact stub is injected instead of full dispatcher bootup on compaction start."""
        workspace = tmp_path / "workspace"
        (workspace / "data").mkdir(parents=True, exist_ok=True)
        (workspace / "logs").mkdir(parents=True, exist_ok=True)

        claude_dir = tmp_path / "lobster" / ".claude"
        dispatcher_bootup, _, compact_stub = _make_bootup_files(claude_dir)

        cause_file = workspace / "data" / "last-startup-cause.json"
        _write_startup_cause(cause_file, "compaction", age_seconds=1)

        stdout, _ = _load_and_run_hook(
            tmp_path=tmp_path,
            workspace=workspace,
            startup_flag_pid=os.getpid(),
            dispatcher_bootup=dispatcher_bootup,
            compact_stub=compact_stub,
            cause_file=cause_file,
        )

        assert "COMPACT DISPATCHER STUB" in stdout, (
            "Compact stub must be injected when startup cause is 'compaction'"
        )
        assert "DISPATCHER BOOTUP FULL" not in stdout, (
            "Full dispatcher bootup must NOT be injected on a compaction start"
        )

    def test_full_bootup_not_injected_on_compaction_start(self, tmp_path):
        """Full bootup is absent from output on compaction start."""
        workspace = tmp_path / "workspace"
        (workspace / "data").mkdir(parents=True, exist_ok=True)
        (workspace / "logs").mkdir(parents=True, exist_ok=True)

        claude_dir = tmp_path / "lobster" / ".claude"
        dispatcher_bootup, _, compact_stub = _make_bootup_files(claude_dir)

        cause_file = workspace / "data" / "last-startup-cause.json"
        _write_startup_cause(cause_file, "compaction", age_seconds=1)

        stdout, _ = _load_and_run_hook(
            tmp_path=tmp_path,
            workspace=workspace,
            startup_flag_pid=os.getpid(),
            dispatcher_bootup=dispatcher_bootup,
            compact_stub=compact_stub,
            cause_file=cause_file,
        )

        assert "DISPATCHER BOOTUP FULL" not in stdout

    def test_user_config_files_skipped_on_compaction_start(self, tmp_path):
        """User base and dispatcher bootup files are NOT injected on compaction start."""
        workspace = tmp_path / "workspace"
        (workspace / "data").mkdir(parents=True, exist_ok=True)
        (workspace / "logs").mkdir(parents=True, exist_ok=True)

        claude_dir = tmp_path / "lobster" / ".claude"
        dispatcher_bootup, _, compact_stub = _make_bootup_files(claude_dir)

        # Create user config files with distinctive content.
        user_base = tmp_path / "user.base.bootup.md"
        user_base.write_text("# USER BASE BOOTUP\n")
        user_dispatcher = tmp_path / "user.dispatcher.bootup.md"
        user_dispatcher.write_text("# USER DISPATCHER BOOTUP\n")

        cause_file = workspace / "data" / "last-startup-cause.json"
        _write_startup_cause(cause_file, "compaction", age_seconds=1)

        stdout, _ = _load_and_run_hook(
            tmp_path=tmp_path,
            workspace=workspace,
            startup_flag_pid=os.getpid(),
            dispatcher_bootup=dispatcher_bootup,
            compact_stub=compact_stub,
            cause_file=cause_file,
            user_base_bootup=user_base,
            user_dispatcher_bootup=user_dispatcher,
        )

        assert "USER BASE BOOTUP" not in stdout, (
            "User base bootup must NOT be injected on a compaction start (saves tokens)"
        )
        assert "USER DISPATCHER BOOTUP" not in stdout, (
            "User dispatcher bootup must NOT be injected on a compaction start"
        )

    def test_startup_cause_banner_still_present_in_compact_stub_path(self, tmp_path):
        """The startup-cause banner is printed even when the compact stub is used."""
        workspace = tmp_path / "workspace"
        (workspace / "data").mkdir(parents=True, exist_ok=True)
        (workspace / "logs").mkdir(parents=True, exist_ok=True)

        claude_dir = tmp_path / "lobster" / ".claude"
        dispatcher_bootup, _, compact_stub = _make_bootup_files(claude_dir)

        cause_file = workspace / "data" / "last-startup-cause.json"
        _write_startup_cause(cause_file, "compaction", age_seconds=1)

        stdout, _ = _load_and_run_hook(
            tmp_path=tmp_path,
            workspace=workspace,
            startup_flag_pid=os.getpid(),
            dispatcher_bootup=dispatcher_bootup,
            compact_stub=compact_stub,
            cause_file=cause_file,
        )

        assert "startup-cause: compaction" in stdout, (
            "The startup-cause banner must still appear in output even when compact stub is used"
        )


# ---------------------------------------------------------------------------
# Tests: full bootup injected on restart start (unchanged behavior)
# ---------------------------------------------------------------------------


class TestFullBootupInjectedOnRestartStart:
    """On a restart-caused dispatcher start, full bootup and user config are injected as before."""

    def test_full_bootup_injected_on_restart(self, tmp_path):
        """Full dispatcher bootup is injected when startup cause is 'restart'."""
        workspace = tmp_path / "workspace"
        (workspace / "data").mkdir(parents=True, exist_ok=True)
        (workspace / "logs").mkdir(parents=True, exist_ok=True)

        claude_dir = tmp_path / "lobster" / ".claude"
        dispatcher_bootup, _, compact_stub = _make_bootup_files(claude_dir)

        cause_file = workspace / "data" / "last-startup-cause.json"
        _write_startup_cause(cause_file, "restart", age_seconds=1)

        stdout, _ = _load_and_run_hook(
            tmp_path=tmp_path,
            workspace=workspace,
            startup_flag_pid=os.getpid(),
            dispatcher_bootup=dispatcher_bootup,
            compact_stub=compact_stub,
            cause_file=cause_file,
        )

        assert "DISPATCHER BOOTUP FULL" in stdout, (
            "Full dispatcher bootup must be injected when startup cause is 'restart'"
        )
        assert "COMPACT DISPATCHER STUB" not in stdout, (
            "Compact stub must NOT be injected on a restart start"
        )

    def test_user_config_injected_on_restart(self, tmp_path):
        """User base and dispatcher bootup files ARE injected on restart start."""
        workspace = tmp_path / "workspace"
        (workspace / "data").mkdir(parents=True, exist_ok=True)
        (workspace / "logs").mkdir(parents=True, exist_ok=True)

        claude_dir = tmp_path / "lobster" / ".claude"
        dispatcher_bootup, _, compact_stub = _make_bootup_files(claude_dir)

        user_base = tmp_path / "user.base.bootup.md"
        user_base.write_text("# USER BASE BOOTUP\n")
        user_dispatcher = tmp_path / "user.dispatcher.bootup.md"
        user_dispatcher.write_text("# USER DISPATCHER BOOTUP\n")

        cause_file = workspace / "data" / "last-startup-cause.json"
        _write_startup_cause(cause_file, "restart", age_seconds=1)

        stdout, _ = _load_and_run_hook(
            tmp_path=tmp_path,
            workspace=workspace,
            startup_flag_pid=os.getpid(),
            dispatcher_bootup=dispatcher_bootup,
            compact_stub=compact_stub,
            cause_file=cause_file,
            user_base_bootup=user_base,
            user_dispatcher_bootup=user_dispatcher,
        )

        assert "USER BASE BOOTUP" in stdout, (
            "User base bootup must be injected on restart start"
        )
        assert "USER DISPATCHER BOOTUP" in stdout, (
            "User dispatcher bootup must be injected on restart start"
        )

    def test_no_startup_cause_file_treats_as_restart(self, tmp_path):
        """When startup cause file is absent, defaults to restart (full bootup injected)."""
        workspace = tmp_path / "workspace"
        (workspace / "data").mkdir(parents=True, exist_ok=True)
        (workspace / "logs").mkdir(parents=True, exist_ok=True)

        claude_dir = tmp_path / "lobster" / ".claude"
        dispatcher_bootup, _, compact_stub = _make_bootup_files(claude_dir)

        # No cause file — should default to "restart".
        cause_file = workspace / "data" / "last-startup-cause.json"
        # File intentionally not created.

        stdout, _ = _load_and_run_hook(
            tmp_path=tmp_path,
            workspace=workspace,
            startup_flag_pid=os.getpid(),
            dispatcher_bootup=dispatcher_bootup,
            compact_stub=compact_stub,
            cause_file=cause_file,
        )

        assert "DISPATCHER BOOTUP FULL" in stdout, (
            "When startup cause file is absent, full bootup must be injected (default is restart)"
        )
        assert "COMPACT DISPATCHER STUB" not in stdout


# ---------------------------------------------------------------------------
# Tests: fallback when compact stub file is missing
# ---------------------------------------------------------------------------


class TestCompactStubFallbackWhenFileMissing:
    """When the compact stub file does not exist, fall back to the full bootup."""

    def test_falls_back_to_full_bootup_when_compact_stub_absent(self, tmp_path):
        """When compact stub is absent, inject full bootup on compaction start (safe fallback)."""
        workspace = tmp_path / "workspace"
        (workspace / "data").mkdir(parents=True, exist_ok=True)
        (workspace / "logs").mkdir(parents=True, exist_ok=True)

        claude_dir = tmp_path / "lobster" / ".claude"
        # Only create dispatcher and subagent bootup — NOT the compact stub.
        dispatcher_bootup = claude_dir / "sys.dispatcher.bootup.md"
        claude_dir.mkdir(parents=True, exist_ok=True)
        dispatcher_bootup.write_text("# DISPATCHER BOOTUP FULL\n")

        cause_file = workspace / "data" / "last-startup-cause.json"
        _write_startup_cause(cause_file, "compaction", age_seconds=1)

        stdout, _ = _load_and_run_hook(
            tmp_path=tmp_path,
            workspace=workspace,
            startup_flag_pid=os.getpid(),
            dispatcher_bootup=dispatcher_bootup,
            compact_stub=None,  # Signals to use a non-existent path.
            cause_file=cause_file,
        )

        assert "DISPATCHER BOOTUP FULL" in stdout, (
            "Full bootup must be injected when compact stub is absent (safe fallback)"
        )


# ---------------------------------------------------------------------------
# Tests: stale compaction cause treated as restart (existing behavior guard)
# ---------------------------------------------------------------------------


class TestStaleCompactionCauseTreatedAsRestart:
    """A stale compaction cause (> COMPACTION_CAUSE_WINDOW_SECONDS) is treated as restart."""

    def test_stale_compaction_cause_injects_full_bootup(self, tmp_path):
        """Stale compaction entry (> 5 min) triggers full bootup injection, not compact stub."""
        workspace = tmp_path / "workspace"
        (workspace / "data").mkdir(parents=True, exist_ok=True)
        (workspace / "logs").mkdir(parents=True, exist_ok=True)

        claude_dir = tmp_path / "lobster" / ".claude"
        dispatcher_bootup, _, compact_stub = _make_bootup_files(claude_dir)

        cause_file = workspace / "data" / "last-startup-cause.json"
        # Write a compaction cause that is 10 minutes old (outside the 5-min window).
        _write_startup_cause(cause_file, "compaction", age_seconds=600)

        stdout, _ = _load_and_run_hook(
            tmp_path=tmp_path,
            workspace=workspace,
            startup_flag_pid=os.getpid(),
            dispatcher_bootup=dispatcher_bootup,
            compact_stub=compact_stub,
            cause_file=cause_file,
        )

        assert "DISPATCHER BOOTUP FULL" in stdout, (
            "Stale compaction entry must be treated as restart — full bootup must be injected"
        )
        assert "COMPACT DISPATCHER STUB" not in stdout, (
            "Compact stub must NOT be injected for a stale compaction entry"
        )


# ---------------------------------------------------------------------------
# Tests: subagent path is unaffected
# ---------------------------------------------------------------------------


class TestSubagentPathUnchanged:
    """Subagent sessions (no startup flag) are never affected by this change."""

    def test_subagent_gets_subagent_bootup_regardless_of_cause(self, tmp_path):
        """When no startup flag, subagent bootup is injected regardless of startup cause."""
        workspace = tmp_path / "workspace"
        (workspace / "data").mkdir(parents=True, exist_ok=True)
        (workspace / "logs").mkdir(parents=True, exist_ok=True)

        claude_dir = tmp_path / "lobster" / ".claude"
        dispatcher_bootup, subagent_bootup, compact_stub = _make_bootup_files(claude_dir)

        # Write compaction cause — should have no effect on subagent injection.
        cause_file = workspace / "data" / "last-startup-cause.json"
        _write_startup_cause(cause_file, "compaction", age_seconds=1)

        stdout, _ = _load_and_run_hook(
            tmp_path=tmp_path,
            workspace=workspace,
            startup_flag_pid=None,  # No startup flag → subagent session.
            dispatcher_bootup=dispatcher_bootup,
            compact_stub=compact_stub,
            cause_file=cause_file,
            subagent_bootup=subagent_bootup,
            session_id="subagent-uuid",
        )

        assert "SUBAGENT BOOTUP" in stdout, (
            "Subagent bootup must be injected for non-dispatcher sessions regardless of cause"
        )
        assert "COMPACT DISPATCHER STUB" not in stdout
        assert "DISPATCHER BOOTUP FULL" not in stdout
