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
  - For non-JSON manifests (pyproject.toml / requirements*.txt / Pipfile),
    whole-line comments (`# ...`) are excluded from the scan first, so prose
    that merely mentions a range in passing (e.g. `# needs numpy>=1.20
    installed separately`) is not treated as a dependency declaration. An
    inline trailing comment on a real dependency line does not suppress
    detection of that line.

**What triggers a block (Bash):**
  - Any package-manager invocation that can silently resolve to "whatever is
    newest right now" instead of an exact, already-decided version:
      npm/pnpm/yarn: `install|i|add <pkg>` with no `@<version>` suffix,
                      or `update`/`upgrade`/`up` (any form, with or without
                      a package name)
      pip:            `install <pkg>` with no `==`, or `-U`/`--upgrade`,
                      including via `python -m pip` / `python3 -m pip`
      uv:             `add <pkg>` with no `==`, `sync`/`lock` with
                      `-U`/`--upgrade`/`--upgrade-package`,
                      `pip install <pkg>` with no `==`,
                      `run --with <pkg>` with no `==` (installs an
                      ephemeral, unpinned dependency for the run)
  - Detection resolves the actual binary being invoked rather than matching
    only the literal string "pip"/"npm"/"uv" at position 0: a leading `env`
    or `command` wrapper, a leading `VAR=val` assignment prefix, and a path
    prefix on the command itself (`/usr/bin/pip`, `venv/bin/pip`,
    `node_modules/.bin/npm`) are all normalized away first, so none of these
    ordinary invocation forms bypass the check.
  - A compound command is split into sub-commands on real shell separators
    only (`|`, `;`, `&`, newline) — the bare English words "and"/"or" are
    NOT treated as separators, since they are not shell operators. Heredoc
    bodies (`<<EOF ... EOF`) are treated as literal data, not sub-commands,
    so example install-command *text* embedded in a heredoc's file content
    (e.g. `cat > f.sh <<EOF`) does not get independently evaluated as a real
    invocation — UNLESS the heredoc is being fed to a real interpreter
    (`bash <<EOF`, `sh <<EOF`, `zsh <<EOF`, `ssh host <<EOF`, `cat <<EOF |
    bash`, etc.), in which case the body genuinely executes as shell code and
    is still scanned line-by-line for real install invocations.
  - Any code string that is actually handed to an interpreter for execution
    is recursively scanned with these same rules, no matter how it is
    embedded in the outer command: a shell/interpreter's `-c "<code>"` (or
    `-c '<code>'`) argument (`bash -c "pip install requests"`, including
    nested through wrapper commands like `docker exec bash -c "..."` or
    `sudo bash -c "..."`, and double-nested like `bash -c "sh -c \"...\""`),
    a here-string body (`bash <<< "pip install requests"`), an `eval
    "<code>"` argument, `$(...)`/backtick command substitution, and a
    literal `echo "<text>" | <interpreter>` / `printf "<text>" | <interpreter>`
    pipe. These are genuinely-executed code, unlike a heredoc's inert
    file-content body (`cat > f.sh <<EOF`) — the distinction that matters is
    whether the text is handed to an interpreter to run, not merely the
    syntax used to embed it. Deliberately NOT covered, and not coverable by
    any static text scan: piping an *opaque* producer into a shell (`cat
    unknown-file | bash`, `curl url | bash`) — the risk there is unknown
    runtime content, not a shell-syntax gap.

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
  - `npm install <local-path>` (e.g. `./local-pkg`, `../sibling`,
    `/abs/path`) or an already-fully-specified tarball/URL/VCS reference —
    not a registry resolution that can silently drift to a newer version.
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

# package.json dependency-block keys that actually get resolved/installed by
# npm/yarn/pnpm. Deliberately EXCLUDES "engines" (and similar environment-
# constraint blocks like "engines-strict", "volta", "packageManager") — those
# specify a compatible runtime range for humans/tooling to check against, not
# a package version npm/yarn/pnpm resolves and installs, so a range there
# (e.g. "node": ">=18.0.0") is normal and is not the silent-upgrade risk this
# hook exists to catch.
_JSON_DEPENDENCY_KEYS = {
    "dependencies",
    "devDependencies",
    "peerDependencies",
    "optionalDependencies",
    "resolutions",
    "overrides",
}

