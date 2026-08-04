#!/usr/bin/env python3
"""
PreToolUse hook: enforce exact dependency pinning across the Lobster system.

Blocks any Edit/Write/NotebookEdit that would introduce an unpinned (range-
style) dependency version into a manifest file, and blocks any Bash command
that would let a package manager silently resolve to a newer-than-currently-
pinned version.

**Covered manifest files** (by basename / suffix, wherever they live under the
tree Claude is operating in — not restricted to one directory):
  - package.json                          (npm/yarn/pnpm)
  - pyproject.toml                        (uv/pip/hatch/setuptools deps)
  - requirements*.txt                     (pip)
  - Pipfile                               (pipenv)

**What triggers a block (Edit/Write/NotebookEdit):**
  - A dependency version string using a range operator instead of an exact
    pin: `^`, `~`, `~=`, `>=`, `<=`, `!=`, `>`, `<`, `*`, or the literal
    string "latest" — in a package.json dependency value, or in a
    pyproject.toml / requirements.txt / Pipfile version specifier.
  - Detection runs against the changed text only (`new_string` for Edit,
    `content` for Write/NotebookEdit) — this is a heuristic regex scan, not a
    full parse, matching the style of hooks/secret-scanner.py and
    hooks/system-file-protect.py. False negatives (an edit that manages to
    smuggle a range past the regex) are possible; false positives on
    unrelated text are unlikely because the regex requires the range
    operator to sit immediately inside a quoted value or after a bare
    package-name token, which is the shape of an actual dependency line.

**What triggers a block (Bash):**
  - Any package-manager invocation that can silently resolve to "whatever is
    newest right now" instead of an exact, already-decided version:
      npm/pnpm/yarn: `install|i|add <pkg>` with no `@<version>` suffix,
                      or `update`/`upgrade`/`up` (any form, with or without
                      a package name)
      pip:            `install <pkg>` with no `==`, or `-U`/`--upgrade`
      uv:             `add <pkg>` with no `==`, `sync`/`lock` with
                      `-U`/`--upgrade`/`--upgrade-package`,
                      `pip install <pkg>` with no `==`

**What does NOT trigger a block:**
  - `npm install` / `npm ci` / `pnpm install` / `yarn install` with no
    package name (installs from the existing lockfile — does not upgrade).
  - `uv sync` / `uv lock` with no upgrade flag (resolves from pyproject.toml
    pins / regenerates the lock without bumping anything).
  - `pip install -r <file>` / `uv pip install -r <file>` (installs from a
    requirements file; the file itself is covered by the Edit/Write check
    above).
  - Any command or edit that specifies an exact version (`==`, `@1.2.3`,
    or a bare exact version with no operator).
  - Edits to files that are not dependency manifests.

**Deliberately out of scope:** generated lockfiles themselves
(package-lock.json, uv.lock, pnpm-lock.yaml, yarn.lock) are NOT blocked from
being edited/written — regenerating them via `uv lock` / `npm install
--package-lock-only` after a manifest change is expected and desired. Only
the *manifest* files (and the Bash commands that mutate them or bypass them)
are gated. Lockfile *content* still reflects whatever the pinned manifest
resolves to, so an attacker/mistake would have to also flip the manifest
pin, which this hook blocks.

**Escape hatch (explicit, reviewed override):**
Set `LOBSTER_ALLOW_DEPENDENCY_CHANGE=true` in the environment for the one
command/edit that is a deliberate, reviewed dependency bump, then unset it.
This is a SEPARATE variable from `LOBSTER_DEBUG` on purpose: LOBSTER_DEBUG is
already set to "true" persistently in this system's settings.json (it exists
to unlock system-file-protect.py for normal dev work), so gating on it here
would make this hook a permanent no-op. Requiring a distinct, purpose-built
override keeps dependency bumps a conscious, separate decision.
"""

import json
import os
import re
import sys
from pathlib import Path

_ENV_OVERRIDE = "LOBSTER_ALLOW_DEPENDENCY_CHANGE"

DENY_REASON_EDIT = (
    "Blocked: {path!r} would introduce an unpinned dependency version "
    "({match!r}). Lobster policy requires every dependency to be pinned to "
    "an exact version (no ^, ~, >=, <=, >, <, *, or \"latest\" ranges) so "
    "installs can never silently pull a newer version. Pin to the exact "
    "version you intend to use. If this is a deliberate, reviewed version "
    "bump, re-run with LOBSTER_ALLOW_DEPENDENCY_CHANGE=true set."
)

DENY_REASON_BASH = (
    "Blocked: this command can silently resolve to a newer package version "
    "than what is currently pinned ({command!r}). Use an exact version "
    "(e.g. `npm install pkg@1.2.3`, `pip install pkg==1.2.3`, "
    "`uv add pkg==1.2.3`) or install from the existing lockfile/manifest "
    "instead. If this is a deliberate, reviewed version bump, re-run with "
    "LOBSTER_ALLOW_DEPENDENCY_CHANGE=true set."
)

