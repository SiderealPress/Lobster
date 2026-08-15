#!/usr/bin/env python3
"""
Native git pre-push hook: semantic PII scan of everything about to be pushed.

Unlike a Claude Code PreToolUse hook (which only fires inside a live Claude
Code session, and only for pushes issued through that session's Bash tool),
this is a real `.git/hooks/pre-push` hook — it runs for every `git push`
regardless of how it was invoked (a plain terminal, a script, an IDE, or a
Claude Code Bash tool call), because it fires at the point where content
actually leaves the machine. That is the coverage guarantee a PreToolUse
matcher cannot give: it would be silently bypassed by any push that doesn't
go through this specific session's Bash tool.

**What this hook does NOT duplicate:** secret/credential detection is already
covered by hooks/secret-scanner.py (warn-mode, PreToolUse) and the regex
CRITICAL patterns in scripts/pre-push-security-scan.sh (block-mode, this same
pre-push point). This hook's job is semantic PII judgment specifically —
real personal names/emails/phones/addresses and accidentally committed data
exports (CRM lists, transcripts, contact spreadsheets) — the kind of judgment
call a regex genuinely cannot make (e.g. telling the company name "Eloso"
apart from a real person's name).

**Deliberate divergence from scripts/security-scan-lib.sh's should_skip_file:**
that skip-list exempts .md/.txt/.csv/.json/.rst files entirely, on the theory
that those are documentation. For a PII scan specifically, that is exactly
backwards — a CRM export or contact list is far more likely to land as a
.csv/.json/.txt than as code. This hook only skips binaries and lockfiles
(see _SKIP_EXTENSIONS / _SKIP_BASENAMES below); it does not skip by directory
(tests/) or documentation extension.

**Rollout gate:** controlled by LOBSTER_PII_SCAN_MODE (mirrors the existing
warn/block precedent in hooks/block-claude-p.py):
    off   (default) — hook is installed but does nothing. This is the
                       shipped state: the mechanism exists and is tested, but
                       does not affect any real push until explicitly turned
                       on after review and sign-off.
    warn            — scans and prints findings to stderr, never blocks.
    block           — scans and exits non-zero (blocks the push) on a
                       confident "block" verdict from the scanner.

**Emergency bypass:** LOBSTER_PII_SCAN_SKIP=1 skips the scan entirely for one
push (mirrors SECURITY_SCAN_SKIP=1 in scripts/pre-push-security-scan.sh).

**Allowlist:** reuses the existing `.security-allowlist` file at the repo
root (same file, same format, as scripts/pre-push-security-scan.sh) so there
is one allowlist to maintain, not two. Entries are handed to the scanner as
"already reviewed, known-safe" context rather than used as a post-hoc regex
filter — the model is asked not to flag anything matching an allowlist entry.

**Fail-closed in block mode (explicit decision, overriding the original
fail-open design):** if the scanner cannot run at all — no API key
configured, a network error, a timeout, a malformed response — this hook
BLOCKS the push in `block` mode rather than letting it through. The original
version of this hook failed open here, on the theory that a synchronous git
hook that can fail closed on a transient API hiccup would brick every push on
the system. That tradeoff was reconsidered: for a hook whose entire purpose
is stopping real customer PII from reaching a public repository, "the scan
didn't run" and "the scan ran and found nothing" are not the same outcome,
and treating them the same means a network blip or an expired key silently
disables the one layer of defense that catches what the regex scanner
cannot. `warn` mode is unaffected — a scanner failure there still only warns,
since warn mode's entire purpose is passive observation before an operator
opts into blocking, and it should not start blocking pushes on its own
transient errors before that. `LOBSTER_PII_SCAN_SKIP=1` remains the
deliberate, explicit escape hatch for a push the operator has already
decided is safe.

**Fail-open observability (issue #2167):** every one of the four fail-open
reasons above (missing key, network error, timeout, malformed response) also
appends a durable JSONL record to
`$LOBSTER_WORKSPACE/logs/pii-scan-guard-failopen.jsonl` (see
log_failopen_event / _scanner_unavailable_result), in both `warn` and `block`
mode. Before this, the only trace of a scanner outage was a stderr line on
the developer's terminal at push time -- nothing durable existed to answer
"has this scanner been a no-op for a week?" This is instrumentation only: it
does not add alerting or a cron job (the hook is not installed anywhere yet
as of this writing), it just makes fail-open events visible somewhere that
could be checked or wired into alerting later. The persisted record never
contains the raw exception text: exception-derived reasons are logged as a
fixed-shape "type name + HTTP status" summary
(_summarize_scanner_exception_for_log), with a literal-value redaction
backstop (_redact_secret) for the API key wherever it's in scope -- a
network/timeout/malformed-response failure can otherwise raise or return
almost anything, and nothing upstream guarantees that content excludes
request data.

**Model:** the scanner runs on Claude Fable 5 (`claude-fable-5`), not Opus.
This hook previously ran on Opus, which was shown (see the PR/issue history)
to be foolable by realistic-looking PII wrapped in business, consulting,
partnership, or "test fixture" framing — a named individual's real-looking
contact record labeled as sample/fixture data for an integration test, or as
a vendor/consulting-affiliate onboarding record, was scored "allow" when it
should have blocked. Fable 5 was substituted specifically to close that gap
(verified not to reproduce the same bypass on the same inputs), and the
system prompt was also tightened (see pii-scan-guard.prompt.md) so the
result does not depend on model choice alone.

**Cost/latency:** every scan is a real Fable 5 call — not free and not
instant, and Fable 5 in particular can take meaningfully longer per request
than Opus did on hard/ambiguous inputs (it thinks more before answering).
Two things keep the cost proportional: (1) the diff is filtered to
PII-relevant content before it is sent, and if a push touches nothing but
binaries/lockfiles the LLM is never called at all; (2) the diff sent to the
model is capped at _MAX_DIFF_CHARS, with the push still evaluated against the
truncated portion rather than refusing to scan large pushes outright. Given
the hook now fails closed on a timeout, _SCAN_TIMEOUT_SECONDS is set with
enough headroom for Fable 5's longer turns rather than reusing Opus's budget
— too tight a timeout would turn ordinary slow responses into spurious
blocked pushes.

Exit codes (matching git's pre-push hook protocol):
  0 - Allow the push (warn-mode outcomes, and any outcome while mode="warn"
      or mode="off", including scanner failures in those modes)
  1 - Block the push (mode=block: a confident PII finding, OR the scanner
      failing to produce one at all -- see "Fail-closed in block mode" above)
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_ENV_MODE = "LOBSTER_PII_SCAN_MODE"
_ENV_BYPASS = "LOBSTER_PII_SCAN_SKIP"
_VALID_MODES = ("off", "warn", "block")

_MODEL = "claude-fable-5"
_MAX_DIFF_CHARS = 200_000
# Fable 5 turns can run noticeably longer than Opus did, especially on the
# ambiguous business/consulting-framed inputs this hook exists to catch. Now
# that a timeout fails CLOSED (see module docstring), too tight a budget
# turns an ordinary slow response into a spurious blocked push -- 60s (the
# prior Opus-tuned value) was observed timing out on real scan inputs during
# migration testing.
_SCAN_TIMEOUT_SECONDS = 180

_ZERO_SHA = "0" * 40
_EMPTY_TREE_SHA = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

_ALLOWLIST_FILENAME = ".security-allowlist"

# ---------------------------------------------------------------------------
# Fail-open observability logging (issue #2167)
# ---------------------------------------------------------------------------
# Every fail-open path (see _scanner_unavailable_result below) previously
# degraded silently -- a stderr line most workflows never durably surface.
# These four categories are exactly the four reasons already named in
# _scanner_unavailable_result's own docstring; this does not invent a new
# taxonomy, it just gives each of those reasons a stable machine-readable
# label for the log. Reuses this repo's existing hook-logging convention
# (see hooks/usage-accumulator.py's ~/lobster-workspace/logs/*.jsonl
# append-only pattern) rather than a new mechanism.
_CATEGORY_MISSING_API_KEY = "missing_api_key"
_CATEGORY_NETWORK_ERROR = "network_error"
_CATEGORY_TIMEOUT = "timeout"
_CATEGORY_MALFORMED_RESPONSE = "malformed_response"

_FAILOPEN_LOG_FILENAME = "pii-scan-guard-failopen.jsonl"

# Extensions/basenames that can never carry meaningful PII content and are
# never scanned. Deliberately narrow — see module docstring for why this does
# NOT mirror scripts/security-scan-lib.sh's should_skip_file.
_SKIP_EXTENSIONS = frozenset({
    ".lock", ".min.js", ".min.css", ".map",
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg",
    ".woff", ".woff2", ".ttf", ".eot",
    ".pdf", ".zip", ".tar", ".gz", ".bz2",
    ".exe", ".dll", ".so", ".dylib",
    ".pyc", ".pyo", ".class", ".o", ".a",
})
_SKIP_BASENAMES = frozenset({
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "uv.lock",
    "Cargo.lock", "go.sum", "poetry.lock", "Gemfile.lock",
})

_SYSTEM_PROMPT_PATH = Path(__file__).parent / "pii-scan-guard.prompt.md"

_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["block", "allow"]},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "file": {"type": "string"},
                    "line": {"type": "integer"},
                    "category": {"type": "string"},
                    "snippet": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["file", "line", "category", "snippet", "reason"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["verdict", "findings"],
    "additionalProperties": False,
}


# ---------------------------------------------------------------------------
# Pre-push stdin protocol
# ---------------------------------------------------------------------------
# git feeds pre-push hooks one line per updated ref on stdin:
#   <local ref> SP <local sha1> SP <remote ref> SP <remote sha1> LF

_REF_LINE_RE = re.compile(r"^(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s*$")


def _is_zero_sha(sha: str) -> bool:
    return sha == _ZERO_SHA


def parse_pre_push_refs(stdin_text: str) -> list[tuple[str, str, str, str]]:
    """Parse the pre-push stdin protocol into (local_ref, local_sha,
    remote_ref, remote_sha) tuples. Malformed lines are skipped rather than
    raising, since a hook that crashes on unexpected input fails closed in
    the worst possible way (it would abort the shell pipeline mid-push)."""
    refs = []
    for line in stdin_text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = _REF_LINE_RE.match(line)
        if not m:
            continue
        refs.append(m.groups())
    return refs


# ---------------------------------------------------------------------------
# Diff resolution
# ---------------------------------------------------------------------------


def resolve_diff_base(remote_sha: str, local_sha: str, cwd: str) -> str | None:
    """Return the sha to diff `local_sha` against to see exactly what is
    about to be pushed, or None if there is nothing to diff (no-op update).

    Existing branch update: base is simply the current remote sha — this is
    exactly the commit range that is new to the remote.

    New branch/ref (remote_sha is all-zero): there is no remote commit to
    diff against, so the base is the parent of the oldest commit that isn't
    already reachable from some other remote-tracking ref (i.e. the oldest
    commit that is actually new to the remote as a whole, not just to this
    ref) — falling back to git's empty-tree sha for a root commit with no
    parent, or `local_sha` itself (an empty diff) if nothing new is found.
    """
    if local_sha == remote_sha:
        return None
    if not _is_zero_sha(remote_sha):
        return remote_sha

    try:
        out = subprocess.run(
            ["git", "rev-list", local_sha, "--not", "--remotes"],
            cwd=cwd, capture_output=True, text=True, check=True,
        ).stdout
    except (subprocess.CalledProcessError, OSError):
        return _EMPTY_TREE_SHA

    new_commits = [c for c in out.splitlines() if c]
    if not new_commits:
        return local_sha

    oldest = new_commits[-1]  # git rev-list lists newest-first
    try:
        parent = subprocess.run(
            ["git", "rev-parse", f"{oldest}^"],
            cwd=cwd, capture_output=True, text=True, check=True,
        ).stdout.strip()
        return parent or _EMPTY_TREE_SHA
    except (subprocess.CalledProcessError, OSError):
        # oldest commit has no parent (it's a root commit) -> whole tree is new
        return _EMPTY_TREE_SHA


def get_diff(base: str | None, local_sha: str, cwd: str) -> str:
    if base is None:
        return ""
    try:
        return subprocess.run(
            ["git", "diff", base, local_sha],
            cwd=cwd, capture_output=True, text=True, check=True,
        ).stdout
    except (subprocess.CalledProcessError, OSError):
        return ""


# ---------------------------------------------------------------------------
# Diff filtering (PII-relevant subset)
# ---------------------------------------------------------------------------

_DIFF_HEADER_RE = re.compile(r"^diff --git a/(.*?) b/(.*?)$", re.MULTILINE)


def _split_diff_by_file(diff_text: str) -> list[tuple[str, str]]:
    """Return [(new_path, block_text), ...] for each file in a unified diff."""
    matches = list(_DIFF_HEADER_RE.finditer(diff_text))
    blocks = []
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(diff_text)
        blocks.append((m.group(2), diff_text[start:end]))
    return blocks


def should_skip_file_for_pii_scan(path: str) -> bool:
    """True if `path` can never carry meaningful PII content (binary or a
    generated lockfile) and should be excluded from scanning."""
    name = Path(path).name
    if name in _SKIP_BASENAMES:
        return True
    if Path(path).suffix.lower() in _SKIP_EXTENSIONS:
        return True
    return False


def _is_binary_diff_block(block: str) -> bool:
    return "Binary files" in block and "differ" in block


def filter_diff_for_scan(diff_text: str) -> tuple[str, bool]:
    """Return (filtered_diff_text, truncated). Drops binary/lockfile blocks,
    then caps total size sent to the model at _MAX_DIFF_CHARS."""
    kept = [
        block
        for path, block in _split_diff_by_file(diff_text)
        if not should_skip_file_for_pii_scan(path) and not _is_binary_diff_block(block)
    ]
    filtered = "".join(kept)
    truncated = len(filtered) > _MAX_DIFF_CHARS
    if truncated:
        filtered = filtered[:_MAX_DIFF_CHARS]
    return filtered, truncated


# ---------------------------------------------------------------------------
# Allowlist
# ---------------------------------------------------------------------------


def load_allowlist(repo_root: str) -> list[str]:
    """Load known-safe strings from `.security-allowlist` at the repo root —
    the same file scripts/pre-push-security-scan.sh already uses, so there is
    one allowlist to maintain rather than two."""
    path = Path(repo_root) / _ALLOWLIST_FILENAME
    if not path.is_file():
        return []
    entries = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        entries.append(line)
    return entries


def build_user_prompt(diff_text: str, allowlist: list[str]) -> str:
    allowlist_section = ""
    if allowlist:
        entries = "\n".join(f"- {e}" for e in allowlist)
        allowlist_section = (
            "\n\nKNOWN-SAFE ALLOWLIST (already reviewed and decided safe — do "
            f"not flag any match to these):\n{entries}"
        )
    return (
        "Review the following git diff for content about to be pushed to a "
        "PUBLIC repository. Identify any real personal PII or accidentally "
        "committed data exports per your instructions."
        + allowlist_section
        + "\n\n--- DIFF START ---\n"
        + diff_text
        + "\n--- DIFF END ---"
    )


# ---------------------------------------------------------------------------
# API key resolution (mirrors hooks/secret-scanner.py's config-file search)
# ---------------------------------------------------------------------------


def find_config_file() -> Path | None:
    candidates = []
    config_dir_env = os.environ.get("LOBSTER_CONFIG_DIR", "")
    if config_dir_env:
        candidates.append(Path(config_dir_env) / "config.env")
    home = Path.home()
    candidates += [
        home / "lobster-config" / "config.env",
        home / "lobster" / "config" / "config.env",
    ]
    return next((p for p in candidates if p.is_file()), None)


_ENV_LINE_RE = re.compile(
    r"^(?P<key>[A-Za-z_][A-Za-z0-9_]*)="
    r"(?P<value>\"[^\"]*\"|'[^']*'|[^\s#]*)"
)


def load_api_key(config_path: Path | None, env: dict | None = None) -> str | None:
    """Resolve ANTHROPIC_API_KEY: environment variable takes priority over
    the config file (same precedence the anthropic SDK itself uses)."""
    env = os.environ if env is None else env
    env_key = env.get("ANTHROPIC_API_KEY", "").strip()
    if env_key:
        return env_key
    if config_path is None:
        return None
    try:
        text = config_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        m = _ENV_LINE_RE.match(line.strip())
        if not m or m.group("key") != "ANTHROPIC_API_KEY":
            continue
        value = m.group("value")
        if (value.startswith('"') and value.endswith('"')) or (
            value.startswith("'") and value.endswith("'")
        ):
            value = value[1:-1]
        return value or None
    return None


# ---------------------------------------------------------------------------
# Scanner call (the only network boundary — injected in tests)
# ---------------------------------------------------------------------------


def _load_system_prompt() -> str:
    return _SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")


def call_scanner(user_prompt: str, api_key: str) -> dict:
    """Call the Fable 5 scanner and return the parsed verdict dict. Raises on
    any failure (network, timeout, malformed response) — callers must catch
    and treat that as a scanner failure (fails closed in block mode, warns in
    warn mode — see run()'s _scanner_unavailable_result); this function does
    not itself decide what "no scan" means for the push. Note that raising is
    not the only failure shape callers must guard against: a call that
    returns valid JSON with a "verdict" value other than "block"/"allow" is
    just as much a scanner failure as an exception, and run() treats it the
    same way."""
    import anthropic  # imported lazily: pure-function tests never need this

    client = anthropic.Anthropic(api_key=api_key, timeout=_SCAN_TIMEOUT_SECONDS)
    response = client.messages.create(
        model=_MODEL,
        max_tokens=8096,
        system=_load_system_prompt(),
        thinking={"type": "adaptive"},
        output_config={
            "effort": "high",
            "format": {"type": "json_schema", "schema": _RESPONSE_SCHEMA},
        },
        messages=[{"role": "user", "content": user_prompt}],
    )
    text = next((b.text for b in response.content if b.type == "text"), "")
    return json.loads(text)


# ---------------------------------------------------------------------------
# Findings formatting
# ---------------------------------------------------------------------------


def format_findings_message(findings: list[dict]) -> str:
    lines = [
        f"BLOCKED: pii-scan-guard found {len(findings)} likely PII issue(s) "
        "in this push:",
        "",
    ]
    for f in findings:
        loc = str(f.get("file", "?"))
        line_no = f.get("line")
        if line_no:
            loc += f":{line_no}"
        lines.append(f"  [{f.get('category', 'pii')}] {loc}")
        lines.append(f"    {f.get('snippet', '')!r}")
        lines.append(f"    Reason: {f.get('reason', '')}")
        lines.append("")
    lines.append(
        "If this is a false positive (e.g. a company name or an "
        "already-decided-safe string), add the exact matching text to "
        f"{_ALLOWLIST_FILENAME} in the repo root and push again. If this is "
        "a deliberate, reviewed exception for this one push, set "
        f"{_ENV_BYPASS}=1."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Orchestration (pure-ish core; main() is the thin CLI shell around this)
# ---------------------------------------------------------------------------


def validate_scanner_response(result: object) -> tuple[str, list[dict]] | None:
    """The single validation boundary for the scanner's parsed response.

    `call_scanner` only guarantees the response is valid JSON -- it says
    nothing about its *shape*. `json.loads` happily returns `None`, a list, a
    bare string, or a number for plenty of valid JSON documents, and none of
    those support `.get()`/key lookups the way a dict does. Every place in
    this module that used to reach into `result` directly (a `.get("verdict")`
    call, a `.get("findings", [])` call) now goes through this function
    instead, so there is exactly one place that assumes anything about the
    scanner's structure rather than several scattered defensive checks that
    each only cover the one shape a previous reviewer happened to try.

    Returns `(verdict, findings)` if and only if `result` is a dict with a
    `"verdict"` of `"block"` or `"allow"`, and -- for a `"block"` verdict --
    `"findings"` is a list whose entries are themselves dicts (so
    `format_findings_message`'s `.get()` calls on each entry are safe).
    Returns `None` for absolutely any other shape: not a dict at all, a dict
    missing "verdict", a dict with an unrecognized verdict string, a
    "findings" value that isn't a list, or a "findings" list containing a
    non-dict entry (e.g. a string or number where a finding object should
    be). Callers MUST treat `None` exactly like a raised exception from
    `call_scanner` -- a scanner failure that fails closed in block mode and
    only warns in warn mode (see `_scanner_unavailable_result`)."""
    if not isinstance(result, dict):
        return None
    verdict = result.get("verdict")
    if verdict not in ("block", "allow"):
        return None
    if verdict == "allow":
        return "allow", []
    findings = result.get("findings", [])
    if not isinstance(findings, list) or not all(isinstance(f, dict) for f in findings):
        return None
    return "block", findings


def _classify_scanner_exception(exc: Exception) -> str:
    """Map an exception raised by `call_scanner` onto one of the two
    exception-shaped fail-open categories (network error vs. timeout).

    Deliberately matches on the exception's type name and message rather
    than importing the `anthropic` SDK's specific error classes (e.g.
    `anthropic.APITimeoutError`) -- `call_scanner` imports `anthropic`
    lazily specifically so pure-function tests never need it installed, and
    classification must not force that import at module load time either.
    """
    name = type(exc).__name__.lower()
    text = str(exc).lower()
    if "timeout" in name or "timeout" in text:
        return _CATEGORY_TIMEOUT
    return _CATEGORY_NETWORK_ERROR



# Anchored on a "code"/"status" keyword immediately before the digits so a
# port number (e.g. "api.anthropic.com:443") is never misread as an HTTP
# status -- the anthropic SDK's own error strings read "Error code: 401 -
# ...", which this matches directly.
_HTTP_STATUS_RE = re.compile(r"(?:code|status)[:\s]+([1-5]\d{2})\b", re.IGNORECASE)


def _summarize_scanner_exception_for_log(exc: Exception) -> str:
    """Produce a fixed-shape, secret-free summary of a scanner exception for
    the durable fail-open log: the exception's type name, plus an HTTP
    status code if one appears in the message.

    This deliberately does NOT persist `str(exc)` verbatim. CodeQL flagged
    the original version of this logging (which wrote `str(exc)` -- via the
    `reason` string built two lines below `call_scanner_fn(prompt, api_key)`
    -- straight into the durable JSONL log) as a clear-text-storage-of-
    sensitive-information path: `exc` can be anything the `anthropic` SDK or
    a lower HTTP layer chooses to raise, and nothing guarantees that never
    includes request content (the API key, headers, etc.) -- e.g. a
    client-side validation error or a debug-mode HTTP dump. A fixed-shape
    "type name + status" summary carries enough signal to alert/triage on
    ("the scanner failed N times, mostly TimeoutError") without ever
    reading (and thus risking persisting) the raw exception text. The
    caller-facing stderr message (`_scanner_unavailable_result`'s `reason`
    parameter, printed at push time) is unchanged and still carries the
    full detail for local operator debugging -- only the durable, greppable
    log record is restricted to this summary.
    """
    name = type(exc).__name__
    match = _HTTP_STATUS_RE.search(str(exc))
    if match:
        return f"{name} (status {match.group(1)})"
    return name


def _redact_secret(text: str, secret: str | None) -> str:
    """Defense-in-depth backstop: remove any literal occurrence of `secret`
    (the real API key, when known at the call site) from `text` before it
    is ever persisted to the durable fail-open log. This is a second,
    independent layer under _summarize_scanner_exception_for_log -- it
    covers reasons that were never exception-derived in the first place
    (e.g. the malformed-response reason, built from the scanner's own
    response body) on the off chance the key ever ends up embedded there
    too, and costs nothing when `secret` is falsy (the missing-API-key
    path, where there is no key to leak)."""
    if not secret:
        return text
    return text.replace(secret, "[REDACTED]")


def _default_failopen_log_path(env: dict) -> Path:
    """Resolve the durable fail-open log location from $LOBSTER_WORKSPACE,
    mirroring dispatcher-state-stop.py's _resolve_inflight_path and
    usage-accumulator.py's logs/ convention."""
    workspace = Path(env.get("LOBSTER_WORKSPACE", str(Path.home() / "lobster-workspace")))
    return workspace / "logs" / _FAILOPEN_LOG_FILENAME


def log_failopen_event(
    env: dict,
    category: str,
    reason: str,
    mode: str,
    log_path: Path | str | None = None,
) -> None:
    """Append one durable, greppable JSONL record of a fail-open event.

    This is instrumentation, not part of the decision path: any failure
    writing the record (unwritable directory, log_path pointing at a
    directory, permissions) is swallowed rather than raised or surfaced,
    so a logging problem can never change the hook's exit code or message.
    """
    target = Path(log_path) if log_path is not None else _default_failopen_log_path(env)
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "hook": "pii-scan-guard",
        "category": category,
        "reason": reason,
        "mode": mode,
    }
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except OSError:
        pass


