"""
Unit tests for hooks/pin-dependencies-guard.py

Tests cover:
- Manifest detection: package.json, pyproject.toml, requirements*.txt, Pipfile
  detected; non-manifest files ignored (find_unpinned_range / is_dependency_manifest)
- Every range operator (^, ~, ~=, >=, <=, !=, >, <, *, "latest") is blocked,
  both as a pure-function check and via full hook invocation for Edit
  (new_string) and Write (content)
- Exact pins (==, @1.2.3) are NOT blocked
- The package.json "engines" regression (commit 10a1ee95): "engines":
  {"node": ">=18.0.0"} must NOT be flagged, for both a full-file Write and an
  Edit fragment
- Bash command detection: npm/pip/uv unpinned installs and upgrade flags are
  blocked; lockfile-respecting / already-pinned commands are not
- LOBSTER_ALLOW_DEPENDENCY_CHANGE=true bypasses both Edit/Write and Bash checks
- Non-Edit/Write/Bash tool calls and malformed/missing stdin are handled
  gracefully (exit 0, no crash, no deny)

Convention: pure functions are imported directly via importlib.util (as in
test_auto_register_agent.py); full hook behavior (stdin -> stdout JSON / exit
code) is exercised via subprocess, matching the sibling
test_require_reply_to_message_id.py convention for stdin/stdout-driven hooks.
"""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_HOOKS_DIR = Path(__file__).parents[3] / "hooks"
HOOK_PATH = _HOOKS_DIR / "pin-dependencies-guard.py"

_ENV_OVERRIDE = "LOBSTER_ALLOW_DEPENDENCY_CHANGE"


