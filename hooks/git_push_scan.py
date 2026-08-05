#!/usr/bin/env python3
"""Shared PII/secret diff-scanning logic for git-push guards.

This module ports the pattern tables and diff-scanning algorithm already
defined in `.githooks/pre-push` (INSTANCE_PATTERNS, PII_PATTERNS,
SECURITY_PATTERNS, NAME_PATTERNS) from bash/grep-P into Python `re`, so that
`hooks/agent-git-push-guard.py` (a Claude Code PreToolUse hook, no TTY, no
bash subprocess-per-pattern cost) does not need to reinvent or drift from the
same detection rules a human pushing at a terminal already gets from
`.githooks/pre-push`.

**Why a port instead of a single shared source file:** `.githooks/pre-push` is
a bash script (it must run via the git hook mechanism, no Python runtime
guaranteed); this module is Python (it runs inside a Claude Code hook
process). The two cannot literally `import` the same code across that
language boundary. To keep them from silently drifting apart, this module's
pattern table is validated in
`tests/unit/test_hooks/test_git_push_scan_parity.py` against a live parse of
`.githooks/pre-push`'s pattern arrays -- if either side adds, removes, or
changes a pattern without updating the other, that test fails.

Both sides also read the *same* user config files
(`~/lobster-user-config/instance-domains.txt`, `blocked-names.txt`) at run
time, so instance-specific data (real domains, real names) is never
duplicated into source -- only the generic pattern shapes are.

No network calls, no Anthropic API calls anywhere in this module.
"""
from __future__ import annotations

import fnmatch
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Config file locations (mirrors .githooks/pre-push exactly)
# ---------------------------------------------------------------------------

_PLACEHOLDER_INSTANCE_URL_REGEX = r"your-instance-domain\.example"
_PLACEHOLDER_BOT_PREFIX_REGEX = r"\bYourPrefix_\w+_bot\b"

# Known false-positive email domains/addresses to ignore (mirrors pre-push).
EMAIL_ALLOWLIST_RE = re.compile(
    r"noreply@anthropic\.com|noreply@github\.com|example\.com|example\.org|test\.com|localhost",
    re.IGNORECASE,
)

# Private/reserved IP ranges to ignore (mirrors pre-push).
PRIVATE_IP_RE = re.compile(
    r"(?:255\.255\.255\.\d+|192\.168\.\d+\.\d+|10\.\d+\.\d+\.\d+|"
    r"172\.(?:1[6-9]|2\d|3[01])\.\d+\.\d+)"
)

# Obvious placeholder/fake value marker (mirrors pre-push's is_placeholder_value).
PLACEHOLDER_VALUE_RE = re.compile(
    r"(?:example|placeholder|your[_-]?\w*[_-]?here|your[_-]?(?:key|token|secret)|"
    r"xxx|dummy|fake|test[_-]?key|CHANGEME|TODO|FIXME|<REDACTED)",
    re.IGNORECASE,
)

# Inline suppression comments (mirrors pre-push).
NOSEC_RE = re.compile(r"#\s*nosec\b|#\s*noqa\b")
NONAME_RE = re.compile(r"#\s*noname\b")


@dataclass(frozen=True)
class Pattern:
    name: str
    regex: "re.Pattern[str]"
    description: str
    allowlist_re: "re.Pattern[str] | None" = None  # per-pattern false-positive filter (NAME_PATTERNS only)


@dataclass(frozen=True)
class Finding:
    filepath: str
    line: int
    description: str
    snippet: str

    def format(self) -> str:
        return f"{self.filepath}:{self.line} - {self.description} - {self.snippet[:120]!r}"


def _user_config_dir() -> Path:
    return Path(os.environ.get("LOBSTER_USER_CONFIG_DIR", str(Path.home() / "lobster-user-config")))