# ---------------------------------------------------------------------------
# Manifest file detection
# ---------------------------------------------------------------------------

_MANIFEST_RE = re.compile(
    r"(^|/)(package\.json|pyproject\.toml|requirements[^/]*\.txt|Pipfile)$"
)


def is_dependency_manifest(file_path: str) -> bool:
    if not file_path:
        return False
    return bool(_MANIFEST_RE.search(file_path))


def _is_package_json(file_path: str) -> bool:
    return file_path.endswith("package.json")


# ---------------------------------------------------------------------------
# Range-operator detection in manifest text
# ---------------------------------------------------------------------------

# package.json: a quoted dependency value starting with a range operator,
# e.g. "chokidar": "^3.6.0" or "qrcode": "~1.5.4" or "foo": "latest"
_JSON_RANGE_RE = re.compile(
    r'"\s*:\s*"\s*(\^|~(?!=)|>=|<=|!=|>|<|\*|latest)',
    re.IGNORECASE,
)

# pyproject.toml / requirements.txt / Pipfile: a package token immediately
# followed by a range operator, e.g. "mcp>=1.0.0" or mcp>=1.0.0 or
# package[extra]~=2.0
_PY_RANGE_RE = re.compile(
    r"[A-Za-z0-9_.\-]+(?:\[[A-Za-z0-9_,\-]+\])?\s*(>=|<=|~=|!=|>|<|\^)\s*[0-9]"
)

# A bare "*" or "latest" as an entire pinned-looking value (Pipfile style:
# requests = "*")
_STAR_LATEST_RE = re.compile(r'=\s*"(\*|latest)"', re.IGNORECASE)


def find_unpinned_range(text: str, is_package_json: bool) -> str | None:
    """Return the offending match text if `text` contains an unpinned
    dependency range, else None."""
    if is_package_json:
        m = _JSON_RANGE_RE.search(text)
        if m:
            # Return a small window of context around the match for the
            # error message.
            start = max(0, m.start() - 20)
            return text[start : m.end() + 10].strip()
        return None

    m = _PY_RANGE_RE.search(text)
    if m:
        return m.group(0)
    m = _STAR_LATEST_RE.search(text)
    if m:
        return m.group(0)
    return None


# ---------------------------------------------------------------------------
# Bash command detection
# ---------------------------------------------------------------------------

# Split a compound shell command into sub-commands on common separators.
_SUB_CMD_SPLIT_RE = re.compile(r"[|;&\n]|\band\b|\bor\b")

# npm/pnpm/yarn: install|i|add <pkg-with-no-@version>, or any update/upgrade
_NODE_INSTALL_RE = re.compile(r"^\s*(npm|pnpm|yarn)\s+(install|i|add)\b(.*)$")
_NODE_UPGRADE_RE = re.compile(r"^\s*(npm|pnpm|yarn)\s+(update|upgrade|up)\b")

# pip: install <pkg-with-no-==>, or -U/--upgrade anywhere
_PIP_INSTALL_RE = re.compile(r"^\s*(pip3?|uv\s+pip)\s+install\b(.*)$")
_PIP_UPGRADE_FLAG_RE = re.compile(r"(^|\s)(-U|--upgrade)(\s|$)")

# uv add <pkg-with-no-==>
_UV_ADD_RE = re.compile(r"^\s*uv\s+add\b(.*)$")
# uv sync/lock with an upgrade flag
_UV_SYNC_LOCK_RE = re.compile(r"^\s*uv\s+(sync|lock)\b(.*)$")
_UV_UPGRADE_FLAG_RE = re.compile(
    r"(^|\s)(-U|--upgrade|--upgrade-package(=\S+)?)(\s|$)"
)


def _tokens_are_all_flags_or_files(rest: str) -> bool:
    """True if every whitespace-delimited token in `rest` is a flag
    (starts with -) or a requirements-file reference — i.e. no bare package
    name is being installed."""
    tokens = [t for t in rest.split() if t]
    for tok in tokens:
        if tok.startswith("-"):
            continue
        # `-r requirements.txt` — the filename itself following -r/-e is fine
        continue
    return True  # handled by caller via a simpler heuristic; see below


def _node_package_args_unpinned(rest: str) -> list[str]:
    """Return package names in `rest` that lack an explicit @version."""
    unpinned = []
    for tok in rest.split():
        if tok.startswith("-"):
            continue
        # scoped or unscoped package name, optionally with @version.
        # e.g. "lodash", "lodash@4.17.21", "@scope/pkg", "@scope/pkg@1.0.0"
        # A version is present only if there is an "@" after position 0
        # (scoped packages start with "@" at position 0, which is not a
        # version marker).
        at_idx = tok.rfind("@")
        if at_idx > 0:
            version = tok[at_idx + 1 :]
            if version and version[0].isdigit():
                continue  # pinned, e.g. pkg@1.2.3
        unpinned.append(tok)
    return unpinned