# package.json: a quoted "<key>": "<range-value>" pair, e.g.
# "chokidar": "^3.6.0" or "qrcode": "~1.5.4" or "foo": "latest". Captures the
# key so callers can exclude known non-dependency keys (see
# _JSON_DEPENDENCY_KEYS above) when the surrounding block can't be
# determined (i.e. for Edit fragments, where a full JSON parse of the
# surrounding object usually isn't possible).
_JSON_RANGE_RE = re.compile(
    r'"([A-Za-z0-9_@/.\-]+)"\s*:\s*"\s*(\^|~(?!=)|>=|<=|!=|>|<|\*|latest)',
    re.IGNORECASE,
)

# Keys that are never themselves a dependency name, even though their value
# looks like a version range — these are the environment-constraint blocks
# called out above. Matches on these keys are ignored.
_JSON_NON_DEPENDENCY_KEYS = {"node", "npm", "yarn", "pnpm", "vscode"}

# pyproject.toml / requirements.txt / Pipfile: a package token immediately
# followed by a range operator, e.g. "mcp>=1.0.0" or mcp>=1.0.0 or
# package[extra]~=2.0
_PY_RANGE_RE = re.compile(
    r"[A-Za-z0-9_.\-]+(?:\[[A-Za-z0-9_,\-]+\])?\s*(>=|<=|~=|!=|>|<|\^)\s*[0-9]"
)

# A bare "*" or "latest" as an entire pinned-looking value (Pipfile style:
# requests = "*")
_STAR_LATEST_RE = re.compile(r'=\s*"(\*|latest)"', re.IGNORECASE)

# A whole-line comment (TOML/requirements.txt style: '#' as the first
# non-whitespace character on the line). Prose like
# "# needs numpy>=1.20 installed separately" merely mentions a version range
# in passing — it is not a dependency declaration, so it must not be scanned.
# Inline trailing comments (`mcp>=1.0.0  # note`) are left alone: the real
# declaration earlier on the same line still needs to be caught.
_FULL_LINE_COMMENT_RE = re.compile(r"^[ \t]*#.*$", re.MULTILINE)


def _strip_full_line_comments(text: str) -> str:
    """Blank out lines that are wholly a comment, preserving line count/
    offsets (so this is safe to apply before either python-style regex)."""
    return _FULL_LINE_COMMENT_RE.sub("", text)


def _find_unpinned_range_in_package_json(text: str) -> str | None:
    """package.json-specific check. Prefers a real JSON parse (possible when
    `text` is a whole-file Write) restricted to actual dependency-block keys
    — this is exact, with no risk of flagging "engines" or similar
    environment-constraint blocks. Falls back to a key-aware regex scan
    (needed for Edit fragments, which are partial and rarely valid JSON on
    their own), which still excludes the known non-dependency key names in
    _JSON_NON_DEPENDENCY_KEYS (e.g. "engines": {"node": ">=18.0.0"})."""
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        parsed = None

    if isinstance(parsed, dict):
        for block_key in _JSON_DEPENDENCY_KEYS:
            block = parsed.get(block_key)
            if not isinstance(block, dict):
                continue
            for pkg, version in block.items():
                if not isinstance(version, str):
                    continue
                if re.match(
                    r"^\s*(\^|~(?!=)|>=|<=|!=|>|<|\*|latest)",
                    version,
                    re.IGNORECASE,
                ):
                    return f'"{pkg}": "{version}"'
        return None

    # Fragment fallback: regex scan, excluding known non-dependency keys.
    for m in _JSON_RANGE_RE.finditer(text):
        key = m.group(1)
        if key in _JSON_NON_DEPENDENCY_KEYS:
            continue
        start = max(0, m.start() - 5)
        return text[start : m.end() + 10].strip()
    return None


def find_unpinned_range(text: str, is_package_json: bool) -> str | None:
    """Return the offending match text if `text` contains an unpinned
    dependency range, else None."""
    if is_package_json:
        return _find_unpinned_range_in_package_json(text)

    text = _strip_full_line_comments(text)
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

