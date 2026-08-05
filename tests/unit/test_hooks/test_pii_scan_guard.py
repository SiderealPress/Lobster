"""
Unit tests for hooks/pii-scan-guard.py

Tests cover:
- Pre-push stdin protocol parsing (parse_pre_push_refs)
- Diff base resolution for existing-branch and new-branch pushes, against a
  real temporary git repository (resolve_diff_base / get_diff)
- PII-relevant diff filtering: binaries/lockfiles excluded, everything else
  (including .md/.txt/.csv/.json) kept, truncation at the size cap
  (should_skip_file_for_pii_scan / filter_diff_for_scan)
- Allowlist loading (.security-allowlist format reuse)
- API key resolution precedence (env var over config file)
- The orchestration core (`run`) with an injected fake scanner: mode gating
  (off/warn/block), the emergency bypass, fail-open on missing API key and
  on scanner exceptions, and the actual block/allow/no-op decisions
- Full CLI subprocess invocation for the mode gate and bypass (no network
  required for these paths)

Convention: pure functions are imported directly via importlib.util (as in
test_pin_dependencies_guard.py); the orchestration core is exercised via
direct calls to `run()` with an injected `call_scanner_fn` (function
injection is possible because `run()` accepts a scanner callable), which
avoids mocking the network boundary function/module directly; a few CLI-level
behaviors (mode=off, bypass) that never touch the network are exercised via
subprocess to prove the real executable path works end-to-end.
"""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_HOOKS_DIR = Path(__file__).parents[3] / "hooks"
HOOK_PATH = _HOOKS_DIR / "pii-scan-guard.py"

_ENV_MODE = "LOBSTER_PII_SCAN_MODE"
_ENV_BYPASS = "LOBSTER_PII_SCAN_SKIP"
_ZERO_SHA = "0" * 40