def _pip_uv_package_args_unpinned(rest: str) -> list[str]:
    """Return package names in `rest` that lack an explicit ==version."""
    unpinned = []
    skip_next = False
    for tok in rest.split():
        if skip_next:
            skip_next = False
            continue
        if tok in ("-r", "--requirement", "-e", "--editable", "-c",
                    "--constraint", "--index-url", "--extra-index-url"):
            skip_next = True
            continue
        if tok.startswith("-"):
            continue
        if tok in (".", "..") or tok.startswith("./") or tok.startswith("../"):
            continue
        if "==" in tok:
            continue
        if tok.startswith("git+") or tok.startswith("http://") or tok.startswith("https://"):
            # VCS/URL installs are already an explicit, fully-specified
            # reference (commit/tag in the URL) — not a silent-latest risk
            # in the same sense as a bare "requests" pull.
            continue
        unpinned.append(tok)
    return unpinned


def bash_introduces_unpinned_dependency(command: str) -> str | None:
    """Return a description of the offending fragment if `command` could
    silently resolve to a newer-than-pinned dependency version, else None."""
    for sub in _SUB_CMD_SPLIT_RE.split(command):
        sub = sub.strip()
        if not sub:
            continue

        if _NODE_UPGRADE_RE.search(sub):
            return sub

        m = _NODE_INSTALL_RE.match(sub)
        if m:
            rest = m.group(3)
            unpinned = _node_package_args_unpinned(rest)
            if unpinned:
                return sub

        if _PIP_UPGRADE_FLAG_RE.search(sub) and re.search(r"\bpip3?\b", sub):
            return sub

        m = _PIP_INSTALL_RE.match(sub)
        if m:
            rest = m.group(2)
            unpinned = _pip_uv_package_args_unpinned(rest)
            if unpinned:
                return sub

        m = _UV_ADD_RE.match(sub)
        if m:
            rest = m.group(1)
            unpinned = _pip_uv_package_args_unpinned(rest)
            if unpinned:
                return sub

        m = _UV_SYNC_LOCK_RE.match(sub)
        if m and _UV_UPGRADE_FLAG_RE.search(m.group(2)):
            return sub

    return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _is_override_set() -> bool:
    return os.environ.get(_ENV_OVERRIDE, "").lower() == "true"


def _deny(reason: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))
    sys.exit(0)


def _scan_file_cli(path: str) -> None:
    """CLI mode used by hooks/pre-commit (git commit-time check, independent
    of the PreToolUse path above — catches manifest edits made outside
    Claude Code's Edit/Write tools, e.g. a plain shell editor or `git apply`).

    Usage: pin-dependencies-guard.py --scan-file <path> [--text -]
    Reads manifest text from stdin (the staged blob content, as passed by
    `git show :path` in hooks/pre-commit) and exits 1 with a message on
    stderr if an unpinned range is found, else exits 0 silently.
    """
    if _is_override_set():
        sys.exit(0)
    text = sys.stdin.read()
    match = find_unpinned_range(text, _is_package_json(path))
    if match:
        print(
            f"pin-dependencies-guard: {path!r} contains an unpinned "
            f"dependency version ({match!r}). Pin to an exact version, or "
            f"set LOBSTER_ALLOW_DEPENDENCY_CHANGE=true for a deliberate, "
            f"reviewed bump.",
            file=sys.stderr,
        )
        sys.exit(1)
    sys.exit(0)


def main() -> None:
    if len(sys.argv) >= 3 and sys.argv[1] == "--scan-file":
        _scan_file_cli(sys.argv[2])
        return

    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    if _is_override_set():
        sys.exit(0)

    tool_name = data.get("tool_name", "")
    tool_input = data.get("tool_input", {})

    if tool_name == "Bash":
        command = str(tool_input.get("command", ""))
        offending = bash_introduces_unpinned_dependency(command)
        if offending:
            _deny(DENY_REASON_BASH.format(command=offending))
        sys.exit(0)

    if tool_name not in ("Edit", "Write", "NotebookEdit"):
        sys.exit(0)

    file_path = str(tool_input.get("file_path", ""))
    if not is_dependency_manifest(file_path):
        sys.exit(0)

    if tool_name == "Edit":
        text = str(tool_input.get("new_string", ""))
    else:
        # Write and NotebookEdit both carry the new content under
        # "content" for our purposes (NotebookEdit's cell "new_source" is
        # not a manifest format, so this branch is realistically Write-only
        # for manifest files; NotebookEdit is included in the matcher only
        # for symmetry with system-file-protect.py's convention).
        text = str(tool_input.get("content", tool_input.get("new_source", "")))

    if not text:
        sys.exit(0)

    match = find_unpinned_range(text, _is_package_json(file_path))
    if match:
        _deny(DENY_REASON_EDIT.format(path=file_path, match=match))

    sys.exit(0)


if __name__ == "__main__":
    main()
