#!/usr/bin/env python3
"""
audit-github-issue-pii.py — audit and (optionally) redact PII in GitHub issue
bodies/comments.

Motivation: two closed-but-public issues in this repo (#1866, #563) leak real
third-party PII in their issue bodies — a real name plus a personal Telegram
chat_id, and a real personal Gmail address. This tool finds that class of
leak in any issue and can rewrite the body with the sensitive spans replaced
by a placeholder, while leaving the surrounding technical content intact.

Design (functional core, imperative shell):
  - Detection (find_pii) and redaction (redact_text) are pure functions:
    text in, findings/text out. No I/O, no globals, fully unit-testable
    without mocking anything.
  - The only side effects — fetching an issue from GitHub and writing a
    redacted body back — are isolated in fetch_issue() and
    apply_body_redaction(). Both take an injectable `run` callable
    (defaults to subprocess.run) so tests can substitute a spy/fake without
    touching the network.
  - process_issue() wires the pure core to the boundary functions and is the
    single place that decides whether a write happens. Under --dry-run,
    apply_body_redaction is never called — this is asserted directly by the
    test suite (tests/unit/test_audit_github_issue_pii.py).

GitHub access convention: this repo's CLAUDE.md mandates the `gh` CLI (not
raw REST calls) for all GitHub operations, so fetch/apply below shell out to
`gh api` rather than using `requests`/`httpx` directly.

PII pattern provenance:
  - Email and phone regexes are adapted from the PII_PATTERNS table in
    .githooks/pre-push (kept in the stricter form that requires an explicit
    separator between phone digit groups, so a 10-digit chat_id like
    "5717728951" is not also misclassified as a phone number).
  - The personal-name denylist loader reuses the exact "name@@allowlist_regex"
    file format and per-name allowlist convention from .githooks/pre-push's
    NAME_PATTERNS loader (~/lobster-user-config/blocked-names.txt), so this
    tool and the pre-push hook share one denylist file instead of drifting.
  - The personal chat/user ID pattern (prose like "Telegram 5717728951") is
    new: existing hooks only cover the `chat_id=NNNN` env-var assignment
    form, not IDs embedded in issue prose.

Contributor email exemption (added after issue #563's redaction of
sayhar@gmail.com was reverted — that address belongs to Sahar Massachi, the
#1 contributor to this repo by commit count, not a "private individual"):
  - A known project contributor's email is a *public, attributable*
    identifier tied to their work on the project (visible in `git log`,
    commit authorship, and the GitHub contributors list) — categorically
    different from a private individual's personal email incidentally
    mentioned in an issue body. Redacting it hides authorship, not PII.
  - `find_emails`/`find_pii` now accept an `email_allowlist` of exempt
    addresses (case-insensitive exact match) that are never flagged, in
    addition to the existing domain-pattern allowlist (noreply addresses,
    example.com, etc).
  - `git_contributor_emails()` derives that allowlist automatically and
    dynamically from `git log --all --format=%ae` in a given repo checkout
    (a boundary function; isolated and injectable via `run=`, exactly like
    fetch_issue/apply_body_redaction) — so a contributor's exemption is
    never a hand-maintained list that goes stale, and instead reflects the
    actual git history. `--repo-path` controls which checkout to read (the
    default of --repo may point at a *remote* repo that isn't the local
    checkout the script runs from).
  - `load_email_allowlist()`/`parse_email_allowlist()` additionally support
    a plain-text override file (one email per line, `#` comments, blank
    lines ignored) for exempting emails that predate or fall outside git
    history, via `--contributor-emails-file`. Both sources are merged.

Usage:
    python scripts/audit-github-issue-pii.py --repo OWNER/REPO --issue 1866 --dry-run
    python scripts/audit-github-issue-pii.py --repo OWNER/REPO --issue 1866 --issue 563 --apply
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PLACEHOLDER = "[REDACTED — private individual]"

# Email address. Adapted from .githooks/pre-push PII_PATTERNS 'email'.
_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")

# Known false-positive email domains/prefixes. Adapted from .githooks/pre-push
# EMAIL_ALLOWLIST.
_EMAIL_ALLOWLIST_RE = re.compile(
    r"(?i)noreply@anthropic\.com|noreply@github\.com|example\.com|example\.org|test\.com|localhost"
)

# US phone number. Adapted from .githooks/pre-push PII_PATTERNS 'phone' — the
# separators between digit groups are mandatory (not optional) so a bare
# 10-digit run (e.g. a Telegram chat_id) is not also matched as a phone
# number.
_PHONE_RE = re.compile(r"(?:\+?1[-.\s])?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}")

# Personal chat/user ID embedded in prose, e.g. "Telegram 5717728951" or
# "chat_id: 5717728951". This pattern is new to this tool — existing hooks
# only recognize the `chat_id=NNNN` env-var assignment form.
_CHAT_ID_RE = re.compile(
    r"(?i)\b(?:telegram|chat[_ ]?id|user[_ ]?id)\b[\s:=]{0,3}(\d{6,15})"
)


# ---------------------------------------------------------------------------
# Data model (immutable)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NameRule:
    """One entry from the personal-name denylist file.

    Mirrors .githooks/pre-push's `name@@allowlist_regex` convention: `name`
    is matched case-insensitively as a whole word; if `allow_regex` is set
    and the token containing the match also matches it, the hit is
    suppressed (e.g. "sahar" denylisted, but "sayhar" allowlisted so the
    substring inside "sayhar@gmail.com" doesn't also fire as a name hit).
    """

    name: str
    allow_regex: str | None = None


@dataclass(frozen=True)
class Finding:
    """A single span of likely PII found in a text."""

    kind: str  # "name" | "email" | "phone" | "chat_id"
    label: str  # human-readable description, shown in reports
    start: int
    end: int
    text: str


@dataclass(frozen=True)
class IssueContent:
    """Fetched issue body + comments. Comments are audited (flagged) but not
    rewritten by --apply — the issue's scope is body redaction only.
    """

    repo: str
    number: int
    body: str
    comments: list[dict] = field(default_factory=list)


@dataclass(frozen=True)
class ProcessResult:
    issue: IssueContent
    findings: list[Finding]
    redacted_body: str
    comment_findings: list[tuple[object, list[Finding]]]
    applied: bool


# ---------------------------------------------------------------------------
# Pure detection functions
# ---------------------------------------------------------------------------


def find_emails(text: str, email_allowlist: Sequence[str] = ()) -> list[Finding]:
    """Find email-shaped spans, excluding known-benign and known-contributor
    addresses.

    `email_allowlist` is compared case-insensitively against the exact
    matched address (not a substring/domain match) — e.g. a contributor's
    own address never fires as a finding, but a *different* address at the
    same domain still does.
    """
    exempt = {e.strip().lower() for e in email_allowlist if e.strip()}
    findings = []
    for m in _EMAIL_RE.finditer(text):
        if _EMAIL_ALLOWLIST_RE.search(m.group(0)):
            continue
        if m.group(0).lower() in exempt:
            continue
        findings.append(Finding("email", "Email address", m.start(), m.end(), m.group(0)))
    return findings


def find_phones(text: str) -> list[Finding]:
    return [
        Finding("phone", "Phone number", m.start(), m.end(), m.group(0))
        for m in _PHONE_RE.finditer(text)
    ]


def find_chat_ids(text: str) -> list[Finding]:
    findings = []
    for m in _CHAT_ID_RE.finditer(text):
        # Redact just the numeric ID (group 1), keeping the context keyword
        # ("Telegram", "chat_id", ...) so the redacted text still reads
        # coherently — it's clear *what kind* of identifier was removed.
        findings.append(
            Finding("chat_id", "Personal chat/user ID", m.start(1), m.end(1), m.group(1))
        )
    return findings


_TOKEN_RE = re.compile(r"\S+")


def _containing_token(text: str, start: int, end: int) -> str:
    """Return the whitespace-delimited token that contains text[start:end]."""
    for m in _TOKEN_RE.finditer(text):
        if m.start() <= start and m.end() >= end:
            return m.group(0)
    return text[start:end]


def find_names(text: str, denylist: Sequence[NameRule]) -> list[Finding]:
    findings = []
    for rule in denylist:
        pattern = re.compile(
            rf"(?<![A-Za-z]){re.escape(rule.name)}(?![A-Za-z])", re.IGNORECASE
        )
        for m in pattern.finditer(text):
            if rule.allow_regex:
                token = _containing_token(text, m.start(), m.end())
                if re.search(rule.allow_regex, token, re.IGNORECASE):
                    continue
            findings.append(
                Finding("name", f"Personal name ({rule.name})", m.start(), m.end(), m.group(0))
            )
    return findings


def _merge_overlapping(text: str, findings: Sequence[Finding]) -> list[Finding]:
    """Merge findings whose spans overlap or touch into a single finding.

    This keeps redaction idempotent and coherent: two pattern categories
    that happen to match overlapping/adjacent spans (e.g. a name match fully
    contained inside a broader match) must produce exactly one placeholder,
    never two adjacent or nested ones.
    """
    if not findings:
        return []

    ordered = sorted(findings, key=lambda f: (f.start, f.end))
    merged = [ordered[0]]
    for current in ordered[1:]:
        last = merged[-1]
        if current.start <= last.end:
            if current.end > last.end:
                label = last.label if current.label == last.label else f"{last.label}; {current.label}"
                kind = last.kind if current.kind == last.kind else f"{last.kind}+{current.kind}"
                merged[-1] = Finding(
                    kind=kind,
                    label=label,
                    start=last.start,
                    end=current.end,
                    text=text[last.start : current.end],
                )
            # else: current is fully contained in last — drop it.
        else:
            merged.append(current)
    return merged


def find_pii(
    text: str,
    name_denylist: Sequence[NameRule] = (),
    email_allowlist: Sequence[str] = (),
) -> list[Finding]:
    """Pure function: text in, PII findings out. No I/O."""
    findings = [
        *find_names(text, name_denylist),
        *find_emails(text, email_allowlist),
        *find_phones(text),
        *find_chat_ids(text),
    ]
    return _merge_overlapping(text, findings)


# ---------------------------------------------------------------------------
# Pure redaction function
# ---------------------------------------------------------------------------


def redact_text(
    text: str, findings: Sequence[Finding], placeholder: str = PLACEHOLDER
) -> str:
    """Pure function: replace each finding's span with `placeholder`.

    Findings are processed in start order; overlapping spans (which
    find_pii() already merges) are defensively skipped here too so this
    function is safe to call directly with arbitrary finding lists in tests.
    """
    pieces = []
    cursor = 0
    for f in sorted(findings, key=lambda x: x.start):
        if f.start < cursor:
            continue
        pieces.append(text[cursor : f.start])
        pieces.append(placeholder)
        cursor = f.end
    pieces.append(text[cursor:])
    return "".join(pieces)


# ---------------------------------------------------------------------------
# Name denylist loading (reuses .githooks/pre-push's file format)
# ---------------------------------------------------------------------------


def parse_name_denylist(lines: Sequence[str]) -> list[NameRule]:
    rules = []
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "@@" in line:
            name, allow = line.split("@@", 1)
        else:
            name, allow = line, ""
        name = name.strip()
        allow = allow.strip()
        if name:
            rules.append(NameRule(name=name, allow_regex=allow or None))
    return rules


def default_names_file() -> Path:
    config_dir = os.environ.get(
        "LOBSTER_USER_CONFIG_DIR", str(Path.home() / "lobster-user-config")
    )
    return Path(config_dir) / "blocked-names.txt"


def load_name_denylist(path: Path) -> list[NameRule]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    return parse_name_denylist(text.splitlines())


# ---------------------------------------------------------------------------
# Contributor email allowlist (static override file + dynamic git-log
# derivation). Two independently-loadable sources, merged by the caller.
# ---------------------------------------------------------------------------


def parse_email_allowlist(lines: Sequence[str]) -> list[str]:
    """Parse a plain-text contributor-email override file: one address per
    line, '#' comments and blank lines ignored. Pure function.
    """
    emails = []
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        emails.append(line.lower())
    return emails


def default_contributor_emails_file() -> Path:
    config_dir = os.environ.get(
        "LOBSTER_USER_CONFIG_DIR", str(Path.home() / "lobster-user-config")
    )
    return Path(config_dir) / "contributor-emails.txt"


def load_email_allowlist(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    return parse_email_allowlist(text.splitlines())


# ---------------------------------------------------------------------------
# Boundary functions (side effects live here, and only here)
# ---------------------------------------------------------------------------


def git_contributor_emails(
    repo_path: str = ".", run: Callable = subprocess.run
) -> list[str]:
    """Derive the set of known-contributor emails from git history itself,
    rather than a hand-maintained list that inevitably goes stale.

    Read-only (`git log`, no writes). Best-effort: any failure (not a git
    checkout, `git` missing, etc.) yields an empty list rather than raising,
    so a missing/foreign checkout degrades to "no dynamic exemptions" instead
    of crashing the audit run.
    """
    try:
        result = run(
            ["git", "-C", repo_path, "log", "--all", "--format=%ae"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return []
    emails = {line.strip().lower() for line in result.stdout.splitlines() if line.strip()}
    return sorted(emails)


def _gh_api_json(run: Callable, path: str):
    result = run(["gh", "api", path], capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def fetch_issue(repo: str, number: int, run: Callable = subprocess.run) -> IssueContent:
    """Fetch an issue's body + comments via `gh api` (read-only)."""
    issue_data = _gh_api_json(run, f"repos/{repo}/issues/{number}")
    comments_data = _gh_api_json(run, f"repos/{repo}/issues/{number}/comments")
    return IssueContent(
        repo=repo,
        number=number,
        body=issue_data.get("body") or "",
        comments=comments_data or [],
    )


def apply_body_redaction(
    repo: str, number: int, new_body: str, run: Callable = subprocess.run
) -> None:
    """Write the redacted body back to the issue.

    This is the ONLY function in this module that performs a GitHub write.
    Nothing under --dry-run may call this — enforced by process_issue()
    below and asserted directly by the test suite.
    """
    # NOTE: this must be -F (--field, "typed" — supports "@-"/"@path" magic
    # value expansion to read from stdin/file) and NOT -f (--raw-field,
    # literal string, no expansion). `gh api ... -f body=@-` would set the
    # issue body to the literal two-character string "@-" instead of reading
    # the redacted body from stdin. Verified against a live `gh api` call:
    #   -f text=@-  ->  body becomes the literal string "@-"
    #   -F text=@-  ->  body becomes the actual stdin content
    # Regression-tested by test_apply_uses_dash_capital_F_for_body (argv
    # shape) and test_apply_against_real_gh_shaped_shim_reads_stdin_body
    # (a fake `gh` shim on PATH exercising the real -f/-F parsing behavior).
    run(
        ["gh", "api", f"repos/{repo}/issues/{number}", "-X", "PATCH", "-F", "body=@-"],
        input=new_body,
        capture_output=True,
        text=True,
        check=True,
    )


# ---------------------------------------------------------------------------
# Wiring: pure core + boundary
# ---------------------------------------------------------------------------


def process_issue(
    issue: IssueContent,
    name_denylist: Sequence[NameRule],
    apply: bool,
    run: Callable = subprocess.run,
    email_allowlist: Sequence[str] = (),
) -> ProcessResult:
    findings = find_pii(issue.body, name_denylist, email_allowlist)
    redacted_body = redact_text(issue.body, findings)
    comment_findings = [
        (c.get("id"), find_pii(c.get("body") or "", name_denylist, email_allowlist))
        for c in issue.comments
    ]

    will_apply = apply and bool(findings)
    if will_apply:
        apply_body_redaction(issue.repo, issue.number, redacted_body, run=run)

    return ProcessResult(
        issue=issue,
        findings=findings,
        redacted_body=redacted_body,
        comment_findings=comment_findings,
        applied=will_apply,
    )


# ---------------------------------------------------------------------------
# Reporting (side effect: print; formatting logic itself is pure/testable
# via the return value of format_report)
# ---------------------------------------------------------------------------


def format_report(result: ProcessResult, apply: bool) -> str:
    issue = result.issue
    mode = "APPLY" if apply else "DRY RUN"
    lines = [
        f"=== {mode}: {issue.repo}#{issue.number} ===",
        "",
        f"Findings in body: {len(result.findings)}",
    ]
    for f in result.findings:
        lines.append(f"  - [{f.kind}] {f.label}: {f.text!r} (chars {f.start}-{f.end})")

    for comment_id, findings in result.comment_findings:
        if not findings:
            continue
        lines.append(f"Findings in comment {comment_id}: {len(findings)}")
        for f in findings:
            lines.append(f"  - [{f.kind}] {f.label}: {f.text!r} (chars {f.start}-{f.end})")

    lines.append("")
    lines.append("--- Proposed redacted body ---")
    lines.append(result.redacted_body)
    lines.append("--- end proposed redacted body ---")
    lines.append("")
    if apply:
        lines.append("WRITE: " + ("applied" if result.applied else "skipped (no findings)"))
    else:
        lines.append("DRY RUN: no API write calls were made.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Audit and (optionally) redact PII in a GitHub issue body."
    )
    parser.add_argument("--repo", required=True, help="OWNER/REPO")
    parser.add_argument(
        "--issue",
        type=int,
        action="append",
        dest="issues",
        required=True,
        help="Issue number to audit. May be repeated for multiple issues.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Report findings + proposed redaction only. Default. Makes zero API writes.",
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        help="Actually rewrite the issue body via the GitHub API.",
    )
    parser.add_argument(
        "--names-file",
        default=str(default_names_file()),
        help="Path to the personal-name denylist file (default: %(default)s).",
    )
    parser.add_argument(
        "--contributor-emails-file",
        default=str(default_contributor_emails_file()),
        help=(
            "Path to a static contributor-email exemption file, one address "
            "per line (default: %(default)s). Merged with emails discovered "
            "via --repo-path's git history."
        ),
    )
    parser.add_argument(
        "--repo-path",
        default=".",
        help=(
            "Local git checkout to read contributor emails from (git log "
            "--all --format=%%ae), for automatic contributor exemption. "
            "May differ from --repo, which is only the remote issue source. "
            "Default: current directory."
        ),
    )
    parser.add_argument(
        "--no-git-contributors",
        action="store_true",
        help="Skip deriving contributor emails from git log (static --contributor-emails-file only).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    apply = bool(args.apply)  # --dry-run is the default; --apply must be explicit

    denylist = load_name_denylist(Path(args.names_file))

    email_allowlist = set(load_email_allowlist(Path(args.contributor_emails_file)))
    if not args.no_git_contributors:
        email_allowlist |= set(git_contributor_emails(args.repo_path))

    for issue_number in args.issues:
        issue = fetch_issue(args.repo, issue_number)
        result = process_issue(issue, denylist, apply=apply, email_allowlist=email_allowlist)
        print(format_report(result, apply=apply))
        print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
