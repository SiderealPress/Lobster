"""
Drift-detection test: hooks/git_push_scan.py (Python) must stay in sync with
.githooks/pre-push (bash), the source of truth these patterns were ported
from.

.githooks/pre-push is bash and hooks/git_push_scan.py is Python -- they
cannot literally share one source file across that language boundary (see
the module docstring in git_push_scan.py for why). Instead, this test parses
.githooks/pre-push's pattern-table declarations directly (`name@@regex@@desc`
entries in PII_PATTERNS / SECURITY_PATTERNS) and asserts the Python module
declares the same pattern names with the same human-readable descriptions.

If a future change adds, removes, or renames a pattern on one side without
updating the other, this test fails -- that is its entire purpose.
"""
import importlib.util
import re
import sys
from pathlib import Path


def _load_git_push_scan():
    hooks_dir = Path(__file__).parent.parent.parent.parent / "hooks"
    spec = importlib.util.spec_from_file_location("git_push_scan", hooks_dir / "git_push_scan.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["git_push_scan"] = mod
    spec.loader.exec_module(mod)
    return mod


def _repo_root() -> Path:
    return Path(__file__).parent.parent.parent.parent


def _parse_bash_array(bash_text: str, array_name: str) -> dict[str, str]:
    """Extract {pattern_name: description} from a bash `NAME=(\n 'a@@b@@c'\n ...)` array.

    Only pulls the static entries (single-quoted or double-quoted string
    literals) -- the dynamically-built INSTANCE_PATTERNS/NAME_PATTERNS entries
    that vary per user-config are intentionally out of scope for this parity
    check (both implementations already read the same config files at run
    time; see git_push_scan.py's module docstring).
    """
    m = re.search(rf"^{array_name}=\((.*?)^\)", bash_text, re.MULTILINE | re.DOTALL)
    assert m, f"could not find bash array {array_name} in .githooks/pre-push"
    body = m.group(1)

    entries: dict[str, str] = {}
    for line_match in re.finditer(r"""^\s*["'](.+?)["']\s*$""", body, re.MULTILINE):
        literal = line_match.group(1)
        parts = literal.split("@@")
        if len(parts) < 3:
            continue
        name, _regex, desc = parts[0], parts[1], parts[2]
        entries[name] = desc
    return entries


def test_pii_patterns_match_bash_source_of_truth():
    gps = _load_git_push_scan()
    bash_text = (_repo_root() / ".githooks" / "pre-push").read_text()
    bash_patterns = _parse_bash_array(bash_text, "PII_PATTERNS")

    python_patterns = {p.name: p.description for p in gps.PII_PATTERNS}

    assert python_patterns == bash_patterns, (
        "git_push_scan.PII_PATTERNS has drifted from .githooks/pre-push's PII_PATTERNS. "
        "Update whichever side is stale so both scan for the same things."
    )


def test_security_patterns_match_bash_source_of_truth():
    gps = _load_git_push_scan()
    bash_text = (_repo_root() / ".githooks" / "pre-push").read_text()
    bash_patterns = _parse_bash_array(bash_text, "SECURITY_PATTERNS")

    python_patterns = {p.name: p.description for p in gps.SECURITY_PATTERNS}

    assert python_patterns == bash_patterns, (
        "git_push_scan.SECURITY_PATTERNS has drifted from .githooks/pre-push's SECURITY_PATTERNS. "
        "Update whichever side is stale so both scan for the same things."
    )


def test_parity_check_is_falsifiable():
    """Prove the parser+comparison actually detects a real mismatch, so the
    two tests above are not vacuously passing due to a parsing bug."""
    fake_bash_text = (
        "PII_PATTERNS=(\n"
        "    'totally_new@@foo@@Totally new PII kind'\n"
        ")\n"
    )
    bash_patterns = _parse_bash_array(fake_bash_text, "PII_PATTERNS")
    gps = _load_git_push_scan()
    python_patterns = {p.name: p.description for p in gps.PII_PATTERNS}
    assert python_patterns != bash_patterns
