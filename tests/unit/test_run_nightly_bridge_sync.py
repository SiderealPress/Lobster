"""
Tests for scripts/run_nightly_bridge_sync.py.

This CLI script replaces the inline Python previously hand-copied into
`.claude/agents/nightly-consolidation.md` step 10 (and its private-overlay
copy). Two regressions motivated extracting it into a tested script:

- #2180: `sys.path.insert(0, 'src')` + `from mcp.user_model.bridges import
  run_bridges` resolves `mcp` to the installed MCP-SDK site-package (which has
  no `user_model` submodule) rather than to `src/mcp/`, because `src/mcp` has
  no `__init__.py` and loses precedence to the installed package.
- #2181: opening the DB with a bare `sqlite3.connect()` instead of
  `user_model.db.open_db()` leaves `row_factory` as `None`, so `user_model.db`
  helpers that do dict-style row access (`row["id"]`) raise
  `TypeError: tuple indices must be integers or slices, not str`.

And the earlier bug PR #2136 already fixed once: `run_bridges()` takes a
single `workspace_path` used for both reads and writes, which breaks when the
canonical read root and the workspace write root differ.
"""

import sqlite3
import sys
from pathlib import Path

import pytest

# Make the repo root importable so `from scripts.run_nightly_bridge_sync
# import main` works with a plain import — no sys.path.insert(0, 'src') or
# `mcp.`-prefix trick required. This mirrors how conftest.py already adds the
# repo root to sys.path for the rest of the test suite, but this test file
# does not depend on conftest.py's autouse fixtures, so make it explicit here
# too.
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def canonical_root(tmp_path: Path) -> Path:
    """A temp canonical-memory root with sample projects/*.md and priorities.md."""
    root = tmp_path / "canonical-root"
    projects_dir = root / "memory" / "canonical" / "projects"
    projects_dir.mkdir(parents=True)

    (projects_dir / "lobster-core.md").write_text(
        "# LobsterCore\n\nStatus: active\n\nAlways-on assistant.\n",
        encoding="utf-8",
    )
    (projects_dir / "transformers.md").write_text(
        "# Transformers\n\nStatus: paused\n\nOn hold.\n",
        encoding="utf-8",
    )

    priorities_file = root / "memory" / "canonical" / "priorities.md"
    priorities_file.write_text(
        "# Priorities\n\n"
        "1. **Ship bridge sync fix** — close out #2180 and #2181\n"
        "2. **Reconcile priorities.md** — nightly consolidation step 7\n",
        encoding="utf-8",
    )

    return root


@pytest.fixture
def workspace_root(tmp_path: Path) -> Path:
    """A temp workspace root (separate from canonical_root) that owns the DB and _context.md output."""
    root = tmp_path / "workspace-root"
    root.mkdir()
    return root


@pytest.fixture
def empty_canonical_root(tmp_path: Path) -> Path:
    """A canonical root with no projects/priorities files at all."""
    root = tmp_path / "empty-canonical-root"
    (root / "memory" / "canonical" / "projects").mkdir(parents=True)
    return root


# ---------------------------------------------------------------------------
# Import shape (guards #2180)
# ---------------------------------------------------------------------------


class TestImportShape:
    """The script must be importable with a plain module import."""

    def test_imports_without_sys_path_or_mcp_prefix_trick(self):
        """
        A plain `from scripts.run_nightly_bridge_sync import main` must work.
        If this ever again required `sys.path.insert(0, 'src')` plus a
        `mcp.user_model...`-prefixed import to reach the real bridges module,
        #2180's ModuleNotFoundError would be back.
        """
        from scripts.run_nightly_bridge_sync import main  # noqa: F401

        assert callable(main)

    def test_module_never_imports_user_model_via_mcp_prefix(self):
        """
        Guard against the exact regression shape: source must not contain
        `from mcp.user_model` or `import mcp.user_model`, which is precisely
        the import that breaks once the installed MCP-SDK `mcp` package
        shadows the namespace-package `src/mcp`.
        """
        script_path = REPO_ROOT / "scripts" / "run_nightly_bridge_sync.py"
        source = script_path.read_text(encoding="utf-8")
        assert "from mcp.user_model" not in source
        assert "import mcp.user_model" not in source


# ---------------------------------------------------------------------------
# Connection factory (guards #2181)
# ---------------------------------------------------------------------------