def _load_instance_domain_fragments() -> tuple[list[str], list[str]]:
    """Return (domain_fragments, bot_prefix_fragments) from instance-domains.txt.

    Mirrors .githooks/pre-push's parsing of `domain@@...` / `bot_prefix@@...` lines.
    """
    config_path = _user_config_dir() / "instance-domains.txt"
    domains: list[str] = []
    prefixes: list[str] = []
    if not config_path.is_file():
        return domains, prefixes
    try:
        text = config_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return domains, prefixes
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "@@" not in line:
            continue
        key, _, value = line.partition("@@")
        if key == "domain":
            domains.append(value)
        elif key == "bot_prefix":
            prefixes.append(value)
    return domains, prefixes


def _load_name_patterns() -> list[Pattern]:
    """Return NAME_PATTERNS from blocked-names.txt, or the built-in placeholders.

    Mirrors .githooks/pre-push's `name@@allowlist_regex` parsing and its
    alice/bob/carol fallback when no config file exists.
    """
    config_path = _user_config_dir() / "blocked-names.txt"
    patterns: list[Pattern] = []
    if config_path.is_file():
        try:
            text = config_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            name, _, allow = line.partition("@@")
            name = name.strip()
            if not name:
                continue
            allow_re = re.compile(allow, re.IGNORECASE) if allow else None
            patterns.append(
                Pattern(
                    name=name,
                    regex=re.compile(rf"(?<![a-z]){re.escape(name)}(?![a-z])", re.IGNORECASE),
                    description=f"Personal name ({name})",
                    allowlist_re=allow_re,
                )
            )
    if not patterns:
        for name in ("alice", "bob", "carol"):
            patterns.append(
                Pattern(
                    name=name,
                    regex=re.compile(rf"(?<![a-z]){name}(?![a-z])", re.IGNORECASE),
                    description=f"Personal name ({name})",
                )
            )
    return patterns


def build_instance_patterns() -> list[Pattern]:
    domains, prefixes = _load_instance_domain_fragments()

    if domains:
        instance_url_regex = "(?:" + "|".join(domains) + ")"
    else:
        instance_url_regex = _PLACEHOLDER_INSTANCE_URL_REGEX

    if prefixes:
        bot_prefix_regex = r"\b(?:" + "|".join(prefixes) + r")\w+_bot\b"
    else:
        bot_prefix_regex = _PLACEHOLDER_BOT_PREFIX_REGEX

    return [
        Pattern("instance_url", re.compile(instance_url_regex, re.IGNORECASE), "Instance-specific URL"),
        Pattern(
            "home_path",
            re.compile(r"/home/(?:admin|lobster)\b"),
            "Hardcoded home path (/home/admin or /home/lobster)",
        ),
        Pattern("awp_bot", re.compile(bot_prefix_regex, re.IGNORECASE), "Instance-specific bot username prefix"),
        Pattern("db_url", re.compile(r"(?:postgresql|mongodb)://", re.IGNORECASE), "Database connection URL"),
        Pattern(
            "numeric_id",
            re.compile(r"(?:BOT_USER_ID|chat_id|ADMIN_CHAT_ID)\s*=\s*\d{8,}", re.IGNORECASE),
            "Hardcoded numeric bot/user ID (8+ digits)",
        ),
    ]