# Split a compound shell command into sub-commands on REAL shell separators
# only: pipe, semicolon, background '&' (this also splits '&&'/'||' since
# each char in the class is matched individually, leaving an empty fragment
# between them, which is filtered out below), and newline. Deliberately does
# NOT split on the bare English words "and"/"or" — those are not shell
# operators (that was a false-positive bug: `echo hello or npm install foo`
# is a single `echo` invocation with literal args, not two commands).
_SUB_CMD_SPLIT_RE = re.compile(r"[|;&\n]")

# A heredoc opener, e.g. `<<EOF`, `<<-EOF`, `<<'EOF'`, `<<"EOF"`.
_HEREDOC_START_RE = re.compile(r"<<-?\s*(['\"]?)(\w+)\1")

# Real command/script interpreters that will EXECUTE a heredoc body as code
# rather than treat it as inert data (contrast with `cat > f.sh <<EOF`, where
# the body is file content). If any of these appears as a whitespace-
# delimited token on the same physical line as the heredoc opener — either
# as the command consuming the heredoc directly (`bash <<EOF`, `ssh host
# <<EOF`) or as the far end of a pipe on that same line (`cat <<EOF |
# bash`) — the heredoc body is real, executable shell code and must still be
# scanned for install invocations.
_INTERPRETER_NAMES = {
    "bash", "sh", "zsh", "ksh", "dash", "ssh",
    "python", "python2", "python3",
}


def _line_invokes_interpreter(line: str) -> bool:
    """True if `line` contains a shell/script interpreter as a distinct
    command token (matched by exact basename after stripping any path
    prefix, e.g. `/bin/bash` or `venv/bin/python3`), so a filename that
    merely ends in these letters (e.g. `f.sh`) does not false-positive
    match."""
    for tok in line.split():
        name = tok.rsplit("/", 1)[-1]
        if name in _INTERPRETER_NAMES:
            return True
    return False


def _mask_heredocs(command: str) -> str:
    """Blank out heredoc body lines (between a `<<DELIM` marker and the line
    containing that bare DELIM) before sub-command splitting — UNLESS the
    heredoc is being fed to a real interpreter (see _line_invokes_interpreter),
    in which case the body is left intact so it still gets split into
    sub-commands and scanned.

    A heredoc body is normally literal data being redirected into a command
    (e.g. file content for `cat > f.sh <<EOF ... EOF`), not additional shell
    commands — treating each of its lines as an independent sub-command (as
    a naive `\\n` split would) causes false positives when the body merely
    contains example install-command *text*. But `bash <<EOF ... EOF` (or
    `sh`/`zsh`/`ssh host`/a pipe into one of these, e.g. `cat <<EOF |
    bash`) genuinely executes its body as shell code — masking it there
    would silently hide a real, unpinned install (this was a regression:
    the original naive `\\n` split caught this case, purely by accident,
    before heredoc-awareness was added). The line with the opening marker
    itself (a real command) is always preserved; only body lines are
    conditionally blanked, so line offsets elsewhere in the string are
    unaffected either way."""
    lines = command.split("\n")
    out = []
    i = 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        m = _HEREDOC_START_RE.search(line)
        if m:
            delim = m.group(2)
            executed = _line_invokes_interpreter(line)
            i += 1
            while i < len(lines) and lines[i].strip() != delim:
                out.append(lines[i] if executed else "")
                i += 1
            if i < len(lines):
                out.append("")  # blank the closing delimiter line too
                i += 1
            continue
        i += 1
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Recursive scanning of code payloads handed directly to an interpreter for
# execution: `-c "<code>"`, here-strings (`<<<`), `eval "<code>"`, and
# `$(...)`/backtick command substitution.
#
# Round 5 bypass (independent review, PR #2151, issuecomment-5187771322):
# `bash -c "pip install requests"` and `bash <<< "pip install requests"` were
# both silently ALLOWED. Root cause: `_HEREDOC_START_RE` never matches a
# here-string (`<<<` is not `<<`), and `-c "<code>"` never enters heredoc
# handling at all — the whole thing is one sub-command starting with
# `bash`/`sh`/`zsh`, so the quoted code string inside is never independently
# scanned by the install-detection regexes below (which are anchored at
# sub-command start, not applied recursively to quoted string arguments).
#
# Fix: before the existing sub-command-split logic runs, pull out every code
# string that is genuinely handed to an interpreter for execution — a `-c`
# argument, a here-string body, an `eval` argument, or a `$(...)`/backtick
# substitution — and recursively run this same detection on each one. This
# is intentionally a search (not an anchored match) so the interpreter
# invocation can appear anywhere in the command (`docker exec bash -c "..."`,
# `sudo bash -c "..."`), and it is intentionally recursive so nested forms
# (`bash -c "sh -c \"pip install requests\""`) resolve one layer at a time:
# each recursive call unescapes one level of quoting and re-scans, so the
# innermost real command is what ultimately gets matched against the
# install-detection regexes.
# ---------------------------------------------------------------------------