def _scanner_unavailable_result(
    mode: str,
    reason: str,
    category: str,
    env: dict,
    log_path: Path | str | None = None,
    log_reason: str | None = None,
    api_key: str | None = None,
) -> tuple[int, str]:
    """Decide the outcome when the scanner itself could not produce a
    verdict at all -- no API key, a network error, a timeout, a malformed
    response. Anything that lands here means we cannot claim the push is
    clean; the caller never learned "allow" or "block" from the model.

    Fails CLOSED in block mode: not knowing whether a push contains PII is
    treated the same as finding it, because in block mode the whole point of
    this hook is that a push must be affirmatively cleared before it goes
    out. Letting a scanner outage silently downgrade to "allow" would mean a
    network blip or an expired key quietly turns off the one layer of
    defense that catches what the regex scanner cannot.

    Does NOT fail closed in warn mode: warn mode's entire purpose is passive
    observation while an operator validates the hook before opting into
    enforcement, so a scanner outage there should still just warn -- it
    would be surprising and wrong for a hook nobody has enabled blocking on
    yet to start blocking pushes because of its own transient errors.

    Always logs the event (see log_failopen_event, issue #2167) regardless
    of mode -- a scanner outage is exactly as invisible in warn mode as in
    block mode, since warn mode's non-blocking behavior only affects the
    push outcome, not whether the outage itself deserves a durable record.

    The persisted log record is never the raw `reason` string verbatim:
    `log_reason` (a fixed-shape, secret-free summary -- see
    _summarize_scanner_exception_for_log) is used in its place when the
    caller supplies one, and -- regardless -- any literal occurrence of
    `api_key` is stripped as a defense-in-depth backstop (see
    _redact_secret). `reason` itself, and the stderr message built from it
    below, are unchanged: the operator still sees full detail at push time,
    only the durable/greppable log is restricted. See issue #2167 and the
    CodeQL "clear-text storage of sensitive information" finding on the PR
    that added this logging in the first place -- `exc` from a scanner
    failure can be anything the anthropic SDK or a lower HTTP layer raises,
    with no guarantee it excludes request content (including api_key).
    """
    raw_log_reason = log_reason if log_reason is not None else reason
    sanitized_log_reason = _redact_secret(raw_log_reason, api_key)
    log_failopen_event(env=env, category=category, reason=sanitized_log_reason, mode=mode, log_path=log_path)
    remediation = (
        f"Investigate the scanner error, or set {_ENV_BYPASS}=1 to "
        "deliberately bypass this one push if you've reviewed it yourself."
    )
    if mode == "block":
        return (
            1,
            f"[pii-scan-guard] BLOCKED: {reason} — failing closed because "
            f"this push cannot be confirmed free of PII. {remediation}",
        )
    return (
        0,
        f"[pii-scan-guard] WARNING: {reason} — scan did not run this push "
        f"(warn mode does not block). {remediation}",
    )