class TestConnectionFactory:
    """The script must always open the DB via open_db(), never bare sqlite3.connect()."""

    def test_open_connection_uses_row_factory_sqlite_row(self, workspace_root: Path):
        from scripts.run_nightly_bridge_sync import open_connection

        db_path = workspace_root / "data" / "memory.db"
        conn = open_connection(db_path)
        try:
            assert conn.row_factory is sqlite3.Row
        finally:
            conn.close()

    def test_module_never_opens_db_with_bare_sqlite3_connect(self):
        """
        Guard against the exact regression shape: the script's own connection
        helper must route through user_model.db.open_db() (which sets
        row_factory = sqlite3.Row), not a bare sqlite3.connect() call that
        would leave row_factory as None and break dict-style row access.
        """
        script_path = REPO_ROOT / "scripts" / "run_nightly_bridge_sync.py"
        source = script_path.read_text(encoding="utf-8")
        assert "open_db(" in source
        # No bare `sqlite3.connect(` call anywhere in the script.
        assert "sqlite3.connect(" not in source


# ---------------------------------------------------------------------------
# Happy path: independent canonical_root / workspace_root (guards pre-#2136 bug)
# ---------------------------------------------------------------------------


class TestRunNightlyBridgeSync:
    def test_syncs_projects_and_injects_priorities_from_canonical_root(
        self, canonical_root: Path, workspace_root: Path
    ):
        from scripts.run_nightly_bridge_sync import run

        result = run(canonical_root=canonical_root, workspace_root=workspace_root)

        assert result["projects"]["synced"] == 2
        assert result["projects"]["created"] == 2
        assert result["priorities"]["injected"] == 2

    def test_writes_context_cache_under_workspace_root_not_canonical_root(
        self, canonical_root: Path, workspace_root: Path
    ):
        from scripts.run_nightly_bridge_sync import run

        run(canonical_root=canonical_root, workspace_root=workspace_root)

        context_file = workspace_root / "user-model" / "_context.md"
        assert context_file.exists()
        content = context_file.read_text(encoding="utf-8")
        assert "LobsterCore" in content or "Transformers" in content

        # Never written under canonical_root.
        assert not (canonical_root / "user-model" / "_context.md").exists()

    def test_canonical_root_and_workspace_root_must_be_independent(
        self, canonical_root: Path, workspace_root: Path
    ):
        """
        Regression guard for the pre-#2136 bug: run_bridges() took a single
        shared workspace_path for both reads and writes. If canonical_root
        and workspace_root were silently collapsed into one path again, a
        mismatched pair (real canonical data, empty/different workspace)
        would silently produce a near-empty read instead of the populated one.
        """
        from scripts.run_nightly_bridge_sync import run

        # canonical_root has 2 project files and 2 priority items; a *third*,
        # unrelated empty directory stands in for "the workspace". If reads
        # were incorrectly rooted at workspace_root instead of canonical_root,
        # this would report 0 synced / 0 injected instead of 2 / 2.
        result = run(canonical_root=canonical_root, workspace_root=workspace_root)
        assert result["projects"]["synced"] == 2
        assert result["priorities"]["injected"] == 2

        # And swapping them must change the outcome to near-empty, proving
        # the two roots are not silently aliased to each other.
        empty_root = workspace_root.parent / "swap-empty-root"
        empty_root.mkdir()
        swapped = run(canonical_root=empty_root, workspace_root=canonical_root)
        assert swapped["projects"]["synced"] == 0
        assert swapped["priorities"]["injected"] == 0

    def test_empty_canonical_root_reports_zero_without_crashing(
        self, empty_canonical_root: Path, workspace_root: Path
    ):
        from scripts.run_nightly_bridge_sync import run

        result = run(canonical_root=empty_canonical_root, workspace_root=workspace_root)
        assert result["projects"]["synced"] == 0
        assert result["priorities"]["injected"] == 0

    def test_main_accepts_canonical_root_and_workspace_root_as_separate_cli_args(
        self, canonical_root: Path, workspace_root: Path, capsys
    ):
        from scripts.run_nightly_bridge_sync import main

        exit_code = main(
            [
                "--canonical-root",
                str(canonical_root),
                "--workspace-root",
                str(workspace_root),
            ]
        )

        assert exit_code == 0
        out = capsys.readouterr().out
        assert '"synced": 2' in out
        assert (workspace_root / "user-model" / "_context.md").exists()