_INTERPRETER_ALT = r"(?:bash|sh|zsh|ksh|dash|python[0-9.]*)"

# `<interpreter> [flags] -c "<code>"` / `-c '<code>'`, wherever it appears in
# the command (not anchored) so wrapper commands (`docker exec`, `sudo`,
# `ssh host`, etc.) preceding the interpreter don't hide it. The closing
# quote must not be escaped (`(?<!\\)`), so a nested/escaped quote inside the
# code (`bash -c "sh -c \"...\""`) is not mistaken for the end of the string.
_DASH_C_PAYLOAD_RE = re.compile(
    r"(?:^|[\s;|&()])(?:[\w./\-]*/)?" + _INTERPRETER_ALT + r"\b"
    r"(?:\s+-{1,2}[A-Za-z][\w-]*)*"
    r"\s+-c\s+"
    r"(['\"])(.*?)(?<!\\)\1",
    re.DOTALL,
)

# `<interpreter> ... <<< "<code>"` / `<<< '<code>'` / `<<< bareword` — a
# here-string, whose body is a real argument fed to the interpreter's stdin,
# not inert heredoc file content.
_HERE_STRING_PAYLOAD_RE = re.compile(
    r"(?:^|[\s;|&()])(?:[\w./\-]*/)?" + _INTERPRETER_ALT + r"\b"
    r"[^\n<]*?<<<\s*"
    r"(?:(['\"])(.*?)(?<!\\)\1|(\S+))",
    re.DOTALL,
)

# `eval "<code>"` / `eval '<code>'` — eval always executes its argument.
_EVAL_PAYLOAD_RE = re.compile(
    r"(?:^|[\s;|&(])eval\s+(['\"])(.*?)(?<!\\)\1",
    re.DOTALL,
)

# `$(<code>)` / `` `<code>` `` command substitution. Non-greedy: this hook is
# a heuristic regex scan (see module docstring), not a shell parser, so
# balanced/nested parens inside `$(...)` are out of scope — realistic
# single-level substitutions are what this guards against.
_CMD_SUBST_RE = re.compile(r"\$\((.*?)\)", re.DOTALL)
_BACKTICK_RE = re.compile(r"`([^`]*)`", re.DOTALL)

# `echo "<text>" | <interpreter>` / `printf "<text>" | <interpreter>` — a
# literal string produced by echo/printf and piped straight into a shell is
# executed exactly like a here-string (`<<< "<text>"`); it is the same bug
# class as the round-5 bypass, just spelled with a pipe instead of `<<<`.
# Deliberately narrow: only a literal quoted/bareword argument to echo/printf
# is statically knowable. A producer whose output can't be known ahead of
# execution (`cat somefile | bash`, `curl url | bash`) is NOT — and cannot
# be made — coverable by this or any other static text scan, since the
# actual danger is in unknown-until-runtime content, not in shell syntax
# this hook failed to parse. That gap is a disclosed, inherent limitation
# of static analysis (see module docstring), not a bug in this regex.
_ECHO_PRINTF_PIPE_RE = re.compile(
    r"(?:^|[\s;|&])(?:echo|printf)\s+(?:-\S+\s+)*"
    r"(?:(['\"])(.*?)(?<!\\)\1|([^|]+?))"
    r"\s*\|\s*(?:[\w./\-]*/)?" + _INTERPRETER_ALT + r"\b",
    re.DOTALL,
)


