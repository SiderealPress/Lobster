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

**Fail-open, by design:** if the scanner cannot run at all — no API key
configured, a network error, a timeout, a malformed response — this hook
WARNS and allows the push through; it does not block. A synchronous git hook
that can fail closed on a transient API hiccup would brick every push on the
system until someone notices and unsets it, which is a worse outcome than an
occasional missed scan. This tradeoff is intentional: the blast radius of
"scan didn't run this one time" is far smaller than "nobody can push
anything, ever, until this hook is fixed or removed."

**Cost/latency:** every scan is a real Opus call — not free and not
instant. Two things keep this proportional: (1) the diff is filtered to
PII-relevant content before it is sent, and if a push touches nothing but
binaries/lockfiles the LLM is never called at all; (2) the diff sent to the
model is capped at _MAX_DIFF_CHARS, with the push still evaluated against the
truncated portion rather than refusing to scan large pushes outright. A
human-visible latency of several seconds to roughly a minute per push that
does contain real code changes is the accepted cost of catching what a regex
cannot, given the stated stakes (real customer PII in a public repo).

Exit codes (matching git's pre-push hook protocol):
  0 - Allow the push (including all warn-mode and fail-open outcomes)
  1 - Block the push (mode=block, confident PII finding)
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_ENV_MODE = "LOBSTER_PII_SCAN_MODE"
_ENV_BYPASS = "LOBSTER_PII_SCAN_SKIP"
_VALID_MODES = ("off", "warn", "block")

_MODEL = "claude-opus-4-8"
_MAX_DIFF_CHARS = 200_000
_SCAN_TIMEOUT_SECONDS = 60

_ZERO_SHA = "0" * 40
_EMPTY_TREE_SHA = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

_ALLOWLIST_FILENAME = ".security-allowlist"

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
    """Call the Opus scanner and return the parsed verdict dict. Raises on
    any failure (network, timeout, malformed response) — callers must catch
    and fail open; this function does not itself decide what "no scan" means
    for the push."""
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


def run(
    stdin_text: str,
    env: dict,
    repo_root: str,
    call_scanner_fn=call_scanner,
) -> tuple[int, str]:
    """Decide the push outcome. Returns (exit_code, message-for-stderr).

    This is the fully-testable core: every side effect (the network call,
    the environment, the stdin payload) is passed in, so callers (tests, or
    main()) control all of it."""
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
            return (
                0,
                "[pii-scan-guard] WARNING: no ANTHROPIC_API_KEY configured — "
                "skipping PII scan (failing open). Set one in config.env to "
                "enable scanning.",
            )

        try:
            result = call_scanner_fn(prompt, api_key)
        except Exception as exc:  # noqa: BLE001 - any scanner failure fails open
            return (
                0,
                f"[pii-scan-guard] WARNING: scanner call failed ({exc}) — "
                "failing open, push allowed. Investigate before relying on "
                "this hook.",
            )

        if result.get("verdict") == "block":
            all_findings.extend(result.get("findings", []))

    if not scanned_anything:
        return 0, ""

    if not all_findings:
        if truncated_any:
            return (
                0,
                "[pii-scan-guard] Note: diff was large and truncated for "
                "scanning — review manually for full coverage.",
            )
        return 0, ""

    message = format_findings_message(all_findings)
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
