#!/usr/bin/env python3
"""PreToolUse hook: blocks agent-initiated `git push` on the public repo when
the outgoing diff contains likely PII/secrets/instance-specific data.

## Why this hook exists

`.githooks/pre-push` already scans every push for PII and instance-specific
data, and blocks interactively -- but only when stdin is a TTY (a human at a
terminal). When a Claude Code agent (dispatcher or subagent) runs `git push`
via its `Bash` tool, there is no TTY: `.githooks/pre-push` detects CI mode,
prints its findings, and lets the push through anyway (see the `IS_TTY`
branch in that script). That is a real, currently-live gap: agent-initiated
pushes get weaker protection than human-initiated ones, for a reason (no TTY
to prompt) a Claude Code PreToolUse hook does not share.

This hook closes that gap using the mechanism this codebase already uses
elsewhere (`hooks/link-checker.py`, `hooks/require-write-result.py`,
`hooks/require-background-agent.py`): a PreToolUse hook that exits 2 to block
the tool call, with a stderr message Claude Code injects into the *same*
agent's own next turn. The agent that ran `git push` is the same agent that
must now assess the finding and decide to fix-and-retry or explain-and-retry.

## No Anthropic API calls

This hook is pure local regex (via `hooks/git_push_scan.py`, ported from
`.githooks/pre-push`'s pattern tables) plus a handful of local `git`
subprocess calls to compute the outgoing diff. It never calls the Anthropic
API or any other network service. The semantic judgment step ("is this
finding real or a false positive") is deferred entirely to the calling
agent's own next turn -- not embedded in this hook.

## Scope guard

Only acts on `git push`-shaped Bash commands whose target remote resolves to
the public `SiderealPress/lobster` repo. Any other repo (a client project, a
scratch worktree with a different origin, etc.) is skipped silently (exit 0)
-- this hook has no opinion about pushes that aren't this repo.

## Retry-counter / fail-open convention

Mirrors `hooks/require-write-result.py`: a bounded fire counter (keyed by
session/agent id) tracked in a `/tmp/lobster-git-push-guard-fires-*` file.
After `MAX_HOOK_FIRES` consecutive blocks on the same push attempt without
resolution, the hook fails open (exit 0, allow the push) rather than
wedging the agent in a false-positive loop forever -- the same policy
`.githooks/pre-push` already applies to its own scan-timeout case.

Exit codes:
  0 - allow the tool call (not a git push, not this repo, clean diff, or
      fail-open after MAX_HOOK_FIRES)
  2 - block the tool call (stderr message injected into the agent's turn)
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import git_push_scan  # noqa: E402

# Maximum number of consecutive blocks on the same push attempt before
# failing open. Mirrors require-write-result.py's MAX_HOOK_FIRES convention,
# but a push finding is expected to resolve in 1-2 turns (fix or explain),
# so the bound here is smaller than the write_result retry bound.
MAX_HOOK_FIRES = 3

_GIT_PUSH_RE = re.compile(r"(?:^|[;&|]\s*)git\s+push\b")

# Public repo this guard is scoped to. Matches both the https and ssh remote
# URL forms, with or without a trailing ".git".
_PUBLIC_REPO_RE = re.compile(
    r"(?:github\.com[:/])SiderealPress/lobster(?:\.git)?/?$", re.IGNORECASE
)

_FAILOPEN_LOG_FILENAME = "agent-git-push-guard-failopen.jsonl"


def _read_stdin_json() -> dict:
    try:
        return json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return {}


def _is_git_push_command(command: str) -> bool:
    return bool(_GIT_PUSH_RE.search(command))


def _extract_remote_name(command: str) -> str:
    """Best-effort extraction of the remote name from a `git push` command.

    Falls back to "origin" (the overwhelming common case in this codebase's
    worktree convention) when no explicit remote is given or parsing fails.
    """
    m = _GIT_PUSH_RE.search(command)
    if not m:
        return "origin"

    rest = command[m.end():]
    # Stop at the next shell control operator so we don't parse into a
    # subsequent chained command.
    rest = re.split(r"[;&|]", rest, maxsplit=1)[0]

    tokens = rest.split()
    # Flags that take a following value we should skip over.
    value_flags = {"-o", "--push-option"}
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in value_flags:
            i += 2
            continue
        if tok.startswith("-"):
            i += 1
            continue
        return tok
    return "origin"


def _get_repo_root(cwd: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _remote_url(repo_root: str, remote_name: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", repo_root, "remote", "get-url", remote_name],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _is_public_lobster_remote(repo_root: str, remote_name: str) -> bool:
    url = _remote_url(repo_root, remote_name)
    if not url:
        return False
    return bool(_PUBLIC_REPO_RE.search(url))


def _run_git(args: list[str], repo_root: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", repo_root, *args],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout


def _get_push_diff(repo_root: str) -> str:
    """Mirrors .githooks/pre-push's get_push_diff(): upstream range first,
    falling back to a diff against origin/main, then the full HEAD log."""
    diff_text = _run_git(["log", "@{u}..HEAD", "--no-merges", "-p"], repo_root)
    if diff_text.strip():
        return diff_text
    diff_text = _run_git(["diff", "origin/main..HEAD"], repo_root)
    if diff_text.strip():
        return diff_text
    return _run_git(["log", "-p", "HEAD"], repo_root)


def _agent_key(data: dict) -> str:
    key = data.get("agent_id") or data.get("session_id") or "unknown"
    return "".join(c if c.isalnum() or c in "-._" else "_" for c in key)


def _fire_count_path(agent_key: str) -> Path:
    return Path(f"/tmp/lobster-git-push-guard-fires-{agent_key}")


def _increment_fire_count(agent_key: str) -> int:
    path = _fire_count_path(agent_key)
    try:
        count = int(path.read_text().strip())
    except (OSError, ValueError):
        count = 0
    count += 1
    try:
        path.write_text(str(count))
    except OSError:
        pass
    return count


def _cleanup_fire_state(agent_key: str) -> None:
    try:
        _fire_count_path(agent_key).unlink(missing_ok=True)
    except OSError:
        pass


def _default_failopen_log_path() -> Path:
    workspace = Path(os.environ.get("LOBSTER_WORKSPACE", str(Path.home() / "lobster-workspace")))
    return workspace / "logs" / _FAILOPEN_LOG_FILENAME


def _log_failopen_event(reason: str, findings_count: int) -> None:
    """Append one durable, best-effort JSONL record. Never affects exit code."""
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "hook": "agent-git-push-guard",
        "reason": reason,
        "findings_count": findings_count,
    }
    try:
        target = _default_failopen_log_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except OSError:
        pass


def _format_block_message(findings: list, fire_count: int) -> str:
    lines = [
        f"BLOCKED [agent-git-push-guard]: possible PII/secrets detected in this push "
        f"(attempt {fire_count}/{MAX_HOOK_FIRES}):",
        "",
    ]
    for f in findings:
        lines.append(f"  - {f.format()}")
    lines += [
        "",
        "Assess whether each finding is real or a false positive.",
        "  - If real: fix the offending content in your working tree and retry the push.",
        "  - If a false positive: say why in your next message and retry -- this will be logged.",
        "",
        "This push targets the public SiderealPress/lobster repo. To permanently allowlist a "
        "known false positive, add an entry to .githooks/security-allowlist.txt "
        "(format: filepath:pattern_description).",
    ]
    return "\n".join(lines)


def main() -> None:
    data = _read_stdin_json()

    if data.get("tool_name") != "Bash":
        sys.exit(0)

    tool_input = data.get("tool_input", {})
    command = str(tool_input.get("command", ""))
    if not _is_git_push_command(command):
        sys.exit(0)

    cwd = data.get("cwd") or os.getcwd()
    repo_root = _get_repo_root(cwd)
    if repo_root is None:
        sys.exit(0)

    remote_name = _extract_remote_name(command)
    if not _is_public_lobster_remote(repo_root, remote_name):
        sys.exit(0)

    agent_key = _agent_key(data)

    diff_text = _get_push_diff(repo_root)
    if not diff_text.strip():
        _cleanup_fire_state(agent_key)
        sys.exit(0)

    findings, timed_out = git_push_scan.scan_diff(diff_text, repo_root=repo_root)

    if timed_out and not findings:
        print(
            "WARNING [agent-git-push-guard]: scan timed out before completing -- "
            "allowing push with partial scan results only.",
            file=sys.stderr,
        )
        sys.exit(0)

    if not findings:
        _cleanup_fire_state(agent_key)
        sys.exit(0)

    fire_count = _increment_fire_count(agent_key)

    if fire_count > MAX_HOOK_FIRES:
        _cleanup_fire_state(agent_key)
        _log_failopen_event("max_hook_fires_exceeded", len(findings))
        print(
            f"WARNING [agent-git-push-guard]: {fire_count - 1} consecutive blocks on this push "
            "without resolution -- failing open (allowing push) per fail-open policy. "
            f"{len(findings)} finding(s) remain unresolved; this has been logged to "
            f"{_default_failopen_log_path()}.",
            file=sys.stderr,
        )
        sys.exit(0)

    print(_format_block_message(findings, fire_count), file=sys.stderr)
    sys.exit(2)


if __name__ == "__main__":
    main()