def _unescape_shell_quotes(s: str) -> str:
    """Undo one level of `\\"` / `\\'` escaping, so a payload extracted from
    inside an outer quoted string (e.g. the inner `sh -c \\"...\\"` of
    `bash -c "sh -c \\"...\\""`) can be re-scanned by the same regexes on
    the next recursive call as if it were typed directly."""
    return s.replace('\\"', '"').replace("\\'", "'")


def _extract_executed_payloads(command: str) -> list[str]:
    """Return every code string in `command` that is actually handed to an
    interpreter/eval/subshell for execution — as opposed to inert heredoc
    file content — so the caller can recursively scan each one."""
    payloads: list[str] = []
    for m in _DASH_C_PAYLOAD_RE.finditer(command):
        payloads.append(_unescape_shell_quotes(m.group(2)))
    for m in _HERE_STRING_PAYLOAD_RE.finditer(command):
        code = m.group(2) if m.group(2) is not None else m.group(3)
        if code:
            payloads.append(_unescape_shell_quotes(code))
    for m in _EVAL_PAYLOAD_RE.finditer(command):
        payloads.append(_unescape_shell_quotes(m.group(2)))
    for m in _CMD_SUBST_RE.finditer(command):
        payloads.append(m.group(1))
    for m in _BACKTICK_RE.finditer(command):
        payloads.append(m.group(1))
    for m in _ECHO_PRINTF_PIPE_RE.finditer(command):
        code = m.group(2) if m.group(2) is not None else m.group(3)
        if code:
            payloads.append(_unescape_shell_quotes(code.strip()))
    return payloads


# Recursion guard: each recursive call operates on a strictly shorter payload
# extracted from inside its parent's quoting, so real input always
# terminates quickly — this bound only protects against a pathological
# edge case, not normal nesting depths.
_MAX_PAYLOAD_RECURSION_DEPTH = 8


# Leading wrapper commands that pass through to a real argv without
# themselves being the package manager: `env FOO=bar pip install x`,
# `command npm install x`, or a bare `FOO=bar pip install x` assignment
# prefix (valid shell syntax with no wrapper word at all). Stripped
# iteratively so `env command pip install x` also resolves.
_WRAPPER_WORD_RE = re.compile(r"^(?:env|command)\b\s*")
_LEADING_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=\S*\s+")


def _strip_wrappers(sub: str) -> str:
    s = sub
    while True:
        m = _WRAPPER_WORD_RE.match(s)
        if m:
            s = s[m.end():]
            continue
        m = _LEADING_ASSIGNMENT_RE.match(s)
        if m:
            s = s[m.end():]
            continue
        break
    return s


# Resolve the actual binary being invoked rather than pattern-matching only
# the literal string "pip"/"npm"/"uv" sitting at position 0: a path prefix
# (`/usr/bin/pip`, `venv/bin/pip`, `node_modules/.bin/npm`) must not defeat
# detection, since none of that changes what actually runs.
_FIRST_TOKEN_RE = re.compile(r"^(\s*)(\S+)(.*)$", re.DOTALL)


def _normalize_command_name(sub: str) -> str:
    """If the leading token is a path (contains '/'), replace it with just
    its basename so path-prefixed invocations are recognized the same as
    the bare command."""
    m = _FIRST_TOKEN_RE.match(sub)
    if not m:
        return sub
    lead, token, rest = m.groups()
    if "/" in token:
        token = token.rsplit("/", 1)[-1]
    return f"{lead}{token}{rest}"


# `python -m pip install ...` / `python3 -m pip install ...` is one of the
# most commonly recommended ways to invoke pip and must be treated exactly
# like a bare `pip install`.
_PYTHON_DASH_M_PIP_RE = re.compile(
    r"^(\s*)(python[0-9]*(?:\.[0-9]+)?)\s+-m\s+pip\b(.*)$", re.DOTALL
)


def _normalize_python_dash_m_pip(sub: str) -> str:
    m = _PYTHON_DASH_M_PIP_RE.match(sub)
    if not m:
        return sub
    lead, _pybin, rest = m.groups()
    return f"{lead}pip{rest}"


def _resolve_subcommand(sub: str) -> str:
    """Normalize a sub-command so the pattern matchers below see the actual
    binary/subcommand being invoked, regardless of wrapper prefixes, path
    prefixes, or the `python -m pip` invocation form."""
    sub = _strip_wrappers(sub)
    sub = _normalize_command_name(sub)
    sub = _normalize_python_dash_m_pip(sub)
    sub = _normalize_command_name(sub)
    return sub


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

