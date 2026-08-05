"""
Tests for scripts/audit-github-issue-pii.py.

This tool scans GitHub issue bodies/comments for likely third-party PII
(real names, emails, phone numbers, personal chat/user IDs) and can redact
it in place. Motivated by two closed-but-public issues in this repo (#1866,
#563) that leak a real person's name + Telegram chat_id, and a real personal
Gmail address, respectively.

Design under test:
  - find_pii(text, name_denylist) -> list[Finding]        (pure)
  - redact_text(text, findings, placeholder) -> str        (pure)
  - fetch_issue(repo, number, run=...) -> IssueContent      (boundary; mocked here)
  - apply_body_redaction(repo, number, body, run=...) -> None  (boundary; mocked here)
  - process_issue(issue, name_denylist, apply, run=...) -> ProcessResult
    (wires the pure functions to the boundary; --dry-run must never reach
    apply_body_redaction / the write call)

Fixtures below intentionally mirror the *shape* of #1866 and #563 without
being the literal real issue text (the real bodies are fetched live only in
the manual --dry-run test against the real repo, never committed here).
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

# The script file is named audit-github-issue-pii.py (hyphenated), which is
# not a valid Python identifier — load it by file path, matching the
# convention used by tests/unit/test_oom_monitor.py for scripts/oom-monitor.py.
_script_path = Path(__file__).parent.parent.parent / "scripts" / "audit-github-issue-pii.py"
_spec = importlib.util.spec_from_file_location("audit_github_issue_pii", _script_path)
audit = importlib.util.module_from_spec(_spec)
sys.modules["audit_github_issue_pii"] = audit
_spec.loader.exec_module(audit)


# ---------------------------------------------------------------------------
# Fixtures (fake bodies shaped like the real #1866 / #563 cases, not the
# literal real text)
# ---------------------------------------------------------------------------

NAME_AND_CHAT_ID_BODY = (
    "## Problem\n\n"
    "Jordan Blake (Acme Corp founder/CEO, Telegram 5717728951) has a personal "
    "account that should also be ingested.\n"
)

EMAIL_BODY = (
    "## Current Limitation\n\n"
    "The digest job only fetches issues assigned to Sam by looking up her "
    "user ID via samplename@gmail.com. This misses two categories of work.\n"
)

NAME_DENYLIST = [audit.NameRule(name="Jordan Blake")]


# ---------------------------------------------------------------------------
# find_pii — pure detection logic
# ---------------------------------------------------------------------------


class TestFindPii:
    def test_detects_real_name_and_personal_chat_id(self):
        findings = audit.find_pii(NAME_AND_CHAT_ID_BODY, NAME_DENYLIST)
        kinds = {f.kind for f in findings}

        assert "name" in kinds
        assert "chat_id" in kinds

        name_finding = next(f for f in findings if f.kind == "name")
        assert name_finding.text == "Jordan Blake"

        chat_id_finding = next(f for f in findings if f.kind == "chat_id")
        assert chat_id_finding.text == "5717728951"

    def test_detects_real_email(self):
        findings = audit.find_pii(EMAIL_BODY, name_denylist=[])
        emails = [f for f in findings if f.kind == "email"]

        assert len(emails) == 1
        assert emails[0].text == "samplename@gmail.com"

    def test_no_findings_on_clean_technical_body(self):
        clean = (
            "## Scope\n\nAdd retry logic to the polling loop with exponential "
            "backoff (60s, 120s, 240s) and a max of 3 attempts.\n"
        )
        assert audit.find_pii(clean, NAME_DENYLIST) == []

    def test_name_denylist_allowlist_suppresses_false_positive(self):
        # "alex" is denylisted, but non-letter characters (e.g. "_") still
        # count as word boundaries, so a token like "alex_bot" (a generic
        # service account, not the real person) would otherwise false-positive
        # as a name hit. allow_regex exempts any match whose containing
        # whitespace-delimited token matches it.
        text = "Routed via alex_bot for the nightly sync; no manual step needed."
        rule = audit.NameRule(name="alex", allow_regex="alex_bot")
        findings = audit.find_pii(text, name_denylist=[rule])
        assert not any(f.kind == "name" for f in findings)

        # Sanity check: without the allowlist, the same text-and-denylist
        # combination *would* flag it — proving the allow_regex is load-bearing.
        rule_without_allow = audit.NameRule(name="alex", allow_regex=None)
        findings_without_allow = audit.find_pii(text, name_denylist=[rule_without_allow])
        assert any(f.kind == "name" for f in findings_without_allow)


# ---------------------------------------------------------------------------
# redact_text — pure transformation logic
# ---------------------------------------------------------------------------


class TestRedactText:
    def test_redacts_name_and_chat_id_preserving_surrounding_text(self):
        findings = audit.find_pii(NAME_AND_CHAT_ID_BODY, NAME_DENYLIST)
        redacted = audit.redact_text(NAME_AND_CHAT_ID_BODY, findings)

        assert "Jordan Blake" not in redacted
        assert "5717728951" not in redacted
        assert redacted.count(audit.PLACEHOLDER) == 2
        # Surrounding technical content must survive.
        assert "Acme Corp founder/CEO" in redacted
        assert "personal" in redacted
        assert "## Problem" in redacted

    def test_redacts_email_preserving_surrounding_text(self):
        findings = audit.find_pii(EMAIL_BODY, name_denylist=[])
        redacted = audit.redact_text(EMAIL_BODY, findings)

        assert "samplename@gmail.com" not in redacted
        assert redacted.count(audit.PLACEHOLDER) == 1
        assert "## Current Limitation" in redacted
        assert "digest job only fetches issues assigned to Sam" in redacted

    def test_no_findings_returns_text_unchanged(self):
        clean = "Nothing sensitive here, just technical scope notes."
        assert audit.redact_text(clean, []) == clean


# ---------------------------------------------------------------------------
# Idempotency: applying redaction twice must not double-redact / corrupt
# ---------------------------------------------------------------------------


class TestIdempotency:
    def test_redacting_already_redacted_body_is_a_no_op(self):
        findings = audit.find_pii(NAME_AND_CHAT_ID_BODY, NAME_DENYLIST)
        once = audit.redact_text(NAME_AND_CHAT_ID_BODY, findings)

        second_findings = audit.find_pii(once, NAME_DENYLIST)
        assert second_findings == []

        twice = audit.redact_text(once, second_findings)
        assert twice == once
        # No nested/doubled placeholder text.
        assert audit.PLACEHOLDER * 2 not in twice
        assert f"{audit.PLACEHOLDER}{audit.PLACEHOLDER}" not in twice

    def test_process_issue_apply_twice_does_not_double_redact(self, monkeypatch):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append({"cmd": cmd, "input": kwargs.get("input")})
            return _FakeCompletedProcess()

        issue = audit.IssueContent(
            repo="octo/example", number=1, body=NAME_AND_CHAT_ID_BODY, comments=[]
        )
        result1 = audit.process_issue(issue, NAME_DENYLIST, apply=True, run=fake_run)
        assert result1.applied is True
        assert len(calls) == 1

        issue_after_first = audit.IssueContent(
            repo="octo/example", number=1, body=result1.redacted_body, comments=[]
        )
        result2 = audit.process_issue(
            issue_after_first, NAME_DENYLIST, apply=True, run=fake_run
        )

        # Second pass finds nothing left to redact, so it must not issue a
        # second write call, and the body must be byte-identical.
        assert result2.findings == []
        assert result2.applied is False
        assert result2.redacted_body == result1.redacted_body
        assert len(calls) == 1  # still just the one call from the first pass


# ---------------------------------------------------------------------------
# Boundary isolation: --dry-run must make zero write calls
# ---------------------------------------------------------------------------


class _FakeCompletedProcess:
    stdout = "{}"
    stderr = ""
    returncode = 0


class TestDryRunMakesNoWriteCalls:
    def test_dry_run_never_invokes_apply_body_redaction(self, monkeypatch):
        write_calls = []
        monkeypatch.setattr(
            audit,
            "apply_body_redaction",
            lambda *a, **kw: write_calls.append((a, kw)),
        )

        issue = audit.IssueContent(
            repo="octo/example", number=1866, body=NAME_AND_CHAT_ID_BODY, comments=[]
        )
        result = audit.process_issue(issue, NAME_DENYLIST, apply=False)

        assert write_calls == []
        assert result.applied is False
        assert len(result.findings) > 0  # findings still reported in dry-run

    def test_dry_run_makes_no_subprocess_calls_at_all(self, monkeypatch):
        run_calls = []

        def fake_run(cmd, **kwargs):
            run_calls.append(cmd)
            return _FakeCompletedProcess()

        issue = audit.IssueContent(
            repo="octo/example", number=1866, body=NAME_AND_CHAT_ID_BODY, comments=[]
        )
        audit.process_issue(issue, NAME_DENYLIST, apply=False, run=fake_run)

        assert run_calls == []


# ---------------------------------------------------------------------------
# --apply calls the edit endpoint with the expected redacted body
# ---------------------------------------------------------------------------


class TestApplyCallsEditEndpoint:
    def test_apply_invokes_gh_api_patch_with_redacted_body(self, monkeypatch):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append({"cmd": cmd, "kwargs": kwargs})
            return _FakeCompletedProcess()

        issue = audit.IssueContent(
            repo="octo/example", number=1866, body=NAME_AND_CHAT_ID_BODY, comments=[]
        )
        result = audit.process_issue(issue, NAME_DENYLIST, apply=True, run=fake_run)

        assert result.applied is True
        assert len(calls) == 1
        cmd = calls[0]["cmd"]
        assert cmd[:2] == ["gh", "api"]
        assert "repos/octo/example/issues/1866" in cmd
        assert "-X" in cmd and "PATCH" in cmd

        # The body written must be exactly the pure redact_text() output —
        # the write call is a thin boundary, not a place where redaction
        # logic re-runs or diverges.
        expected_body = audit.redact_text(
            NAME_AND_CHAT_ID_BODY, audit.find_pii(NAME_AND_CHAT_ID_BODY, NAME_DENYLIST)
        )
        assert calls[0]["kwargs"]["input"] == expected_body
        assert "Jordan Blake" not in expected_body
        assert "5717728951" not in expected_body

    def test_apply_skips_write_when_no_findings(self, monkeypatch):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return _FakeCompletedProcess()

        clean = "Nothing sensitive here."
        issue = audit.IssueContent(repo="octo/example", number=2, body=clean, comments=[])
        result = audit.process_issue(issue, NAME_DENYLIST, apply=True, run=fake_run)

        assert result.applied is False
        assert calls == []


# ---------------------------------------------------------------------------
# Regression: `gh api` has two similarly-named flags with very different
# semantics for the magic "@-"/"@path" stdin/file expansion:
#   -F / --field      expands "@-" to stdin content (what we want)
#   -f / --raw-field  treats the value as a LITERAL string — "@-" would
#                     become the literal two-character body "@-", silently
#                     destroying the issue body instead of writing the
#                     redacted text.
# A prior version of apply_body_redaction used -f, which every existing
# test missed because they all mock the `run` callable entirely — nothing
# exercised gh's real flag-parsing semantics. These two tests close that
# gap: one checks the exact argv shape, the other drives a real subprocess
# against a `gh`-shaped shim that mirrors gh's actual documented behavior
# for -f vs -F (verified independently via a live `gh api -X POST /markdown`
# call: `-f text=@-` renders literally as "@-"; `-F text=@-` renders the
# real stdin content).
# ---------------------------------------------------------------------------


class TestApplyUsesFieldFlagForBody:
    def test_apply_uses_dash_capital_F_not_dash_f_for_body(self, monkeypatch):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return _FakeCompletedProcess()

        issue = audit.IssueContent(
            repo="octo/example", number=1866, body=NAME_AND_CHAT_ID_BODY, comments=[]
        )
        audit.process_issue(issue, NAME_DENYLIST, apply=True, run=fake_run)

        assert len(calls) == 1
        cmd = calls[0]
        # "-F body=@-" must be present; "-f" must not appear anywhere in the
        # command (there is nothing else in this call that legitimately
        # needs --raw-field).
        assert "-f" not in cmd
        assert "-F" in cmd
        f_index = cmd.index("-F")
        assert cmd[f_index + 1] == "body=@-"

    def test_apply_against_real_gh_shaped_shim_reads_stdin_body(
        self, monkeypatch, tmp_path
    ):
        """Exercise real subprocess + real argv-parsing-shaped behavior,
        not a mocked `run`. A fake `gh` executable on PATH mirrors gh's
        actual, verified-live behavior: -F body=@- reads stdin; -f body=@-
        would yield the literal string "@-". This is the same falsifiability
        bar as the other core-logic checks, aimed at the boundary function
        instead of the pure core.
        """
        body_output_file = tmp_path / "captured_body.txt"

        shim = tmp_path / "gh"
        shim.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "flag=\"\"\n"
            "value=\"\"\n"
            "prev=\"\"\n"
            "for arg in \"$@\"; do\n"
            "    if [[ \"$arg\" == body=* ]]; then\n"
            "        flag=\"$prev\"\n"
            "        value=\"${arg#body=}\"\n"
            "    fi\n"
            "    prev=\"$arg\"\n"
            "done\n"
            "if [[ \"$flag\" == \"-F\" && \"$value\" == \"@-\" ]]; then\n"
            "    resolved=\"$(cat)\"\n"
            "elif [[ \"$flag\" == \"-f\" && \"$value\" == \"@-\" ]]; then\n"
            "    resolved=\"@-\"\n"
            "else\n"
            "    resolved=\"$value\"\n"
            "fi\n"
            f'printf \'%s\' "$resolved" > "{body_output_file}"\n'
            "echo '{}'\n"
        )
        shim.chmod(0o755)

        monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")

        redacted_body = audit.redact_text(
            NAME_AND_CHAT_ID_BODY, audit.find_pii(NAME_AND_CHAT_ID_BODY, NAME_DENYLIST)
        )
        audit.apply_body_redaction("octo/example", 1866, redacted_body)

        captured = body_output_file.read_text()
        # bash's $(cat) command substitution strips trailing newlines, so
        # compare with trailing newlines normalized; the load-bearing checks
        # are that the real stdin content made it through at all, and that
        # it is NOT the literal "@-" that -f would have produced.
        assert captured.rstrip("\n") == redacted_body.rstrip("\n")
        assert captured != "@-"


# ---------------------------------------------------------------------------
# Name denylist file loader (reuses the existing pre-push hook's
# blocked-names.txt "name@@allowlist_regex" format)
# ---------------------------------------------------------------------------


class TestNameDenylistLoader:
    def test_parses_name_only_lines(self):
        rules = audit.parse_name_denylist(["alice", "# comment", "", "carol"])
        assert rules == [
            audit.NameRule(name="alice", allow_regex=None),
            audit.NameRule(name="carol", allow_regex=None),
        ]

    def test_parses_name_with_allowlist_regex(self):
        rules = audit.parse_name_denylist(["bob@@bobcat"])
        assert rules == [audit.NameRule(name="bob", allow_regex="bobcat")]

    def test_load_name_denylist_missing_file_returns_empty(self, tmp_path):
        missing = tmp_path / "does-not-exist.txt"
        assert audit.load_name_denylist(missing) == []

    def test_load_name_denylist_reads_file(self, tmp_path):
        f = tmp_path / "blocked-names.txt"
        f.write_text("# comment\nalice@@\ncarol@@\n")
        rules = audit.load_name_denylist(f)
        assert rules == [
            audit.NameRule(name="alice", allow_regex=None),
            audit.NameRule(name="carol", allow_regex=None),
        ]


# ---------------------------------------------------------------------------
# fetch_issue — boundary, mocked (no real network calls in unit tests)
# ---------------------------------------------------------------------------


class TestFetchIssue:
    def test_fetch_issue_calls_gh_api_for_body_and_comments(self, monkeypatch):
        import json as _json

        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            if cmd[-1].endswith("/comments"):
                return _StubProcess(_json.dumps([{"id": 1, "body": "a comment"}]))
            return _StubProcess(_json.dumps({"body": "the issue body"}))

        issue = audit.fetch_issue("octo/example", 42, run=fake_run)

        assert issue.repo == "octo/example"
        assert issue.number == 42
        assert issue.body == "the issue body"
        assert issue.comments == [{"id": 1, "body": "a comment"}]
        assert len(calls) == 2
        assert all(c[:2] == ["gh", "api"] for c in calls)


class _StubProcess:
    def __init__(self, stdout: str):
        self.stdout = stdout
        self.stderr = ""
        self.returncode = 0