def _load_module():
    spec = importlib.util.spec_from_file_location("pin_dependencies_guard", HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_mod = _load_module()
is_dependency_manifest = _mod.is_dependency_manifest
_is_package_json = _mod._is_package_json
find_unpinned_range = _mod.find_unpinned_range
bash_introduces_unpinned_dependency = _mod.bash_introduces_unpinned_dependency
_is_override_set = _mod._is_override_set
_strip_full_line_comments = _mod._strip_full_line_comments
_mask_heredocs = _mod._mask_heredocs
_strip_wrappers = _mod._strip_wrappers
_normalize_command_name = _mod._normalize_command_name
_normalize_python_dash_m_pip = _mod._normalize_python_dash_m_pip
_resolve_subcommand = _mod._resolve_subcommand


# ---------------------------------------------------------------------------
# Subprocess helper for full hook invocation
# ---------------------------------------------------------------------------


def _clean_env(overrides: dict | None = None) -> dict:
    """os.environ copy with LOBSTER_ALLOW_DEPENDENCY_CHANGE explicitly unset
    unless the caller opts in via overrides, so tests are isolated from
    whatever the host process happens to have set."""
    env = os.environ.copy()
    env.pop(_ENV_OVERRIDE, None)
    if overrides:
        env.update(overrides)
    return env


def _run_hook(payload: dict, env: dict | None = None) -> tuple[int, str, str]:
    result = subprocess.run(
        [sys.executable, str(HOOK_PATH)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=_clean_env(env),
    )
    return result.returncode, result.stdout, result.stderr


def _run_hook_raw_stdin(stdin_text: str, env: dict | None = None) -> tuple[int, str, str]:
    result = subprocess.run(
        [sys.executable, str(HOOK_PATH)],
        input=stdin_text,
        capture_output=True,
        text=True,
        env=_clean_env(env),
    )
    return result.returncode, result.stdout, result.stderr


def _is_denied(stdout: str) -> bool:
    """True if the hook's stdout JSON represents a permissionDecision=deny."""
    if not stdout.strip():
        return False
    data = json.loads(stdout)
    return (
        data.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"
    )


def _edit_payload(file_path: str, new_string: str) -> dict:
    return {
        "tool_name": "Edit",
        "tool_input": {"file_path": file_path, "new_string": new_string},
    }


def _write_payload(file_path: str, content: str) -> dict:
    return {
        "tool_name": "Write",
        "tool_input": {"file_path": file_path, "content": content},
    }


def _bash_payload(command: str) -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": command}}


# ---------------------------------------------------------------------------
# (a) Manifest detection
# ---------------------------------------------------------------------------


class TestManifestDetection:
    @pytest.mark.parametrize(
        "path",
        [
            "package.json",
            "connectors/whatsapp/package.json",
            "pyproject.toml",
            "lobster-shop/multiplayer-telegram-bot/pyproject.toml",
            "requirements.txt",
            "tests/requirements-test.txt",
            "requirements-dev.txt",
            "Pipfile",
            "some/nested/dir/Pipfile",
        ],
    )
    def test_manifest_files_detected(self, path):
        assert is_dependency_manifest(path) is True

    @pytest.mark.parametrize(
        "path",
        [
            "",
            "README.md",
            "src/main.py",
            "package.json.bak",
            "requirements.txt.orig",
            "package-lock.json",
            "uv.lock",
            "notpyproject.toml.txt",
            "Pipfile.lock",
        ],
    )
    def test_non_manifest_files_ignored(self, path):
        assert is_dependency_manifest(path) is False

    def test_none_like_empty_string_is_not_a_manifest(self):
        assert is_dependency_manifest("") is False


class TestIsPackageJson:
    def test_package_json_true(self):
        assert _is_package_json("package.json") is True
        assert _is_package_json("connectors/whatsapp/package.json") is True

    @pytest.mark.parametrize(
        "path", ["pyproject.toml", "requirements.txt", "Pipfile", ""]
    )
    def test_non_package_json_false(self, path):
        assert _is_package_json(path) is False


# ---------------------------------------------------------------------------
# (b) + (c) Range operator detection: pure-function level, python-style manifests
# ---------------------------------------------------------------------------


class TestFindUnpinnedRangePython:
    """pyproject.toml / requirements.txt / Pipfile-style detection
    (find_unpinned_range with is_package_json=False)."""

    @pytest.mark.parametrize(
        "text,operator",
        [
            ("requests>=2.0.0", ">="),
            ("requests<=2.0.0", "<="),
            ("requests~=2.0.0", "~="),
            ("requests!=2.0.0", "!="),
            ("requests>2.0.0", ">"),
            ("requests<2.0.0", "<"),
            ("requests^2.0.0", "^"),
        ],
    )
    def test_operator_blocked(self, text, operator):
        match = find_unpinned_range(text, is_package_json=False)
        assert match is not None, f"expected {operator!r} range to be flagged in {text!r}"

    @pytest.mark.parametrize(
        "text",
        [
            'requests = "*"',
            'requests = "latest"',
            'requests = "LATEST"',
        ],
    )
    def test_star_and_latest_blocked_pipfile_style(self, text):
        match = find_unpinned_range(text, is_package_json=False)
        assert match is not None

    @pytest.mark.parametrize(
        "text",
        [
            "requests==2.0.0",
            'mcp==1.26.0',
            "package[extra]==1.0.0",
            "hatchling==1.31.0",
        ],
    )
    def test_exact_pin_not_blocked(self, text):
        assert find_unpinned_range(text, is_package_json=False) is None

    def test_realistic_pyproject_dependency_line_blocked(self):
        text = 'dependencies = [\n    "mcp>=1.0.0",\n]\n'
        assert find_unpinned_range(text, is_package_json=False) is not None

    def test_realistic_pyproject_dependency_line_pinned_not_blocked(self):
        text = 'dependencies = [\n    "mcp==1.26.0",\n]\n'
        assert find_unpinned_range(text, is_package_json=False) is None


# ---------------------------------------------------------------------------
# (b) + (c) + (d) Range operator detection: package.json
# ---------------------------------------------------------------------------


class TestFindUnpinnedRangePackageJson:
    @pytest.mark.parametrize(
        "operator,value",
        [
            ("^", "^3.6.0"),
            ("~", "~3.6.0"),
            (">=", ">=3.6.0"),
            ("<=", "<=3.6.0"),
            ("!=", "!=3.6.0"),
            (">", ">3.6.0"),
            ("<", "<3.6.0"),
            ("*", "*"),
            ("latest", "latest"),
        ],
    )
    def test_operator_blocked_as_edit_fragment(self, operator, value):
        """Edit fragments are rarely valid standalone JSON, so this exercises
        the regex-fallback path in _find_unpinned_range_in_package_json."""
        fragment = f'  "chokidar": "{value}",\n'
        match = find_unpinned_range(fragment, is_package_json=True)
        assert match is not None, f"expected {operator!r} to be flagged in fragment {fragment!r}"

    @pytest.mark.parametrize(
        "operator,value",
        [
            ("^", "^3.6.0"),
            ("~", "~3.6.0"),
            (">=", ">=3.6.0"),
            ("<=", "<=3.6.0"),
            ("!=", "!=3.6.0"),
            (">", ">3.6.0"),
            ("<", "<3.6.0"),
            ("*", "*"),
            ("latest", "latest"),
        ],
    )
    def test_operator_blocked_as_full_write_content(self, operator, value):
        """Full-file Write content is valid JSON, exercising the json.loads
        parse path (restricted to real dependency-block keys)."""
        content = json.dumps({"dependencies": {"chokidar": value}})
        match = find_unpinned_range(content, is_package_json=True)
        assert match is not None, f"expected {operator!r} to be flagged in {content!r}"

    @pytest.mark.parametrize(
        "value", ["3.6.0", "1.5.4"],
    )
    def test_exact_pin_not_blocked_fragment(self, value):
        fragment = f'  "chokidar": "{value}",\n'
        assert find_unpinned_range(fragment, is_package_json=True) is None

    def test_exact_pin_not_blocked_full_write(self):
        content = json.dumps({"dependencies": {"chokidar": "3.6.0"}})
        assert find_unpinned_range(content, is_package_json=True) is None

    @pytest.mark.parametrize(
        "block_key",
        [
            "dependencies",
            "devDependencies",
            "peerDependencies",
            "optionalDependencies",
            "resolutions",
            "overrides",
        ],
    )
    def test_all_dependency_blocks_covered_full_write(self, block_key):
        content = json.dumps({block_key: {"lodash": "^4.17.21"}})
        assert find_unpinned_range(content, is_package_json=True) is not None

    # -- The "engines" regression (commit 10a1ee95) --------------------------

    def test_engines_range_not_blocked_full_write(self):
        """A full package.json Write with only an engines block containing
        a range must NOT be flagged — this was the bug fixed in 10a1ee95."""
        content = json.dumps({"engines": {"node": ">=18.0.0"}})
        assert find_unpinned_range(content, is_package_json=True) is None

    def test_engines_range_not_blocked_edit_fragment(self):
        """Same check for the Edit-fragment (regex fallback) path."""
        fragment = '  "engines": {\n    "node": ">=18.0.0"\n  },\n'
        assert find_unpinned_range(fragment, is_package_json=True) is None

    def test_engines_alongside_real_dependency_range_full_write(self):
        """engines must be ignored, but a real dependency range in the same
        file must still be caught."""
        content = json.dumps(
            {
                "engines": {"node": ">=18.0.0"},
                "dependencies": {"chokidar": "^3.6.0"},
            }
        )
        match = find_unpinned_range(content, is_package_json=True)
        assert match is not None
        assert "chokidar" in match

    def test_engines_alongside_real_dependency_range_fragment(self):
        fragment = (
            '  "engines": {\n    "node": ">=18.0.0"\n  },\n'
            '  "dependencies": {\n    "chokidar": "^3.6.0"\n  }\n'
        )
        match = find_unpinned_range(fragment, is_package_json=True)
        assert match is not None


# ---------------------------------------------------------------------------
# (b)/(c)/(d) Full hook integration for Edit and Write
# ---------------------------------------------------------------------------


class TestHookEditWriteIntegration:
    def test_edit_package_json_range_blocked(self):
        rc, stdout, _ = _run_hook(
            _edit_payload("connectors/whatsapp/package.json", '"chokidar": "^3.6.0"')
        )
        assert rc == 0
        assert _is_denied(stdout)

    def test_write_package_json_range_blocked(self):
        content = json.dumps({"dependencies": {"chokidar": "^3.6.0"}})
        rc, stdout, _ = _run_hook(_write_payload("package.json", content))
        assert rc == 0
        assert _is_denied(stdout)

    def test_edit_pyproject_range_blocked(self):
        rc, stdout, _ = _run_hook(
            _edit_payload("pyproject.toml", 'dependencies = ["mcp>=1.0.0"]')
        )
        assert rc == 0
        assert _is_denied(stdout)

    def test_write_pyproject_range_blocked(self):
        rc, stdout, _ = _run_hook(
            _write_payload("pyproject.toml", 'dependencies = ["mcp>=1.0.0"]\n')
        )
        assert rc == 0
        assert _is_denied(stdout)

    def test_edit_pyproject_exact_pin_allowed(self):
        rc, stdout, _ = _run_hook(
            _edit_payload("pyproject.toml", 'dependencies = ["mcp==1.26.0"]')
        )
        assert rc == 0
        assert not _is_denied(stdout)

    def test_write_package_json_exact_pin_allowed(self):
        content = json.dumps({"dependencies": {"chokidar": "3.6.0"}})
        rc, stdout, _ = _run_hook(_write_payload("package.json", content))
        assert rc == 0
        assert not _is_denied(stdout)

    def test_write_package_json_engines_only_allowed(self):
        """Full hook run of the exact 10a1ee95 regression scenario."""
        content = json.dumps({"engines": {"node": ">=18.0.0"}})
        rc, stdout, _ = _run_hook(_write_payload("package.json", content))
        assert rc == 0
        assert not _is_denied(stdout)

    def test_edit_package_json_engines_only_allowed(self):
        rc, stdout, _ = _run_hook(
            _edit_payload(
                "connectors/whatsapp/package.json",
                '  "engines": {\n    "node": ">=18.0.0"\n  },\n',
            )
        )
        assert rc == 0
        assert not _is_denied(stdout)

    def test_edit_non_manifest_file_allowed_even_with_range_looking_text(self):
        rc, stdout, _ = _run_hook(
            _edit_payload("src/main.py", 'VERSION_CONSTRAINT = "requests>=2.0.0"')
        )
        assert rc == 0
        assert not _is_denied(stdout)

    def test_edit_requirements_test_range_blocked(self):
        rc, stdout, _ = _run_hook(
            _edit_payload("tests/requirements-test.txt", "pytest>=8.0.0\n")
        )
        assert rc == 0
        assert _is_denied(stdout)

    def test_edit_empty_new_string_allowed(self):
        rc, stdout, _ = _run_hook(_edit_payload("pyproject.toml", ""))
        assert rc == 0
        assert not _is_denied(stdout)


# ---------------------------------------------------------------------------
# (e) Bash detection
# ---------------------------------------------------------------------------


class TestBashDetectionPureFunction:
    @pytest.mark.parametrize(
        "command",
        [
            "npm install lodash",
            "npm i lodash",
            "npm add lodash",
            "npm update",
            "npm upgrade",
            "npm up lodash",
            "pip install requests",
            "pip3 install requests",
            "pip install --upgrade requests",
            "pip install -U requests",
            "uv add requests",
            "uv sync --upgrade",
            "uv sync -U",
            "uv lock --upgrade-package requests",
        ],
    )
    def test_blocked_commands(self, command):
        assert bash_introduces_unpinned_dependency(command) is not None

    @pytest.mark.parametrize(
        "command",
        [
            "npm ci",
            "npm install",
            "npm install --save-dev",
            "uv sync",
            "uv lock",
            "pip install -r requirements.txt",
            "uv pip install -r requirements.txt",
            "npm install lodash@4.17.21",
            "pip install requests==2.31.0",
            "uv add requests==2.31.0",
            "pip install -e .",
            "pip install git+https://github.com/org/repo.git",
        ],
    )
    def test_allowed_commands(self, command):
        assert bash_introduces_unpinned_dependency(command) is None

    def test_compound_command_with_blocked_segment(self):
        assert (
            bash_introduces_unpinned_dependency("echo hi && npm install lodash")
            is not None
        )

    def test_compound_command_all_segments_allowed(self):
        assert (
            bash_introduces_unpinned_dependency("echo hi && npm ci && echo done")
            is None
        )


class TestHookBashIntegration:
    @pytest.mark.parametrize(
        "command",
        [
            "npm install lodash",
            "npm update",
            "pip install requests",
            "pip install --upgrade requests",
            "uv add requests",
            "uv sync --upgrade",
        ],
    )
    def test_bash_blocked(self, command):
        rc, stdout, _ = _run_hook(_bash_payload(command))
        assert rc == 0
        assert _is_denied(stdout)

    @pytest.mark.parametrize(
        "command",
        [
            "npm ci",
            "npm install",
            "uv sync",
            "pip install -r requirements.txt",
        ],
    )
    def test_bash_allowed(self, command):
        rc, stdout, _ = _run_hook(_bash_payload(command))
        assert rc == 0
        assert not _is_denied(stdout)


# ---------------------------------------------------------------------------
# (e2) Bash bypass-gap fixes: python -m pip, path-prefixed binaries,
# env/command wrappers, uv run --with (independent-reviewer NEEDS-WORK #1)
# ---------------------------------------------------------------------------


class TestBashBypassGapsPureFunction:
    """Every one of these is an ordinary, everyday invocation (not
    adversarial evasion) that silently installed an unpinned package with
    zero block before the fix — demonstrated directly by the independent
    reviewer on PR #2151."""

    @pytest.mark.parametrize(
        "command",
        [
            # python -m pip: one of the most commonly recommended ways to
            # invoke pip at all.
            "python -m pip install requests",
            "python3 -m pip install requests",
            "python3.12 -m pip install requests",
            # path-prefixed / wrapper-prefixed binaries: the regexes only
            # matched when npm/pip/uv sat literally at position 0.
            "/usr/bin/pip install requests",
            "/usr/local/bin/pip3 install requests",
            "venv/bin/pip install requests",
            "node_modules/.bin/npm install lodash",
            "/usr/local/bin/npm install lodash",
            "env pip install requests",
            "command pip install requests",
            "env command pip install requests",
            "FOO=bar pip install requests",
            "env FOO=bar pip install requests",
            # python -m pip through a path-prefixed python binary too.
            "/usr/bin/python3 -m pip install requests",
            # uv run --with <pkg>: installs an ephemeral unpinned dependency.
            "uv run --with requests python foo.py",
            "uv run --with=requests python foo.py",
            "uv run --with requests --with pandas python foo.py",
            "uv run --with requests,pandas python foo.py",
        ],
    )
    def test_bypass_gap_now_blocked(self, command):
        assert bash_introduces_unpinned_dependency(command) is not None, (
            f"expected {command!r} to be detected as an unpinned install"
        )

    @pytest.mark.parametrize(
        "command",
        [
            # Pinned via the same forms — must NOT be blocked.
            "python -m pip install requests==2.31.0",
            "python3 -m pip install requests==2.31.0",
            "/usr/bin/pip install requests==2.31.0",
            "venv/bin/pip install requests==2.31.0",
            "env pip install requests==2.31.0",
            "FOO=bar pip install requests==2.31.0",
            "uv run --with requests==2.31.0 python foo.py",
            "uv run python foo.py",
            "uv run --with-requirements reqs.txt python foo.py",
            # lockfile-respecting forms via a path prefix.
            "/usr/local/bin/npm ci",
            "venv/bin/pip install -r requirements.txt",
        ],
    )
    def test_pinned_or_lockfile_forms_via_new_paths_not_blocked(self, command):
        assert bash_introduces_unpinned_dependency(command) is None


class TestNormalizationHelpers:
    def test_normalize_command_name_strips_path_prefix(self):
        assert _normalize_command_name("/usr/bin/pip install x") == "pip install x"
        assert (
            _normalize_command_name("node_modules/.bin/npm install x")
            == "npm install x"
        )

    def test_normalize_command_name_leaves_bare_command_alone(self):
        assert _normalize_command_name("npm install x") == "npm install x"

    def test_strip_wrappers_env_and_command(self):
        assert _strip_wrappers("env pip install x") == "pip install x"
        assert _strip_wrappers("command pip install x") == "pip install x"
        assert _strip_wrappers("env command pip install x") == "pip install x"

    def test_strip_wrappers_leading_assignment(self):
        assert _strip_wrappers("FOO=bar pip install x") == "pip install x"
        assert _strip_wrappers("env FOO=bar pip install x") == "pip install x"

    def test_strip_wrappers_no_wrapper_is_noop(self):
        assert _strip_wrappers("pip install x") == "pip install x"

    def test_normalize_python_dash_m_pip(self):
        assert (
            _normalize_python_dash_m_pip("python -m pip install x")
            == "pip install x"
        )
        assert (
            _normalize_python_dash_m_pip("python3 -m pip install x")
            == "pip install x"
        )

    def test_normalize_python_dash_m_pip_noop_for_non_pip(self):
        assert (
            _normalize_python_dash_m_pip("python -m venv .venv")
            == "python -m venv .venv"
        )

    def test_resolve_subcommand_full_pipeline(self):
        assert (
            _resolve_subcommand("/usr/bin/python3 -m pip install x")
            == "pip install x"
        )


class TestHookBashBypassGapsIntegration:
    @pytest.mark.parametrize(
        "command",
        [
            "python -m pip install requests",
            "python3 -m pip install requests",
            "/usr/bin/pip install requests",
            "venv/bin/pip install requests",
            "node_modules/.bin/npm install lodash",
            "env pip install requests",
            "command pip install requests",
            "uv run --with requests python foo.py",
        ],
    )
    def test_bypass_gap_blocked_via_full_hook(self, command):
        rc, stdout, _ = _run_hook(_bash_payload(command))
        assert rc == 0
        assert _is_denied(stdout)


# ---------------------------------------------------------------------------
# (e3) Bash false-positive fixes: heredoc bodies, bare "and"/"or", local-path
# npm installs (independent-reviewer NEEDS-WORK #2)
# ---------------------------------------------------------------------------


class TestBashFalsePositivesPureFunction:
    def test_heredoc_body_containing_install_text_not_blocked(self):
        command = "cat > install.sh <<'EOF'\nnpm install lodash\nEOF\n"
        assert bash_introduces_unpinned_dependency(command) is None

    def test_heredoc_unquoted_delimiter_not_blocked(self):
        command = "cat > install.sh <<EOF\npip install requests\nEOF\n"
        assert bash_introduces_unpinned_dependency(command) is None

    def test_heredoc_dash_variant_not_blocked(self):
        command = "cat > install.sh <<-EOF\nnpm install lodash\nEOF\n"
        assert bash_introduces_unpinned_dependency(command) is None

    def test_real_command_before_heredoc_still_detected(self):
        command = "npm install lodash\ncat > f.sh <<EOF\nsome text\nEOF\n"
        assert bash_introduces_unpinned_dependency(command) is not None

    def test_real_command_after_heredoc_still_detected(self):
        command = "cat > f.sh <<EOF\nsome text\nEOF\nnpm install lodash\n"
        assert bash_introduces_unpinned_dependency(command) is not None

    @pytest.mark.parametrize(
        "command",
        [
            "echo hello or npm install foo",
            "echo hello and npm install foo",
            "echo command and control",
        ],
    )
    def test_bare_and_or_words_not_treated_as_separators(self, command):
        assert bash_introduces_unpinned_dependency(command) is None

    def test_real_and_operator_still_splits(self):
        assert (
            bash_introduces_unpinned_dependency("echo hi && npm install lodash")
            is not None
        )

    def test_real_or_operator_still_splits(self):
        assert (
            bash_introduces_unpinned_dependency("npm ci || npm install lodash")
            is not None
        )

    @pytest.mark.parametrize(
        "command",
        [
            "npm install ./local-package",
            "npm install ../sibling-package",
            "npm install /abs/path/to/package",
            "npm i ./local-package",
        ],
    )
    def test_local_path_npm_install_not_blocked(self, command):
        assert bash_introduces_unpinned_dependency(command) is None

    def test_local_path_alongside_real_unpinned_package_still_detected(self):
        assert (
            bash_introduces_unpinned_dependency(
                "npm install ./local-package lodash"
            )
            is not None
        )


class TestHookBashFalsePositivesIntegration:
    def test_heredoc_install_text_allowed_via_full_hook(self):
        rc, stdout, _ = _run_hook(
            _bash_payload("cat > install.sh <<'EOF'\nnpm install lodash\nEOF\n")
        )
        assert rc == 0
        assert not _is_denied(stdout)

    def test_bare_or_word_allowed_via_full_hook(self):
        rc, stdout, _ = _run_hook(_bash_payload("echo hello or npm install foo"))
        assert rc == 0
        assert not _is_denied(stdout)

    def test_local_path_npm_install_allowed_via_full_hook(self):
        rc, stdout, _ = _run_hook(_bash_payload("npm install ./local-package"))
        assert rc == 0
        assert not _is_denied(stdout)


# ---------------------------------------------------------------------------
# (e4) Manifest false positive: prose comments merely mentioning a range
# (independent-reviewer NEEDS-WORK #2.4)
# ---------------------------------------------------------------------------


class TestManifestCommentFalsePositive:
    def test_full_line_comment_mentioning_range_not_blocked(self):
        text = "# needs numpy>=1.20 installed separately\n"
        assert find_unpinned_range(text, is_package_json=False) is None

    def test_full_line_comment_with_leading_whitespace_not_blocked(self):
        text = "    # needs numpy>=1.20 installed separately\n"
        assert find_unpinned_range(text, is_package_json=False) is None

    def test_comment_alongside_real_dependency_line_still_blocked(self):
        text = (
            "# needs numpy>=1.20 installed separately\n"
            'dependencies = [\n    "mcp>=1.0.0",\n]\n'
        )
        match = find_unpinned_range(text, is_package_json=False)
        assert match is not None
        assert "mcp" in match

    def test_inline_trailing_comment_does_not_suppress_real_match(self):
        text = "mcp>=1.0.0  # some note\n"
        assert find_unpinned_range(text, is_package_json=False) is not None

    def test_strip_full_line_comments_helper(self):
        text = "# numpy>=1.20\nmcp==1.26.0\n"
        stripped = _strip_full_line_comments(text)
        assert "numpy" not in stripped
        assert "mcp==1.26.0" in stripped


class TestHookManifestCommentFalsePositiveIntegration:
    def test_edit_prose_comment_in_requirements_allowed(self):
        rc, stdout, _ = _run_hook(
            _edit_payload(
                "requirements.txt",
                "# needs numpy>=1.20 installed separately\n",
            )
        )
        assert rc == 0
        assert not _is_denied(stdout)

    def test_edit_prose_comment_in_pyproject_allowed(self):
        rc, stdout, _ = _run_hook(
            _edit_payload(
                "pyproject.toml",
                "# needs numpy>=1.20 installed separately\n",
            )
        )
        assert rc == 0
        assert not _is_denied(stdout)


# ---------------------------------------------------------------------------
# (f) LOBSTER_ALLOW_DEPENDENCY_CHANGE bypass
# ---------------------------------------------------------------------------


class TestOverrideEnvVar:
    def test_is_override_set_true(self, monkeypatch):
        monkeypatch.setenv(_ENV_OVERRIDE, "true")
        assert _is_override_set() is True

    def test_is_override_set_case_insensitive(self, monkeypatch):
        monkeypatch.setenv(_ENV_OVERRIDE, "True")
        assert _is_override_set() is True
        monkeypatch.setenv(_ENV_OVERRIDE, "TRUE")
        assert _is_override_set() is True

    def test_is_override_set_false_when_absent(self, monkeypatch):
        monkeypatch.delenv(_ENV_OVERRIDE, raising=False)
        assert _is_override_set() is False

    def test_is_override_not_set_for_other_values(self, monkeypatch):
        monkeypatch.setenv(_ENV_OVERRIDE, "1")
        assert _is_override_set() is False

    def test_override_does_not_bypass_via_lobster_debug(self, monkeypatch):
        """LOBSTER_DEBUG is a separate, unrelated variable and must not
        trigger the bypass (this is the whole point of using a dedicated
        env var per the module docstring)."""
        monkeypatch.delenv(_ENV_OVERRIDE, raising=False)
        monkeypatch.setenv("LOBSTER_DEBUG", "true")
        assert _is_override_set() is False


class TestHookOverrideBypass:
    def test_override_bypasses_edit_block(self):
        rc, stdout, _ = _run_hook(
            _edit_payload("pyproject.toml", 'dependencies = ["mcp>=1.0.0"]'),
            env={_ENV_OVERRIDE: "true"},
        )
        assert rc == 0
        assert not _is_denied(stdout)

    def test_override_bypasses_write_block(self):
        content = json.dumps({"dependencies": {"chokidar": "^3.6.0"}})
        rc, stdout, _ = _run_hook(
            _write_payload("package.json", content), env={_ENV_OVERRIDE: "true"}
        )
        assert rc == 0
        assert not _is_denied(stdout)

    def test_override_bypasses_bash_block(self):
        rc, stdout, _ = _run_hook(
            _bash_payload("npm install lodash"), env={_ENV_OVERRIDE: "true"}
        )
        assert rc == 0
        assert not _is_denied(stdout)

    def test_override_false_value_does_not_bypass(self):
        rc, stdout, _ = _run_hook(
            _bash_payload("npm install lodash"), env={_ENV_OVERRIDE: "false"}
        )
        assert rc == 0
        assert _is_denied(stdout)

    def test_override_absent_does_not_bypass(self):
        rc, stdout, _ = _run_hook(_bash_payload("npm install lodash"), env=None)
        assert rc == 0
        assert _is_denied(stdout)


# ---------------------------------------------------------------------------
# (g) Non-covered tools, malformed / missing stdin
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# (e5) Heredoc-masking regression: a heredoc body fed to a real interpreter
# (bash/sh/zsh/ssh/etc.) must still be scanned, even though a heredoc body
# used as inert file content (e.g. `cat > f.sh <<EOF`) stays exempt
# (independent-reviewer NEEDS-WORK, round 2: `bash <<EOF\npip install
# requests\nEOF` was silently allowed by the e3 heredoc fix above)
# ---------------------------------------------------------------------------


class TestHeredocInterpreterExecutionStillScanned:
    """The e3 fix (see TestBashFalsePositivesPureFunction above) made
    _mask_heredocs blank out every heredoc body unconditionally, to stop
    inert example text (`cat > f.sh <<EOF\\nnpm install lodash\\nEOF`) from
    being misread as a real command. But when the heredoc's body is piped
    into an actual shell/interpreter instead of a file, that body *is* real,
    executable shell code — masking it created a silent bypass. These cases
    must be BLOCKED."""

    @pytest.mark.parametrize(
        "command",
        [
            "bash <<EOF\npip install requests\nEOF\n",
            "bash <<'EOF'\npip install requests\nEOF\n",
            "sh <<EOF\npip install requests\nEOF\n",
            "zsh <<EOF\npip install requests\nEOF\n",
            "ssh myhost <<EOF\npip install requests\nEOF\n",
            "cat <<EOF | bash\npip install requests\nEOF\n",
        ],
    )
    def test_interpreter_heredoc_with_real_install_blocked(self, command):
        assert bash_introduces_unpinned_dependency(command) is not None

    def test_original_inert_text_heredoc_fix_not_regressed(self):
        """The exact case the e3 fix targeted (npm install text written as
        file content, not executed) must remain ALLOWED."""
        command = "cat > install.sh <<'EOF'\nnpm install lodash\nEOF\n"
        assert bash_introduces_unpinned_dependency(command) is None

    def test_interpreter_heredoc_with_pinned_install_still_allowed(self):
        """A heredoc fed to bash whose body is a real but already-pinned
        install must not be blocked — same rule as any other Bash command."""
        command = "bash <<EOF\npip install requests==2.31.0\nEOF\n"
        assert bash_introduces_unpinned_dependency(command) is None

    def test_tee_heredoc_still_treated_as_inert_file_content(self):
        """`tee` (like `cat >`) writes the heredoc body to a file rather
        than executing it — must stay exempt."""
        command = "tee script.sh <<EOF\nnpm install lodash\nEOF\n"
        assert bash_introduces_unpinned_dependency(command) is None


class TestHookHeredocInterpreterExecutionIntegration:
    @pytest.mark.parametrize(
        "command",
        [
            "bash <<EOF\npip install requests\nEOF\n",
            "sh <<EOF\npip install requests\nEOF\n",
            "zsh <<EOF\npip install requests\nEOF\n",
            "ssh myhost <<EOF\npip install requests\nEOF\n",
            "cat <<EOF | bash\npip install requests\nEOF\n",
        ],
    )
    def test_interpreter_heredoc_blocked_via_full_hook(self, command):
        rc, stdout, _ = _run_hook(_bash_payload(command))
        assert rc == 0
        assert _is_denied(stdout)

    def test_inert_heredoc_still_allowed_via_full_hook(self):
        rc, stdout, _ = _run_hook(
            _bash_payload("cat > install.sh <<'EOF'\nnpm install lodash\nEOF\n")
        )
        assert rc == 0
        assert not _is_denied(stdout)


# ---------------------------------------------------------------------------
# (e6) Round-5 bypass: interpreter `-c "<code>"` and here-strings (`<<<`)
# never got scanned at all — the whole thing was one sub-command starting
# with `bash`/`sh`/`zsh`, so the quoted/here-string install command inside
# was invisible to the install-detection regexes (independent review,
# PR #2151, issuecomment-5187771322).
# ---------------------------------------------------------------------------


class TestDashCAndHereStringBypassPureFunction:
    """The exact 5 bypass commands the round-4 independent review
    demonstrated as silently ALLOWED must now be BLOCKED, plus the nested
    wrapper-command forms (docker exec, sudo) that go through the same
    code path."""

    @pytest.mark.parametrize(
        "command",
        [
            'bash -c "pip install requests"',
            'bash <<< "pip install requests"',
            'sh -c "uv add requests"',
            'zsh -c "pip install requests"',
            'bash -c "npm install lodash"',
            'docker exec bash -c "pip install requests"',
            'sudo bash -c "npm install foo"',
        ],
    )
    def test_named_bypass_now_blocked(self, command):
        assert bash_introduces_unpinned_dependency(command) is not None, (
            f"expected {command!r} to be detected as an unpinned install"
        )

    @pytest.mark.parametrize(
        "command",
        [
            # Pinned installs through the same forms must stay ALLOWED.
            'bash -c "pip install requests==2.31.0"',
            'bash <<< "pip install requests==2.31.0"',
            'sh -c "uv add requests==2.31.0"',
            'zsh -c "pip install requests==2.31.0"',
            'bash -c "npm install lodash@4.17.21"',
            'docker exec bash -c "pip install requests==2.31.0"',
            'sudo bash -c "npm install foo@1.0.0"',
            # Lockfile-respecting forms through -c must also stay ALLOWED.
            'bash -c "npm ci"',
            'bash -c "uv sync"',
        ],
    )
    def test_pinned_or_lockfile_via_dash_c_or_here_string_not_blocked(self, command):
        assert bash_introduces_unpinned_dependency(command) is None

    def test_original_inert_heredoc_file_content_still_not_regressed(self):
        """The round-2 false positive this fix must not reintroduce: example
        install-command *text* used as file content in a heredoc (not
        executed) must remain ALLOWED."""
        command = "cat > f.sh <<EOF\npip install requests\nEOF\n"
        assert bash_introduces_unpinned_dependency(command) is None

    def test_interpreter_heredoc_execution_still_blocked(self):
        """Existing (round-4) heredoc-fed-to-a-real-interpreter detection
        must not regress."""
        command = "bash <<EOF\npip install requests\nEOF\n"
        assert bash_introduces_unpinned_dependency(command) is not None


class TestHookDashCAndHereStringBypassIntegration:
    @pytest.mark.parametrize(
        "command",
        [
            'bash -c "pip install requests"',
            'bash <<< "pip install requests"',
            'sh -c "uv add requests"',
            'zsh -c "pip install requests"',
            'bash -c "npm install lodash"',
            'docker exec bash -c "pip install requests"',
            'sudo bash -c "npm install foo"',
        ],
    )
    def test_named_bypass_blocked_via_full_hook(self, command):
        rc, stdout, _ = _run_hook(_bash_payload(command))
        assert rc == 0
        assert _is_denied(stdout)

    @pytest.mark.parametrize(
        "command",
        [
            'bash -c "pip install requests==2.31.0"',
            'bash <<< "pip install requests==2.31.0"',
            'bash -c "npm ci"',
        ],
    )
    def test_pinned_or_lockfile_via_dash_c_allowed_via_full_hook(self, command):
        rc, stdout, _ = _run_hook(_bash_payload(command))
        assert rc == 0
        assert not _is_denied(stdout)


class TestAdversarialPayloadVectorsPureFunction:
    """Vectors beyond the 5 named bypasses, exercised in the round-5
    self-review: eval, command substitution, and double-nested -c. These
    confirm the fix is a general "scan any code actually handed to an
    interpreter" mechanism, not a regex tuned to the 5 named cases."""

    @pytest.mark.parametrize(
        "command",
        [
            'eval "pip install requests"',
            "eval 'npm install lodash'",
            "$(pip install requests)",
            "`pip install requests`",
            # Double-nested -c: bash -> sh -> pip install requests
            'bash -c "sh -c \\"pip install requests\\""',
            # -c mixed with an unrelated heredoc elsewhere in the same
            # command: both must be independently evaluated.
            'bash -c "npm install lodash" && cat > f.sh <<EOF\nsome text\nEOF\n',
        ],
    )
    def test_adversarial_vector_blocked(self, command):
        assert bash_introduces_unpinned_dependency(command) is not None, (
            f"expected {command!r} to be detected as an unpinned install"
        )

    def test_double_nested_dash_c_pinned_not_blocked(self):
        command = 'bash -c "sh -c \\"pip install requests==2.31.0\\""'
        assert bash_introduces_unpinned_dependency(command) is None

    def test_eval_pinned_not_blocked(self):
        assert bash_introduces_unpinned_dependency('eval "pip install requests==2.31.0"') is None


# ---------------------------------------------------------------------------
# (e7) Round-5 self-review finding: `echo "<text>" | <interpreter>` /
# `printf "<text>" | <interpreter>` is the same "literal executed text"
# bug class as the here-string bypass, just spelled with a pipe. Found
# during the mandatory adversarial self-review (no external reviewer this
# round), not among the 5 named bypasses — demonstrates the fix generalizes
# rather than pattern-matching the named cases.
# ---------------------------------------------------------------------------


class TestEchoPrintfPipeBypassPureFunction:
    @pytest.mark.parametrize(
        "command",
        [
            'echo "pip install requests" | bash',
            'printf "pip install requests" | sh',
            'echo pip install requests | bash',
            'echo "npm install lodash" | zsh',
            'printf "uv add requests" | bash',
        ],
    )
    def test_echo_printf_pipe_to_interpreter_blocked(self, command):
        assert bash_introduces_unpinned_dependency(command) is not None, (
            f"expected {command!r} to be detected as an unpinned install"
        )

    @pytest.mark.parametrize(
        "command",
        [
            'echo "pip install requests==2.31.0" | bash',
            'echo hi | bash',
            'printf "just some text" | sh',
            'echo hi && npm ci',
            'echo hello or npm install foo',
        ],
    )
    def test_echo_printf_pipe_pinned_or_unrelated_not_blocked(self, command):
        assert bash_introduces_unpinned_dependency(command) is None

    def test_opaque_producer_piped_to_interpreter_is_a_documented_out_of_scope_gap(self):
        """`cat <unknown file> | bash` cannot be statically evaluated — the
        file's contents aren't known ahead of execution. This is a
        deliberate, disclosed limitation (see module docstring), not a
        regression: only a *literal* echo/printf argument is staticaly
        knowable."""
        assert bash_introduces_unpinned_dependency("cat unknownfile.sh | bash") is None


class TestHookEchoPrintfPipeBypassIntegration:
    @pytest.mark.parametrize(
        "command",
        [
            'echo "pip install requests" | bash',
            'echo pip install requests | bash',
        ],
    )
    def test_echo_printf_pipe_blocked_via_full_hook(self, command):
        rc, stdout, _ = _run_hook(_bash_payload(command))
        assert rc == 0
        assert _is_denied(stdout)


class TestHookGracefulHandling:
    @pytest.mark.parametrize("tool_name", ["Read", "Glob", "Grep", "Task", "TodoWrite"])
    def test_non_covered_tool_exits_0_no_deny(self, tool_name):
        rc, stdout, _ = _run_hook(
            {"tool_name": tool_name, "tool_input": {"file_path": "package.json"}}
        )
        assert rc == 0
        assert not _is_denied(stdout)

    def test_malformed_json_stdin_exits_0(self):
        rc, stdout, stderr = _run_hook_raw_stdin("not-valid-json{{{")
        assert rc == 0
        assert not _is_denied(stdout)

    def test_empty_stdin_exits_0(self):
        rc, stdout, _ = _run_hook_raw_stdin("")
        assert rc == 0
        assert not _is_denied(stdout)

    def test_missing_tool_input_exits_0(self):
        rc, stdout, _ = _run_hook({"tool_name": "Edit"})
        assert rc == 0
        assert not _is_denied(stdout)

    def test_edit_missing_file_path_exits_0(self):
        rc, stdout, _ = _run_hook(
            {"tool_name": "Edit", "tool_input": {"new_string": "foo>=1.0.0"}}
        )
        assert rc == 0
        assert not _is_denied(stdout)

    def test_bash_missing_command_exits_0(self):
        rc, stdout, _ = _run_hook({"tool_name": "Bash", "tool_input": {}})
        assert rc == 0
        assert not _is_denied(stdout)

    def test_notebook_edit_non_manifest_allowed(self):
        rc, stdout, _ = _run_hook(
            {
                "tool_name": "NotebookEdit",
                "tool_input": {"file_path": "notebook.ipynb", "new_source": "x = 1"},
            }
        )
        assert rc == 0
        assert not _is_denied(stdout)