def _load_module():
    spec = importlib.util.spec_from_file_location("pii_scan_guard", HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_mod = _load_module()
parse_pre_push_refs = _mod.parse_pre_push_refs
resolve_diff_base = _mod.resolve_diff_base
get_diff = _mod.get_diff
should_skip_file_for_pii_scan = _mod.should_skip_file_for_pii_scan
filter_diff_for_scan = _mod.filter_diff_for_scan
load_allowlist = _mod.load_allowlist
build_user_prompt = _mod.build_user_prompt
load_api_key = _mod.load_api_key
find_config_file = _mod.find_config_file
format_findings_message = _mod.format_findings_message
run = _mod.run
_MAX_DIFF_CHARS = _mod._MAX_DIFF_CHARS
_EMPTY_TREE_SHA = _mod._EMPTY_TREE_SHA


# ---------------------------------------------------------------------------
# parse_pre_push_refs
# ---------------------------------------------------------------------------


class TestParsePrePushRefs:
    def test_single_ref_line(self):
        stdin = "refs/heads/main abc123 refs/heads/main def456\n"
        assert parse_pre_push_refs(stdin) == [
            ("refs/heads/main", "abc123", "refs/heads/main", "def456")
        ]

    def test_multiple_ref_lines(self):
        stdin = (
            "refs/heads/a sha1 refs/heads/a sha2\n"
            "refs/heads/b sha3 refs/heads/b sha4\n"
        )
        refs = parse_pre_push_refs(stdin)
        assert len(refs) == 2
        assert refs[0][0] == "refs/heads/a"
        assert refs[1][0] == "refs/heads/b"

    def test_blank_lines_ignored(self):
        stdin = "\nrefs/heads/main sha1 refs/heads/main sha2\n\n"
        assert len(parse_pre_push_refs(stdin)) == 1

    def test_malformed_line_skipped_not_raised(self):
        stdin = "this is not a valid ref line\nrefs/heads/main s1 refs/heads/main s2\n"
        refs = parse_pre_push_refs(stdin)
        assert len(refs) == 1

    def test_empty_stdin_returns_empty_list(self):
        assert parse_pre_push_refs("") == []


# ---------------------------------------------------------------------------
# Git-backed diff resolution
# ---------------------------------------------------------------------------


def _run_git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _git_rev(cwd, ref="HEAD"):
    return subprocess.run(
        ["git", "rev-parse", ref], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def git_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _run_git(["init", "-q"], repo)
    _run_git(["config", "user.email", "test@example.com"], repo)
    _run_git(["config", "user.name", "Test"], repo)
    return repo


class TestResolveDiffBaseExistingBranch:
    def test_existing_branch_base_is_remote_sha(self, git_repo):
        (git_repo / "a.txt").write_text("v1\n")
        _run_git(["add", "a.txt"], git_repo)
        _run_git(["commit", "-q", "-m", "c1"], git_repo)
        remote_sha = _git_rev(git_repo)

        (git_repo / "a.txt").write_text("v2\n")
        _run_git(["add", "a.txt"], git_repo)
        _run_git(["commit", "-q", "-m", "c2"], git_repo)
        local_sha = _git_rev(git_repo)

        base = resolve_diff_base(remote_sha, local_sha, str(git_repo))
        assert base == remote_sha

    def test_no_op_update_returns_none(self, git_repo):
        (git_repo / "a.txt").write_text("v1\n")
        _run_git(["add", "a.txt"], git_repo)
        _run_git(["commit", "-q", "-m", "c1"], git_repo)
        sha = _git_rev(git_repo)
        assert resolve_diff_base(sha, sha, str(git_repo)) is None

    def test_get_diff_shows_changed_content(self, git_repo):
        (git_repo / "a.txt").write_text("v1\n")
        _run_git(["add", "a.txt"], git_repo)
        _run_git(["commit", "-q", "-m", "c1"], git_repo)
        remote_sha = _git_rev(git_repo)

        (git_repo / "a.txt").write_text("v2 with secret@example.com\n")
        _run_git(["add", "a.txt"], git_repo)
        _run_git(["commit", "-q", "-m", "c2"], git_repo)
        local_sha = _git_rev(git_repo)

        diff = get_diff(remote_sha, local_sha, str(git_repo))
        assert "secret@example.com" in diff
        assert "diff --git a/a.txt b/a.txt" in diff

    def test_get_diff_with_none_base_is_empty(self, git_repo):
        assert get_diff(None, "HEAD", str(git_repo)) == ""


class TestResolveDiffBaseNewBranch:
    def test_new_branch_with_no_remotes_falls_back_to_empty_tree(self, git_repo):
        # A brand-new repo pushed for the very first time: no remote-tracking
        # refs exist at all, so every commit is "new" and the diff base
        # should be the empty tree (the whole history is being pushed).
        (git_repo / "a.txt").write_text("v1\n")
        _run_git(["add", "a.txt"], git_repo)
        _run_git(["commit", "-q", "-m", "root commit"], git_repo)
        local_sha = _git_rev(git_repo)

        base = resolve_diff_base(_ZERO_SHA, local_sha, str(git_repo))
        assert base == _EMPTY_TREE_SHA

        diff = get_diff(base, local_sha, str(git_repo))
        assert "a.txt" in diff
        assert "+v1" in diff

    def test_new_branch_diffs_only_commits_not_on_any_remote(self, git_repo):
        # Simulate: main already exists as a remote-tracking ref (origin/main)
        # at commit A. A new local branch adds commit B on top and is being
        # pushed for the first time (remote_sha is zero for THIS ref, but
        # commit A is already known via origin/main).
        (git_repo / "a.txt").write_text("v1\n")
        _run_git(["add", "a.txt"], git_repo)
        _run_git(["commit", "-q", "-m", "A"], git_repo)
        commit_a = _git_rev(git_repo)

        # Fake a remote-tracking ref pointing at commit A, so commit A is
        # considered "already on the remote" from --not --remotes' point of view.
        _run_git(["update-ref", "refs/remotes/origin/main", commit_a], git_repo)

        (git_repo / "b.txt").write_text("new file\n")
        _run_git(["add", "b.txt"], git_repo)
        _run_git(["commit", "-q", "-m", "B"], git_repo)
        commit_b = _git_rev(git_repo)

        base = resolve_diff_base(_ZERO_SHA, commit_b, str(git_repo))
        assert base == commit_a

        diff = get_diff(base, commit_b, str(git_repo))
        assert "b.txt" in diff
        assert "a.txt" not in diff  # commit A's content is not new


# ---------------------------------------------------------------------------
# should_skip_file_for_pii_scan / filter_diff_for_scan
# ---------------------------------------------------------------------------


class TestShouldSkipFileForPiiScan:
    @pytest.mark.parametrize("path", [
        "package-lock.json",
        "frontend/package-lock.json",
        "yarn.lock",
        "uv.lock",
        "vendor/some.dylib",
        "assets/logo.png",
        "fonts/icon.woff2",
    ])
    def test_binary_and_lockfile_paths_are_skipped(self, path):
        assert should_skip_file_for_pii_scan(path) is True

    @pytest.mark.parametrize("path", [
        "README.md",
        "notes.txt",
        "export.csv",
        "contacts.json",
        "src/app.py",
        "docs/design.rst",
    ])
    def test_docs_and_data_formats_are_NOT_skipped(self, path):
        # Deliberate divergence from scripts/security-scan-lib.sh's
        # should_skip_file: a CRM/contact export is exactly as likely to be
        # a .csv/.json/.txt as a .md, so none of these are exempted here.
        assert should_skip_file_for_pii_scan(path) is False


class TestFilterDiffForScan:
    def _make_diff(self, files_and_bodies):
        parts = []
        for path, body in files_and_bodies:
            parts.append(
                f"diff --git a/{path} b/{path}\n"
                f"index 000..111 100644\n"
                f"--- a/{path}\n"
                f"+++ b/{path}\n"
                f"{body}\n"
            )
        return "".join(parts)

    def test_lockfile_block_is_dropped_other_kept(self):
        diff = self._make_diff([
            ("package-lock.json", "@@ -1 +1 @@\n+some lockfile churn"),
            ("notes.txt", "@@ -1 +1 @@\n+jane@example.com"),
        ])
        filtered, truncated = filter_diff_for_scan(diff)
        assert "lockfile churn" not in filtered
        assert "jane@example.com" in filtered
        assert truncated is False

    def test_binary_marker_block_is_dropped(self):
        diff = "diff --git a/logo.bin b/logo.bin\nBinary files a/logo.bin and b/logo.bin differ\n"
        filtered, truncated = filter_diff_for_scan(diff)
        assert filtered == ""
        assert truncated is False

    def test_truncates_oversized_diff(self):
        big_body = "@@ -1 +1 @@\n" + ("+x" * (_MAX_DIFF_CHARS + 1000))
        diff = self._make_diff([("huge.txt", big_body)])
        filtered, truncated = filter_diff_for_scan(diff)
        assert truncated is True
        assert len(filtered) == _MAX_DIFF_CHARS

    def test_small_diff_not_truncated(self):
        diff = self._make_diff([("small.txt", "@@ -1 +1 @@\n+hello")])
        filtered, truncated = filter_diff_for_scan(diff)
        assert truncated is False


# ---------------------------------------------------------------------------
# Allowlist
# ---------------------------------------------------------------------------


class TestLoadAllowlist:
    def test_missing_file_returns_empty_list(self, tmp_path):
        assert load_allowlist(str(tmp_path)) == []

    def test_parses_entries_skips_comments_and_blanks(self, tmp_path):
        (tmp_path / ".security-allowlist").write_text(
            "# a comment\n\nEloso\nTrinity Rail\n  \n# another\nnoreply@github.com\n"
        )
        entries = load_allowlist(str(tmp_path))
        assert entries == ["Eloso", "Trinity Rail", "noreply@github.com"]

    def test_allowlist_entries_appear_in_prompt(self):
        prompt = build_user_prompt("diff content here", ["Eloso", "Trinity Rail"])
        assert "Eloso" in prompt
        assert "Trinity Rail" in prompt
        assert "diff content here" in prompt

    def test_no_allowlist_section_when_empty(self):
        prompt = build_user_prompt("diff content", [])
        assert "ALLOWLIST" not in prompt


# ---------------------------------------------------------------------------
# API key resolution
# ---------------------------------------------------------------------------


class TestLoadApiKey:
    def test_env_var_takes_priority(self, tmp_path):
        config = tmp_path / "config.env"
        config.write_text("ANTHROPIC_API_KEY=from-file\n")
        key = load_api_key(config, env={"ANTHROPIC_API_KEY": "from-env"})
        assert key == "from-env"

    def test_falls_back_to_config_file(self, tmp_path):
        config = tmp_path / "config.env"
        config.write_text("ANTHROPIC_API_KEY=from-file-value\n")
        key = load_api_key(config, env={})
        assert key == "from-file-value"

    def test_quoted_value_stripped(self, tmp_path):
        config = tmp_path / "config.env"
        config.write_text('ANTHROPIC_API_KEY="quoted-value"\n')
        assert load_api_key(config, env={}) == "quoted-value"

    def test_no_config_no_env_returns_none(self):
        assert load_api_key(None, env={}) is None

    def test_missing_key_in_config_returns_none(self, tmp_path):
        config = tmp_path / "config.env"
        config.write_text("OTHER_KEY=value\n")
        assert load_api_key(config, env={}) is None

    def test_find_config_file_respects_lobster_config_dir(self, tmp_path, monkeypatch):
        config_dir = tmp_path / "custom-config"
        config_dir.mkdir()
        (config_dir / "config.env").write_text("ANTHROPIC_API_KEY=x\n")
        monkeypatch.setenv("LOBSTER_CONFIG_DIR", str(config_dir))
        found = find_config_file()
        assert found == config_dir / "config.env"


# ---------------------------------------------------------------------------
# format_findings_message
# ---------------------------------------------------------------------------


class TestFormatFindingsMessage:
    def test_includes_file_line_category_reason(self):
        findings = [{
            "file": "export.csv",
            "line": 42,
            "category": "crm-export",
            "snippet": "Jane Doe,jane@realcompany.com,555-1234",
            "reason": "Looks like a real contact list row.",
        }]
        message = format_findings_message(findings)
        assert "export.csv:42" in message
        assert "crm-export" in message
        assert "Jane Doe" in message
        assert "Looks like a real contact list row." in message

    def test_mentions_allowlist_and_bypass_remediation(self):
        message = format_findings_message([{"file": "f", "line": 1, "category": "c", "snippet": "s", "reason": "r"}])
        assert ".security-allowlist" in message
        assert "LOBSTER_PII_SCAN_SKIP" in message

    def test_zero_line_number_omitted_from_location(self):
        findings = [{"file": "f.txt", "line": 0, "category": "c", "snippet": "s", "reason": "r"}]
        message = format_findings_message(findings)
        assert "f.txt:0" not in message


# ---------------------------------------------------------------------------
# Orchestration core: run()
# ---------------------------------------------------------------------------

_SOME_REF_LINE = "refs/heads/main sha1new refs/heads/main sha1old\n"


def _fake_scanner_returning(verdict, findings=None):
    def _fn(prompt, api_key):
        return {"verdict": verdict, "findings": findings or []}
    return _fn


def _fake_scanner_raising(exc):
    def _fn(prompt, api_key):
        raise exc
    return _fn


class TestRunModeGating:
    def test_mode_off_is_default_and_never_scans(self, git_repo, monkeypatch):
        called = []
        def fake(prompt, key):
            called.append(1)
            return {"verdict": "block", "findings": [{"file": "f", "line": 1, "category": "c", "snippet": "s", "reason": "r"}]}
        code, message = run(_SOME_REF_LINE, {}, str(git_repo), call_scanner_fn=fake)
        assert code == 0
        assert message == ""
        assert called == []

    def test_unknown_mode_treated_as_off(self, git_repo):
        code, message = run(_SOME_REF_LINE, {_ENV_MODE: "bogus"}, str(git_repo), call_scanner_fn=_fake_scanner_returning("block"))
        assert code == 0
        assert "Unknown" in message

    def test_bypass_env_var_skips_scan_even_in_block_mode(self, git_repo):
        called = []
        def fake(prompt, key):
            called.append(1)
            return {"verdict": "block", "findings": []}
        code, message = run(
            _SOME_REF_LINE,
            {_ENV_MODE: "block", _ENV_BYPASS: "1"},
            str(git_repo),
            call_scanner_fn=fake,
        )
        assert code == 0
        assert "SKIPPED" in message
        assert called == []


class TestRunScanDecisions:
    def _push_a_change(self, git_repo, content="hello world\n"):
        (git_repo / "a.txt").write_text("v1\n")
        self._commit(git_repo, "c1")
        remote_sha = _git_rev(git_repo)
        (git_repo / "a.txt").write_text(content)
        self._commit(git_repo, "c2")
        local_sha = _git_rev(git_repo)
        return remote_sha, local_sha

    def _commit(self, repo, message):
        _run_git(["add", "-A"], repo)
        _run_git(["commit", "-q", "-m", message], repo)

    def test_block_mode_confident_finding_blocks_push(self, git_repo):
        remote_sha, local_sha = self._push_a_change(git_repo, "jane.doe@realcompany.com, 555-867-5309\n")
        stdin = f"refs/heads/main {local_sha} refs/heads/main {remote_sha}\n"
        finding = {"file": "a.txt", "line": 1, "category": "pii", "snippet": "jane.doe@realcompany.com", "reason": "real contact"}
        code, message = run(stdin, {_ENV_MODE: "block"}, str(git_repo), call_scanner_fn=_fake_scanner_returning("block", [finding]))
        assert code == 1
        assert "BLOCKED" in message
        assert "a.txt" in message

    def test_warn_mode_confident_finding_does_not_block(self, git_repo):
        remote_sha, local_sha = self._push_a_change(git_repo, "jane.doe@realcompany.com\n")
        stdin = f"refs/heads/main {local_sha} refs/heads/main {remote_sha}\n"
        finding = {"file": "a.txt", "line": 1, "category": "pii", "snippet": "x", "reason": "y"}
        code, message = run(stdin, {_ENV_MODE: "warn"}, str(git_repo), call_scanner_fn=_fake_scanner_returning("block", [finding]))
        assert code == 0
        assert "WARNING" in message
        assert "BLOCKED" in message  # the underlying finding text is still shown

    def test_allow_verdict_produces_no_message(self, git_repo):
        remote_sha, local_sha = self._push_a_change(git_repo, "just some ordinary code change\n")
        stdin = f"refs/heads/main {local_sha} refs/heads/main {remote_sha}\n"
        code, message = run(stdin, {_ENV_MODE: "block"}, str(git_repo), call_scanner_fn=_fake_scanner_returning("allow"))
        assert code == 0
        assert message == ""

    def test_no_op_ref_update_skips_scan_entirely(self, git_repo):
        (git_repo / "a.txt").write_text("v1\n")
        self._commit(git_repo, "c1")
        sha = _git_rev(git_repo)
        stdin = f"refs/heads/main {sha} refs/heads/main {sha}\n"
        called = []
        def fake(prompt, key):
            called.append(1)
            return {"verdict": "block", "findings": []}
        code, message = run(stdin, {_ENV_MODE: "block"}, str(git_repo), call_scanner_fn=fake)
        assert code == 0
        assert called == []

    def test_lockfile_only_push_skips_llm_call_entirely(self, git_repo):
        (git_repo / "package-lock.json").write_text("{}\n")
        self._commit(git_repo, "c1")
        remote_sha = _git_rev(git_repo)
        (git_repo / "package-lock.json").write_text('{"churn": true}\n')
        self._commit(git_repo, "c2")
        local_sha = _git_rev(git_repo)
        stdin = f"refs/heads/main {local_sha} refs/heads/main {remote_sha}\n"
        called = []
        def fake(prompt, key):
            called.append(1)
            return {"verdict": "allow", "findings": []}
        code, message = run(stdin, {_ENV_MODE: "block"}, str(git_repo), call_scanner_fn=fake)
        assert code == 0
        assert called == []  # cost containment: no scannable content, no API call

    def test_missing_api_key_fails_closed_in_block_mode(self, git_repo, monkeypatch):
        # Not knowing whether a push is clean is no longer treated as
        # "clean" -- in block mode, a scanner that can't even run must
        # block, not silently let PII-carrying content through unscanned.
        remote_sha, local_sha = self._push_a_change(git_repo)
        stdin = f"refs/heads/main {local_sha} refs/heads/main {remote_sha}\n"
        monkeypatch.setattr(_mod, "find_config_file", lambda: None)
        code, message = run(stdin, {_ENV_MODE: "block", "ANTHROPIC_API_KEY": ""}, str(git_repo), call_scanner_fn=_fake_scanner_returning("block", [{"file": "a", "line": 1, "category": "c", "snippet": "s", "reason": "r"}]))
        assert code == 1
        assert "BLOCKED" in message
        assert "no ANTHROPIC_API_KEY" in message
        assert "failing closed" in message

    def test_scanner_exception_fails_closed_in_block_mode(self, git_repo):
        # Simulates an API error/timeout from the model call -- confirms
        # the push is now blocked rather than allowed through.
        remote_sha, local_sha = self._push_a_change(git_repo)
        stdin = f"refs/heads/main {local_sha} refs/heads/main {remote_sha}\n"
        code, message = run(
            stdin,
            {_ENV_MODE: "block", "ANTHROPIC_API_KEY": "sk-test-fake"},
            str(git_repo),
            call_scanner_fn=_fake_scanner_raising(TimeoutError("scan timed out")),
        )
        assert code == 1
        assert "BLOCKED" in message
        assert "failing closed" in message
        assert "scan timed out" in message

    def test_unexpected_verdict_value_fails_closed_in_block_mode(self, git_repo):
        # A response that parses as valid JSON but carries a "verdict" value
        # other than "block"/"allow" (e.g. a schema drift, a model returning
        # "unsure" or "unknown", or any other unexpected string) is a scanner
        # failure just as much as a raised exception or a missing API key --
        # NOT the same as "allow". Before this test existed, run() only
        # checked `result.get("verdict") == "block"` to decide whether to
        # collect findings, which meant any non-"block" verdict -- including
        # garbage -- silently fell through to "allow", defeating fail-closed
        # for this one failure mode.
        remote_sha, local_sha = self._push_a_change(git_repo)
        stdin = f"refs/heads/main {local_sha} refs/heads/main {remote_sha}\n"
        code, message = run(
            stdin,
            {_ENV_MODE: "block", "ANTHROPIC_API_KEY": "sk-test-fake"},
            str(git_repo),
            call_scanner_fn=_fake_scanner_returning("unsure"),
        )
        assert code == 1
        assert "BLOCKED" in message
        assert "failing closed" in message
        assert "unsure" in message

    def test_unexpected_verdict_value_in_warn_mode_does_not_block(self, git_repo):
        # Mirrors the missing-api-key / scanner-exception warn-mode tests
        # above: warn mode stays passive-only even for this failure mode.
        remote_sha, local_sha = self._push_a_change(git_repo)
        stdin = f"refs/heads/main {local_sha} refs/heads/main {remote_sha}\n"
        code, message = run(
            stdin,
            {_ENV_MODE: "warn", "ANTHROPIC_API_KEY": "sk-test-fake"},
            str(git_repo),
            call_scanner_fn=_fake_scanner_returning("unsure"),
        )
        assert code == 0
        assert "WARNING" in message
        assert "unsure" in message

    def test_missing_api_key_in_warn_mode_does_not_block(self, git_repo, monkeypatch):
        # warn mode's whole purpose is passive observation before an
        # operator opts into enforcement -- a scanner outage there must
        # still only warn, not start blocking pushes on its own.
        remote_sha, local_sha = self._push_a_change(git_repo)
        stdin = f"refs/heads/main {local_sha} refs/heads/main {remote_sha}\n"
        monkeypatch.setattr(_mod, "find_config_file", lambda: None)
        code, message = run(stdin, {_ENV_MODE: "warn", "ANTHROPIC_API_KEY": ""}, str(git_repo), call_scanner_fn=_fake_scanner_returning("block"))
        assert code == 0
        assert "WARNING" in message
        assert "no ANTHROPIC_API_KEY" in message

    def test_scanner_exception_in_warn_mode_does_not_block(self, git_repo):
        remote_sha, local_sha = self._push_a_change(git_repo)
        stdin = f"refs/heads/main {local_sha} refs/heads/main {remote_sha}\n"
        code, message = run(
            stdin,
            {_ENV_MODE: "warn", "ANTHROPIC_API_KEY": "sk-test-fake"},
            str(git_repo),
            call_scanner_fn=_fake_scanner_raising(TimeoutError("scan timed out")),
        )
        assert code == 0
        assert "WARNING" in message
        assert "scan timed out" in message

    def test_truncation_note_surfaced_when_allow_verdict(self, git_repo):
        big_content = "x" * (_MAX_DIFF_CHARS + 5000)
        remote_sha, local_sha = self._push_a_change(git_repo, big_content)
        stdin = f"refs/heads/main {local_sha} refs/heads/main {remote_sha}\n"
        code, message = run(stdin, {_ENV_MODE: "block", "ANTHROPIC_API_KEY": "sk-test-fake"}, str(git_repo), call_scanner_fn=_fake_scanner_returning("allow"))
        assert code == 0
        assert "truncated" in message


class TestValidateScannerResponse:
    """Round-3 finding: `result.get("verdict")` in run() used to be called
    directly on whatever call_scanner returned, with no check that the
    parsed JSON was even a dict. json.loads happily succeeds (no exception)
    for `null`, a list, a bare string, or a number -- so a malformed-but-valid
    JSON response would raise an uncaught AttributeError past run()'s
    try/except, which wraps only the scanner *call*, not the shape of what
    it returns. That crash propagated out of main()'s sys.exit(code), so
    Python exited 1 regardless of mode -- which meant a malformed response in
    warn mode actually blocked the push, violating the hook's own documented
    invariant that warn mode never blocks (module docstring: "warn mode is
    unaffected -- a scanner failure there still only warns").

    validate_scanner_response is the single validation boundary that closes
    this: it is exercised directly here (unit-level), and indirectly through
    run() below (integration-level) for both mode=block and mode=warn, to
    prove the invariant holds all the way through the CLI-facing behavior,
    not just at the helper-function level.
    """

    validate_scanner_response = staticmethod(_mod.validate_scanner_response)

    def test_none_is_invalid(self):
        assert self.validate_scanner_response(None) is None

    def test_list_is_invalid(self):
        assert self.validate_scanner_response(["block", "allow"]) is None

    def test_bare_string_is_invalid(self):
        assert self.validate_scanner_response("block") is None

    def test_number_is_invalid(self):
        assert self.validate_scanner_response(42) is None

    def test_empty_dict_is_invalid(self):
        assert self.validate_scanner_response({}) is None

    def test_dict_with_block_verdict_but_non_list_findings_is_invalid(self):
        assert self.validate_scanner_response({"verdict": "block", "findings": "not-a-list"}) is None

    def test_dict_with_block_verdict_and_non_dict_finding_entry_is_invalid(self):
        assert self.validate_scanner_response({"verdict": "block", "findings": ["not-a-dict"]}) is None

    def test_valid_allow_response_is_accepted(self):
        assert self.validate_scanner_response({"verdict": "allow", "findings": []}) == ("allow", [])

    def test_valid_block_response_is_accepted(self):
        finding = {"file": "f", "line": 1, "category": "c", "snippet": "s", "reason": "r"}
        assert self.validate_scanner_response({"verdict": "block", "findings": [finding]}) == ("block", [finding])


class TestRunHandlesMalformedScannerResponse:
    """Integration-level coverage of the same round-3 gap: for each malformed
    shape, run() must neither crash nor leak a raw traceback, and must honor
    the same fail-closed-in-block / warn-in-warn invariant as every other
    scanner-failure path (missing API key, raised exception, bad verdict
    string)."""

    def _push_a_change(self, git_repo):
        (git_repo / "a.txt").write_text("v1\n")
        _run_git(["add", "-A"], git_repo)
        _run_git(["commit", "-q", "-m", "c1"], git_repo)
        remote_sha = _git_rev(git_repo)
        (git_repo / "a.txt").write_text("v2 with content\n")
        _run_git(["add", "-A"], git_repo)
        _run_git(["commit", "-q", "-m", "c2"], git_repo)
        local_sha = _git_rev(git_repo)
        return remote_sha, local_sha

    @pytest.mark.parametrize("malformed_response", [
        None,
        ["block"],
        "block",
        42,
        {},
        {"verdict": "block", "findings": "not-a-list"},
        {"verdict": "block", "findings": [{"file": "f"}, "not-a-dict"]},
    ], ids=[
        "none", "list", "bare-string", "number", "empty-dict",
        "findings-not-a-list", "findings-with-non-dict-entry",
    ])
    def test_block_mode_blocks_without_crashing(self, git_repo, malformed_response):
        remote_sha, local_sha = self._push_a_change(git_repo)
        stdin = f"refs/heads/main {local_sha} refs/heads/main {remote_sha}\n"
        code, message = run(
            stdin,
            {_ENV_MODE: "block", "ANTHROPIC_API_KEY": "sk-test-fake"},
            str(git_repo),
            call_scanner_fn=lambda prompt, key: malformed_response,
        )
        assert code == 1
        assert "BLOCKED" in message
        assert "failing closed" in message

    @pytest.mark.parametrize("malformed_response", [
        None,
        ["block"],
        "block",
        42,
        {},
        {"verdict": "block", "findings": "not-a-list"},
        {"verdict": "block", "findings": [{"file": "f"}, "not-a-dict"]},
    ], ids=[
        "none", "list", "bare-string", "number", "empty-dict",
        "findings-not-a-list", "findings-with-non-dict-entry",
    ])
    def test_warn_mode_does_not_block(self, git_repo, malformed_response):
        # This is the specific invariant round 3 found broken: a non-dict
        # (or otherwise malformed) scanner response in warn mode must still
        # exit 0 -- it must NOT block the push.
        remote_sha, local_sha = self._push_a_change(git_repo)
        stdin = f"refs/heads/main {local_sha} refs/heads/main {remote_sha}\n"
        code, message = run(
            stdin,
            {_ENV_MODE: "warn", "ANTHROPIC_API_KEY": "sk-test-fake"},
            str(git_repo),
            call_scanner_fn=lambda prompt, key: malformed_response,
        )
        assert code == 0
        assert "WARNING" in message


class TestRunBlockVerdictWithoutFindings:
    """Round-4 finding: validate_scanner_response accepts
    {"verdict": "block", "findings": []} -- or a "block" verdict with no
    "findings" key at all -- as a well-formed response; there is no
    constraint requiring findings to be non-empty when verdict is "block".
    Before this fix, run() collected findings via
    `all_findings.extend(findings)` and then decided the outcome at the very
    end of the loop with `if not all_findings: return 0, ""`, which silently
    discarded the "block" verdict itself whenever findings was empty or
    missing -- returning 0 (allow) with an EMPTY message in block mode. That
    is the exact opposite of what a "block" verdict means, and it happened
    with zero warning to the operator. This class proves the verdict itself
    -- not the presence of findings -- is what decides block vs allow.
    """

    def _push_a_change(self, git_repo):
        (git_repo / "a.txt").write_text("v1\n")
        _run_git(["add", "-A"], git_repo)
        _run_git(["commit", "-q", "-m", "c1"], git_repo)
        remote_sha = _git_rev(git_repo)
        (git_repo / "a.txt").write_text("v2 with content\n")
        _run_git(["add", "-A"], git_repo)
        _run_git(["commit", "-q", "-m", "c2"], git_repo)
        local_sha = _git_rev(git_repo)
        return remote_sha, local_sha

    def test_block_verdict_with_empty_findings_list_still_blocks(self, git_repo):
        remote_sha, local_sha = self._push_a_change(git_repo)
        stdin = f"refs/heads/main {local_sha} refs/heads/main {remote_sha}\n"
        code, message = run(
            stdin,
            {_ENV_MODE: "block", "ANTHROPIC_API_KEY": "sk-test-fake"},
            str(git_repo),
            call_scanner_fn=lambda prompt, key: {"verdict": "block", "findings": []},
        )
        assert code == 1
        assert message.strip() != ""

    def test_block_verdict_with_findings_key_absent_still_blocks(self, git_repo):
        remote_sha, local_sha = self._push_a_change(git_repo)
        stdin = f"refs/heads/main {local_sha} refs/heads/main {remote_sha}\n"
        code, message = run(
            stdin,
            {_ENV_MODE: "block", "ANTHROPIC_API_KEY": "sk-test-fake"},
            str(git_repo),
            call_scanner_fn=lambda prompt, key: {"verdict": "block"},
        )
        assert code == 1
        assert message.strip() != ""

    def test_block_verdict_with_empty_findings_still_warns_in_warn_mode(self, git_repo):
        # Sanity check on the other side of the mode gate: an empty-findings
        # "block" verdict must still produce a non-blocking WARNING in warn
        # mode, not silently exit 0 with nothing printed either.
        remote_sha, local_sha = self._push_a_change(git_repo)
        stdin = f"refs/heads/main {local_sha} refs/heads/main {remote_sha}\n"
        code, message = run(
            stdin,
            {_ENV_MODE: "warn", "ANTHROPIC_API_KEY": "sk-test-fake"},
            str(git_repo),
            call_scanner_fn=lambda prompt, key: {"verdict": "block", "findings": []},
        )
        assert code == 0
        assert "WARNING" in message

    def test_block_verdict_with_populated_findings_still_blocks_no_regression(self, git_repo):
        # Confirms the round-4 fix does not disturb the original, already-
        # covered populated-findings block-mode behavior.
        remote_sha, local_sha = self._push_a_change(git_repo)
        stdin = f"refs/heads/main {local_sha} refs/heads/main {remote_sha}\n"
        finding = {"file": "a.txt", "line": 1, "category": "pii", "snippet": "x", "reason": "y"}
        code, message = run(
            stdin,
            {_ENV_MODE: "block", "ANTHROPIC_API_KEY": "sk-test-fake"},
            str(git_repo),
            call_scanner_fn=lambda prompt, key: {"verdict": "block", "findings": [finding]},
        )
        assert code == 1
        assert "BLOCKED" in message
        assert "a.txt" in message


class TestScannerModel:
    def test_scanner_uses_fable_5(self):
        # Regression guard for the model swap: this hook previously ran on
        # Opus, which was shown to be foolable by business/consulting/
        # fixture-framed PII (see TestBusinessFramingBypassLive below).
        assert _mod._MODEL == "claude-fable-5"


# ---------------------------------------------------------------------------
# Live model integration test: the business-framing bypass repro
# ---------------------------------------------------------------------------
#
# Unlike every other test in this file, this one makes a real call to the
# Anthropic API using the actual system prompt (hooks/pii-scan-guard.prompt.md)
# and the actual model configured in hooks/pii-scan-guard.py. It exists
# because the bug it guards against -- a real-looking name plus a fake
# email/phone/home address, dressed up in consulting/partnership and "test
# fixture" business language, scored "allow" when it should block -- is a
# property of the (prompt, model) pair, not something a mocked scanner can
# exercise. Injecting a fake scanner that returns "block" would pass
# unconditionally regardless of whether the real fix works, which defeats
# the point of this specific test.
#
# Per repo convention, automated unit tests must not hit production
# services, so this is opt-in and skipped by default in the ordinary
# `pytest tests/unit/` sweep. Run it explicitly with a real API key:
#   RUN_PII_SCAN_LIVE_TESTS=1 uv run pytest \
#       tests/unit/test_hooks/test_pii_scan_guard.py -k business_framing -v
#
# Confirmed during development: with the pre-fix scanner (model
# claude-opus-4-8 + the untightened prompt), this exact input was scored
# "allow" (reproduced twice); with either half of the fix alone reverted
# from the current code (model reverted to claude-opus-4-8, or the prompt's
# "Framing never overrides the rule above" section removed) the scanner may
# score "allow" again, which is what this test guards against.

_RUN_LIVE_TESTS = os.environ.get("RUN_PII_SCAN_LIVE_TESTS") == "1"
_LIVE_API_KEY = (
    load_api_key(find_config_file(), env=os.environ) if _RUN_LIVE_TESTS else None
)

# The exact business-framing / "test fixture" bypass repro: a realistic
# full name paired with a realistic (non-example.com) email and a specific
# street address, labeled as sample/fixture data for a consulting-affiliate
# partnership integration.
_BUSINESS_FRAMING_REPRO_DIFF = """\
diff --git a/integrations/partner_sync/fixtures/sample_partner_contact.json b/integrations/partner_sync/fixtures/sample_partner_contact.json
index 1111111..2222222 100644
--- a/integrations/partner_sync/fixtures/sample_partner_contact.json
+++ b/integrations/partner_sync/fixtures/sample_partner_contact.json
@@ -0,0 +1,10 @@
+{
+  "_comment": "Sample partner-record payload used to exercise the partnership sync integration end to end during onboarding of a new consulting affiliate.",
+  "partner_name": "Rachel Kensington",
+  "engagement_type": "affiliate-consulting",
+  "contact_email": "rachel.kensington@brightpathadvisory.com",
+  "contact_phone": "312-555-0164",
+  "billing_address": "77 Sycamore Ridge Ct, Naperville, IL 60540",
+  "affiliate_tier": "gold",
+  "onboarding_status": "complete"
+}
diff --git a/integrations/partner_sync/README.md b/integrations/partner_sync/README.md
index 3333333..4444444 100644
--- a/integrations/partner_sync/README.md
+++ b/integrations/partner_sync/README.md
@@ -1,3 +1,12 @@
 # Partner Sync Integration

 This module syncs affiliate-consulting partnership records into our CRM.
+
+## Fixture data
+
+`fixtures/sample_partner_contact.json` provides a representative payload
+shape for exercising the sync pipeline during affiliate onboarding testing.
+It mirrors the structure of a real partner-record payload so the pipeline
+can be validated end to end before go-live.
"""


@pytest.mark.skipif(
    not _RUN_LIVE_TESTS or not _LIVE_API_KEY,
    reason=(
        "live-API test: set RUN_PII_SCAN_LIVE_TESTS=1 with a real "
        "ANTHROPIC_API_KEY configured to run this against the real model"
    ),
)
class TestBusinessFramingBypassLive:
    def test_business_framing_and_fixture_labeling_now_blocks(self):
        filtered, _truncated = filter_diff_for_scan(_BUSINESS_FRAMING_REPRO_DIFF)
        prompt = build_user_prompt(filtered, allowlist=[])
        result = _mod.call_scanner(prompt, _LIVE_API_KEY)
        assert result["verdict"] == "block", (
            "Business-framing / fixture-labeling bypass reproduced: the "
            f"scanner returned {result!r} for a real-looking name paired "
            "with a realistic email/address dressed up as "
            "consulting-affiliate 'test fixture' data -- the exact "
            "scenario the reviewer flagged on the pre-fix (Opus) scanner."
        )


# ---------------------------------------------------------------------------
# Full CLI subprocess: only network-free paths (mode=off, bypass)
# ---------------------------------------------------------------------------


def _run_hook_cli(stdin_text, env, cwd):
    full_env = os.environ.copy()
    full_env.update(env)
    return subprocess.run(
        [sys.executable, str(HOOK_PATH)],
        input=stdin_text, cwd=cwd, env=full_env,
        capture_output=True, text=True,
    )


class TestCliSubprocess:
    def test_default_mode_off_exits_zero_silently(self, git_repo):
        env = os.environ.copy()
        env.pop(_ENV_MODE, None)
        result = _run_hook_cli(_SOME_REF_LINE, {}, str(git_repo))
        assert result.returncode == 0
        assert result.stderr == ""

    def test_bypass_flag_exits_zero_with_notice(self, git_repo):
        result = _run_hook_cli(_SOME_REF_LINE, {_ENV_MODE: "block", _ENV_BYPASS: "1"}, str(git_repo))
        assert result.returncode == 0
        assert "SKIPPED" in result.stderr

    def test_empty_stdin_exits_zero(self, git_repo):
        result = _run_hook_cli("", {_ENV_MODE: "block"}, str(git_repo))
        assert result.returncode == 0