# uv run --with <pkg> — installs an ephemeral, unpinned dependency for the
# duration of the run, the same silent-latest risk as `uv add`/`pip install`
# with no version pin. `--with-requirements <file>` is a different flag
# (installs from a file, covered by the Edit/Write check on that file) and
# is deliberately NOT matched here since nothing directly follows "--with".
_UV_RUN_RE = re.compile(r"^\s*uv\s+run\b(.*)$", re.DOTALL)
_UV_RUN_WITH_PKG_RE = re.compile(r"--with(?:=|\s+)(\S+)")


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
        # A local filesystem path (`./local-pkg`, `../sibling`, `/abs/path`)
        # or an already-fully-specified tarball/URL/VCS reference is not a
        # registry resolution that can silently drift to "latest" — the
        # exact code is whatever is at that path/URL right now, unaffected
        # by npm's version-resolution step this hook guards against.
        if (
            tok.startswith("./")
            or tok.startswith("../")
            or tok.startswith("/")
            or tok.startswith("~/")
            or tok.startswith("git+")
            or tok.startswith("git://")
            or tok.startswith("http://")
            or tok.startswith("https://")
            or tok.endswith((".tgz", ".tar.gz"))
        ):
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


def _uv_run_with_unpinned(rest: str) -> list[str]:
    """Return `--with` package specs in a `uv run ...` invocation's tail
    that lack an explicit `==version`."""
    unpinned = []
    for m in _UV_RUN_WITH_PKG_RE.finditer(rest):
        for spec in m.group(1).split(","):
            spec = spec.strip()
            if not spec or spec.startswith("-"):
                continue
            if "==" in spec:
                continue
            if spec.startswith("./") or spec.startswith("../") or spec.startswith("/"):
                continue
            unpinned.append(spec)
    return unpinned


def bash_introduces_unpinned_dependency(command: str, _depth: int = 0) -> str | None:
    """Return a description of the offending fragment if `command` could
    silently resolve to a newer-than-pinned dependency version, else None.

    Before the sub-command split below, recursively scan every code payload
    that is genuinely handed to an interpreter for execution (a `-c`
    argument, a here-string body, an `eval` argument, or `$(...)`/backtick
    command substitution) — see the "Recursive scanning of code payloads"
    section above for why this is necessary and how it terminates."""
    if _depth <= _MAX_PAYLOAD_RECURSION_DEPTH:
        for payload in _extract_executed_payloads(command):
            found = bash_introduces_unpinned_dependency(payload, _depth + 1)
            if found:
                return found

    command = _mask_heredocs(command)
    for raw_sub in _SUB_CMD_SPLIT_RE.split(command):
        raw_sub = raw_sub.strip()
        if not raw_sub:
            continue

        # Resolve wrapper/path/`-m pip` forms to the canonical command shape
        # the pattern matchers below expect, but report the original text
        # back to the caller so the deny message shows what was actually
        # typed.
        sub = _resolve_subcommand(raw_sub)

        if _NODE_UPGRADE_RE.search(sub):
            return raw_sub

        m = _NODE_INSTALL_RE.match(sub)
        if m:
            rest = m.group(3)
            unpinned = _node_package_args_unpinned(rest)
            if unpinned:
                return raw_sub

        if _PIP_UPGRADE_FLAG_RE.search(sub) and re.search(r"\bpip3?\b", sub):
            return raw_sub

        m = _PIP_INSTALL_RE.match(sub)
        if m:
            rest = m.group(2)
            unpinned = _pip_uv_package_args_unpinned(rest)
            if unpinned:
                return raw_sub

        m = _UV_ADD_RE.match(sub)
        if m:
            rest = m.group(1)
            unpinned = _pip_uv_package_args_unpinned(rest)
            if unpinned:
                return raw_sub

        m = _UV_SYNC_LOCK_RE.match(sub)
        if m and _UV_UPGRADE_FLAG_RE.search(m.group(2)):
            return raw_sub

        m = _UV_RUN_RE.match(sub)
        if m:
            unpinned = _uv_run_with_unpinned(m.group(1))
            if unpinned:
                return raw_sub

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