def run(
    stdin_text: str,
    env: dict,
    repo_root: str,
    call_scanner_fn=call_scanner,
    failopen_log_path: Path | str | None = None,
) -> tuple[int, str]:
    """Decide the push outcome. Returns (exit_code, message-for-stderr).

    This is the fully-testable core: every side effect (the network call,
    the environment, the stdin payload) is passed in, so callers (tests, or
    main()) control all of it. `failopen_log_path` is injected the same way
    `call_scanner_fn` is -- tests pass a tmp_path file; main() leaves it
    None so _scanner_unavailable_result falls back to the real
    $LOBSTER_WORKSPACE/logs location (see _default_failopen_log_path)."""
    mode = env.get(_ENV_MODE, "off").strip().lower()
    if mode == "off":
        return 0, ""
    if mode not in _VALID_MODES:
        return 0, f"[pii-scan-guard] Unknown {_ENV_MODE}={mode!r}; treating as off."

    if env.get(_ENV_BYPASS, "") == "1":
        return 0, f"[pii-scan-guard] SKIPPED ({_ENV_BYPASS}=1)"

    refs = parse_pre_push_refs(stdin_text)
    if not refs:
        return 0, ""

    all_findings: list[dict] = []
    any_block_verdict = False
    truncated_any = False
    scanned_anything = False

    for _local_ref, local_sha, _remote_ref, remote_sha in refs:
        if _is_zero_sha(local_sha):
            continue  # deleting a ref — nothing being pushed

        base = resolve_diff_base(remote_sha, local_sha, repo_root)
        if base is None:
            continue  # no-op ref update

        diff_text = get_diff(base, local_sha, repo_root)
        filtered, truncated = filter_diff_for_scan(diff_text)
        truncated_any = truncated_any or truncated
        if not filtered.strip():
            continue  # nothing scannable (binary/lockfile-only push)

        scanned_anything = True
        allowlist = load_allowlist(repo_root)
        prompt = build_user_prompt(filtered, allowlist)

        api_key = load_api_key(find_config_file(), env)
        if not api_key:
            return _scanner_unavailable_result(
                mode, "no ANTHROPIC_API_KEY configured — cannot run PII scan",
                _CATEGORY_MISSING_API_KEY, env, failopen_log_path,
            )

        try:
            result = call_scanner_fn(prompt, api_key)
        except Exception as exc:  # noqa: BLE001 - any scanner failure fails closed
            return _scanner_unavailable_result(
                mode, f"scanner call failed ({exc})",
                _classify_scanner_exception(exc), env, failopen_log_path,
                log_reason=_summarize_scanner_exception_for_log(exc),
                api_key=api_key,
            )

        validated = validate_scanner_response(result)
        if validated is None:
            # Anything that fails validate_scanner_response -- not a dict at
            # all, an unrecognized verdict, or a findings list/entries with
            # the wrong shape -- is just as much "the scanner did not produce
            # a usable answer" as a raised exception. Treating any of these
            # as "allow" would silently defeat fail-closed for that failure
            # mode, so route through the same _scanner_unavailable_result
            # path (fails closed in block mode, warns in warn mode).
            return _scanner_unavailable_result(
                mode, f"scanner returned an invalid response ({result!r})",
                _CATEGORY_MALFORMED_RESPONSE, env, failopen_log_path,
                api_key=api_key,
            )
        verdict, findings = validated

        if verdict == "block":
            # The verdict itself is what decides block vs allow -- NOT
            # whether findings happens to be non-empty. validate_scanner_response
            # accepts {"verdict": "block", "findings": []} (or a "block"
            # verdict with no "findings" key at all) as a well-formed
            # response: there is no constraint requiring findings to be
            # non-empty when the scanner says "block". Round-4 finding: this
            # flag must be tracked independently of all_findings, because
            # deciding the outcome later from `if not all_findings` (as the
            # code used to) silently downgrades an empty-findings "block"
            # verdict to "allow" -- the exact opposite of what "block" means,
            # with zero warning to the operator.
            any_block_verdict = True
            all_findings.extend(findings)

    if not scanned_anything:
        return 0, ""

    if not any_block_verdict:
        if truncated_any:
            return (
                0,
                "[pii-scan-guard] Note: diff was large and truncated for "
                "scanning — review manually for full coverage.",
            )
        return 0, ""

    if all_findings:
        message = format_findings_message(all_findings)
    else:
        # A "block" verdict with no usable findings details -- still a
        # block, just without specifics to show. An empty findings list must
        # never cause this to fall through to exit 0 / an empty message.
        message = (
            "BLOCKED: pii-scan-guard scanner flagged this push for review "
            "but did not provide specific findings details.\n\n"
            "Investigate manually before proceeding. If this is a "
            "deliberate, reviewed exception for this one push, set "
            f"{_ENV_BYPASS}=1."
        )
    if mode == "warn":
        return 0, f"[pii-scan-guard] WARNING (warn mode, not blocking):\n{message}"
    return 1, message


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _git_repo_root(cwd: str | None = None) -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd, capture_output=True, text=True, check=True,
        ).stdout.strip()
        return out or (cwd or os.getcwd())
    except (subprocess.CalledProcessError, OSError):
        return cwd or os.getcwd()


def main() -> None:
    stdin_text = sys.stdin.read()
    repo_root = _git_repo_root()
    code, message = run(stdin_text, dict(os.environ), repo_root)
    if message:
        print(message, file=sys.stderr)
    sys.exit(code)


if __name__ == "__main__":
    main()
