"""
Unit tests for hooks/git_push_scan.py -- the shared PII/secret diff-scanning
module used by hooks/agent-git-push-guard.py.

These tests exercise the *real* scan_diff() implementation against realistic
unified-diff text (no mocking of the scan logic itself) so that a reverted or
broken implementation genuinely fails these tests -- see
test_scan_diff_is_falsifiable below for an explicit demonstration.
"""
import importlib.util
import sys
import tempfile
from pathlib import Path

import pytest


def _load_module():
    hooks_dir = Path(__file__).parent.parent.parent.parent / "hooks"
    mod_path = hooks_dir / "git_push_scan.py"
    spec = importlib.util.spec_from_file_location("git_push_scan", mod_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["git_push_scan"] = mod
    spec.loader.exec_module(mod)
    return mod


gps = _load_module()


def _diff(filepath: str, added_lines: list[str], start_line: int = 1) -> str:
    """Build a minimal unified diff with one hunk of added lines."""
    header = (
        f"diff --git a/{filepath} b/{filepath}\n"
        f"index 0000000..1111111 100644\n"
        f"--- a/{filepath}\n"
        f"+++ b/{filepath}\n"
        f"@@ -0,0 +{start_line},{len(added_lines)} @@\n"
    )
    body = "".join(f"+{line}\n" for line in added_lines)
    return header + body


# ---------------------------------------------------------------------------
# True positives
# ---------------------------------------------------------------------------

def test_detects_email_address():
    findings, timed_out = gps.scan_diff(_diff("src/x.py", ['user_email = "realuser@gmail.com"']))
    assert not timed_out
    assert any(f.description == "Email address" for f in findings)


def test_detects_ssn():
    findings, _ = gps.scan_diff(_diff("src/x.py", ['ssn = "123-45-6789"']))
    assert any(f.description == "Social Security Number" for f in findings)


def test_detects_aws_key():
    findings, _ = gps.scan_diff(_diff("src/x.py", ['AWS_KEY = "AKIAABCDEFGHIJKLMNOP"']))
    assert any(f.description == "AWS Access Key ID" for f in findings)


def test_detects_openai_style_secret_key():
    findings, _ = gps.scan_diff(_diff("src/x.py", ['key = "sk-abcdefghijklmnopqrstuvwx"']))
    assert any(f.description == "API secret key (sk-...)" for f in findings)


def test_detects_github_token():
    findings, _ = gps.scan_diff(_diff("src/x.py", ['token = "ghp_' + "a" * 40 + '"']))
    assert any(f.description == "GitHub token" for f in findings)


def test_detects_private_key_header():
    findings, _ = gps.scan_diff(_diff("src/x.py", ["-----BEGIN RSA PRIVATE KEY-----"]))
    assert any(f.description == "Private key" for f in findings)


def test_detects_hardcoded_home_path():
    findings, _ = gps.scan_diff(_diff("src/x.py", ['path = "/home/lobster/secret-stuff"']))
    assert any("Hardcoded home path" in f.description for f in findings)


def test_detects_hardcoded_password():
    findings, _ = gps.scan_diff(_diff("src/x.py", ['password = "hunter22"']))
    assert any(f.description == "Hardcoded password" for f in findings)


def test_only_scans_added_lines_not_removed():
    # A credit-card-shaped number on a removed line must NOT be flagged.
    diff_text = (
        "diff --git a/src/x.py b/src/x.py\n"
        "index 0000000..1111111 100644\n"
        "--- a/src/x.py\n"
        "+++ b/src/x.py\n"
        "@@ -1,1 +1,1 @@\n"
        "-cc = \"4111111111111111\"\n"
        "+cc = \"REDACTED\"\n"
    )
    findings, _ = gps.scan_diff(diff_text)
    assert not any(f.description == "Credit card number" for f in findings)


# ---------------------------------------------------------------------------
# True negatives / suppressions
# ---------------------------------------------------------------------------

def test_clean_diff_has_no_findings():
    findings, timed_out = gps.scan_diff(_diff("src/x.py", ["def add(a, b):", "    return a + b"]))
    assert findings == []
    assert not timed_out


def test_placeholder_value_is_not_flagged():
    findings, _ = gps.scan_diff(_diff("src/x.py", ['password = "changeme_placeholder"']))
    assert findings == []


def test_known_allowlisted_email_is_not_flagged():
    findings, _ = gps.scan_diff(_diff("src/x.py", ["contact = 'noreply@github.com'"]))
    assert findings == []


def test_private_ip_is_not_flagged():
    findings, _ = gps.scan_diff(_diff("src/x.py", ['host = "192.168.1.50"']))
    assert not any(f.description == "IP address" for f in findings)


def test_test_directory_is_skipped():
    findings, _ = gps.scan_diff(_diff("tests/fixtures/x.py", ['email = "real@example.org"']))
    assert findings == []


def test_doc_file_is_skipped():
    findings, _ = gps.scan_diff(_diff("docs/notes.md", ["contact real-person@gmail.com"]))
    assert findings == []


def test_githooks_dir_is_skipped():
    # The hook files themselves legitimately contain these patterns as regex source.
    findings, _ = gps.scan_diff(_diff(".githooks/pre-push", ['ssn@@\\b\\d{3}-\\d{2}-\\d{4}\\b@@SSN']))
    assert findings == []


def test_nosec_inline_suppression():
    findings, _ = gps.scan_diff(_diff("src/x.py", ['email = "real@example.org"  # nosec']))
    assert findings == []


def test_allowlist_file_suppresses_known_finding(tmp_path):
    repo_root = tmp_path
    githooks = repo_root / ".githooks"
    githooks.mkdir()
    (githooks / "security-allowlist.txt").write_text("src/x.py:Email address\n")
    findings, _ = gps.scan_diff(_diff("src/x.py", ['email = "real@example.org"']), repo_root=repo_root)
    assert findings == []


# ---------------------------------------------------------------------------
# Falsifiability check: prove these tests would catch a broken/reverted impl
# ---------------------------------------------------------------------------

def test_scan_diff_is_falsifiable(monkeypatch):
    """If scan_diff() were reverted to always return no findings (e.g. a
    no-op stub), this test fails -- proving the true-positive tests above are
    not vacuously passing against a broken implementation."""

    def _broken_scan_diff(diff_text, repo_root=None, timeout_seconds=10.0):
        return [], False

    monkeypatch.setattr(gps, "scan_diff", _broken_scan_diff)
    findings, _ = gps.scan_diff(_diff("src/x.py", ['ssn = "123-45-6789"']))
    assert findings == [], "sanity check: the monkeypatched stub itself returns no findings"

    # Reload the real implementation and confirm it DOES find the SSN --
    # i.e. the assertion above is only true because of the monkeypatch,
    # not because the real detector is broken.
    real_mod = _load_module()
    real_findings, _ = real_mod.scan_diff(_diff("src/x.py", ['ssn = "123-45-6789"']))
    assert any(f.description == "Social Security Number" for f in real_findings)