PII_PATTERNS: list[Pattern] = [
    Pattern("email", re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}"), "Email address"),
    Pattern("phone", re.compile(r"(\+?1[-.\s])?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}"), "Phone number"),
    Pattern("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "Social Security Number"),
    Pattern(
        "cc",
        re.compile(r"\b(?:4\d{3}|5[1-5]\d{2}|3[47]\d{2}|6(?:011|5\d{2}))[- ]?\d{4}[- ]?\d{4}[- ]?\d{4}\b"),
        "Credit card number",
    ),
    Pattern(
        "ip",
        re.compile(r"\b(?!127\.0\.0\.1\b)(?!0\.0\.0\.0\b)\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b"),
        "IP address",
    ),
]

SECURITY_PATTERNS: list[Pattern] = [
    Pattern("api_key_sk", re.compile(r"sk-[a-zA-Z0-9_-]{20,}"), "API secret key (sk-...)"),
    Pattern("api_key_pk", re.compile(r"pk_(?:live|test)_[a-zA-Z0-9]{20,}"), "Publishable key (pk_...)"),
    Pattern("aws_key", re.compile(r"AKIA[0-9A-Z]{16}"), "AWS Access Key ID"),
    Pattern(
        "password",
        re.compile(r"(?:password|passwd|pwd)\s*[=:]\s*[\"'][^\"']{4,}[\"']", re.IGNORECASE),
        "Hardcoded password",
    ),
    Pattern(
        "token_assign",
        re.compile(r"(?:token|secret|api_key|apikey|auth_token)\s*[=:]\s*[\"'][^\"']{8,}[\"']", re.IGNORECASE),
        "Hardcoded token/secret",
    ),
    Pattern("jwt", re.compile(r"eyJ[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}"), "JWT token"),
    Pattern(
        "private_key",
        re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"),
        "Private key",
    ),
    Pattern("gcp_key", re.compile(r"\"type\"\s*:\s*\"service_account\""), "GCP service account key"),
    Pattern(
        "azure_conn",
        re.compile(r"(?:AccountKey|SharedAccessKey)\s*=\s*[A-Za-z0-9+/=]{20,}", re.IGNORECASE),
        "Azure connection string",
    ),
    Pattern("github_token", re.compile(r"gh[pousr]_[A-Za-z0-9_]{36,}"), "GitHub token"),
    Pattern(
        "telegram_token",
        re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b"),
        "Telegram bot token",
    ),
]

# Patterns that skip the "looks like a variable reference" check (password/token
# assigned to a shell variable expansion like `token = "$VAR"`), mirroring pre-push.
_VAR_REF_SKIP_NAMES = {"password", "token_assign"}
_VAR_REF_RE = re.compile(r"[=:]\s*[\"']\$")


# ---------------------------------------------------------------------------
# File-skip rules (mirrors .githooks/pre-push's should_skip_file)
# ---------------------------------------------------------------------------

_SKIP_EXACT_DIR_PREFIXES = ("tests/", "test/", "spec/")
_SKIP_MID_PATH_MARKERS = ("/test/", "/tests/", "/spec/")
_SKIP_DOC_EXTENSIONS = (".md", ".mdx", ".rst", ".txt", ".adoc")
_SKIP_BINARY_EXTENSIONS = (
    "png", "jpg", "jpeg", "gif", "ico", "svg", "woff", "woff2", "ttf", "eot",
    "mp3", "mp4", "zip", "tar", "gz", "bz2", "xz", "pdf",
    "pyc", "whl", "egg", "so", "dylib", "dll", "exe", "o", "a", "class",
    "lock",
)


def should_skip_file(filepath: str) -> bool:
    if filepath.startswith(".githooks/"):
        return True
    if filepath.startswith(_SKIP_EXACT_DIR_PREFIXES):
        return True
    if any(marker in filepath for marker in _SKIP_MID_PATH_MARKERS):
        return True
    if filepath.endswith(_SKIP_DOC_EXTENSIONS):
        return True
    ext = filepath.rsplit(".", 1)[-1].lower() if "." in filepath else ""
    if ext in _SKIP_BINARY_EXTENSIONS:
        return True
    return False


def is_placeholder_value(line: str) -> bool:
    return bool(PLACEHOLDER_VALUE_RE.search(line))


def load_allowlist(repo_root: str | Path) -> list[tuple[str, str]]:
    """Return [(filepath_glob, pattern_desc_substring), ...] from security-allowlist.txt."""
    path = Path(repo_root) / ".githooks" / "security-allowlist.txt"
    entries: list[tuple[str, str]] = []
    if not path.is_file():
        return entries
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return entries
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        filepath, _, pattern_desc = line.partition(":")
        entries.append((filepath, pattern_desc))
    return entries


def is_allowlisted(allowlist: list[tuple[str, str]], filepath: str, pattern_desc: str) -> bool:
    filepath = filepath[2:] if filepath.startswith("./") else filepath
    for allowed_file, allowed_pattern in allowlist:
        if fnmatch.fnmatch(filepath, allowed_file) and allowed_pattern in pattern_desc:
            return True
    return False


# ---------------------------------------------------------------------------
# Diff parsing (mirrors .githooks/pre-push's scan_diff loop)
# ---------------------------------------------------------------------------

_DIFF_GIT_RE = re.compile(r"^diff --git")
_PLUS_PLUS_PLUS_RE = re.compile(r"^\+\+\+ b/(.+)$")
_HUNK_HEADER_RE = re.compile(r"^@@ [^+]*\+(\d+)")

DEFAULT_SCAN_TIMEOUT_SECONDS = 10.0


def scan_diff(
    diff_text: str,
    repo_root: str | Path | None = None,
    timeout_seconds: float = DEFAULT_SCAN_TIMEOUT_SECONDS,
) -> tuple[list[Finding], bool]:
    """Scan a unified diff's added lines for PII/secrets/instance data.

    Returns (findings, timed_out). Mirrors .githooks/pre-push's scan_diff():
    only '+' (added) lines are scanned, tracked per-file/per-line-number from
    the diff headers; context lines advance the line counter without being
    scanned; removed ('-') lines are ignored entirely.
    """
    findings: list[Finding] = []
    if not diff_text:
        return findings, False

    instance_patterns = build_instance_patterns()
    name_patterns = _load_name_patterns()
    allowlist = load_allowlist(repo_root) if repo_root else []

    current_file = ""
    line_num = 0
    start = time.monotonic()
    timed_out = False

    for raw_line in diff_text.splitlines():
        if timeout_seconds and (time.monotonic() - start) >= timeout_seconds:
            timed_out = True
            break

        if _DIFF_GIT_RE.match(raw_line):
            current_file = ""
            line_num = 0
            continue

        m = _PLUS_PLUS_PLUS_RE.match(raw_line)
        if m:
            current_file = m.group(1)
            line_num = 0
            continue

        hm = _HUNK_HEADER_RE.match(raw_line)
        if hm:
            line_num = int(hm.group(1)) - 1
            continue

        if raw_line.startswith(" "):
            line_num += 1
            continue

        if raw_line == "+" or (raw_line.startswith("+") and not raw_line.startswith("++")):
            line_num += 1
            content = raw_line[1:]
            fp = current_file or "<unknown>"

            if current_file and should_skip_file(current_file):
                continue

            has_nosec = bool(NOSEC_RE.search(content))
            has_noname = bool(NONAME_RE.search(content))

            if not has_nosec:
                for pat in instance_patterns:
                    if pat.regex.search(content):
                        if is_allowlisted(allowlist, fp, pat.description):
                            continue
                        findings.append(Finding(fp, line_num, pat.description, content))

                for pat in PII_PATTERNS:
                    match = pat.regex.search(content)
                    if not match:
                        continue
                    if pat.name == "email" and EMAIL_ALLOWLIST_RE.search(content):
                        continue
                    if pat.name == "ip" and PRIVATE_IP_RE.search(content):
                        continue
                    if is_placeholder_value(content):
                        continue
                    if is_allowlisted(allowlist, fp, pat.description):
                        continue
                    findings.append(Finding(fp, line_num, pat.description, content))

                for pat in SECURITY_PATTERNS:
                    if not pat.regex.search(content):
                        continue
                    if pat.name in _VAR_REF_SKIP_NAMES and _VAR_REF_RE.search(content):
                        continue
                    if is_placeholder_value(content):
                        continue
                    if is_allowlisted(allowlist, fp, pat.description):
                        continue
                    findings.append(Finding(fp, line_num, pat.description, content))

            if not has_noname:
                for pat in name_patterns:
                    if not pat.regex.search(content):
                        continue
                    if pat.allowlist_re and pat.allowlist_re.search(content):
                        continue
                    if is_allowlisted(allowlist, fp, pat.description):
                        continue
                    findings.append(Finding(fp, line_num, pat.description, content))
            continue

        # Removed lines ('-' but not '---') and other diff metadata: ignored.

    return findings, timed_out
