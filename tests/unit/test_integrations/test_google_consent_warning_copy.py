"""
Tests for the BIS-743 "unverified app" warning-message copy tweak.

Guards the doc-only change to the three Google connect-flow skill docs
(gcal-links, gmail, google-workspace `behavior/system.md`): the consent-link
message now proactively mentions Google's "unverified app" warning screen so
the user isn't alarmed by it.

This test module exists specifically to catch a bug an independent review
pass (Fable) found during BIS-743: an early version of this copy tweak left
a literal, un-interpolated ``(app name)`` placeholder in the user-facing
string (not inside an f-string, so it would never be substituted with the
real app name — Telegram users would have seen the literal text
"Go to (app name) (unsafe)"). These tests pin the fixed wording and prevent
that class of bug from silently regressing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).parent.parent.parent.parent

_SKILL_DOC_PATHS = [
    _REPO_ROOT / "lobster-shop" / "gcal-links" / "behavior" / "system.md",
    _REPO_ROOT / "lobster-shop" / "gmail" / "behavior" / "system.md",
    _REPO_ROOT / "lobster-shop" / "google-workspace" / "behavior" / "system.md",
]


@pytest.mark.parametrize("doc_path", _SKILL_DOC_PATHS, ids=lambda p: p.parent.parent.name)
def test_unverified_app_warning_present(doc_path: Path):
    """Each consent-flow skill doc mentions the unverified-app warning screen."""
    text = doc_path.read_text()
    assert "unverified app" in text.lower(), (
        f"{doc_path} does not mention Google's 'unverified app' warning screen"
    )


@pytest.mark.parametrize("doc_path", _SKILL_DOC_PATHS, ids=lambda p: p.parent.parent.name)
def test_no_unfilled_placeholder_in_warning_copy(doc_path: Path):
    """Regression guard: no literal, un-interpolated '(app name)' placeholder.

    An early draft of this copy tweak wrote a plain string containing
    "(app name)" as a stand-in for the real app name, but never actually
    interpolated it (it wasn't inside an f-string) — so the literal text
    "(app name)" would have been sent to real users. Fable's review caught
    this before merge; this test pins the fix.
    """
    text = doc_path.read_text()
    assert "(app name)" not in text, (
        f"{doc_path} contains an unfilled '(app name)' placeholder in "
        "user-facing message copy"
    )


@pytest.mark.parametrize("doc_path", _SKILL_DOC_PATHS, ids=lambda p: p.parent.parent.name)
def test_warning_copy_is_syntactically_valid_python_string(doc_path: Path):
    """The reply string containing the warning must be valid, parseable Python.

    Guards against unescaped quote characters inside the string literal
    (e.g. a raw '"Go to ... (unsafe)"' embedded in an already-double-quoted
    Python string, which would be a SyntaxError at runtime).
    """
    import ast

    text = doc_path.read_text()
    # Extract fenced python code blocks and confirm each parses.
    blocks = []
    lines = text.splitlines()
    in_block = False
    current: list[str] = []
    for line in lines:
        if line.strip().startswith("```python"):
            in_block = True
            current = []
            continue
        if line.strip() == "```" and in_block:
            in_block = False
            blocks.append("\n".join(current))
            continue
        if in_block:
            current.append(line)

    assert blocks, f"No python code blocks found in {doc_path}"
    for block in blocks:
        if "unverified app" in block.lower():
            try:
                ast.parse(block)
            except SyntaxError as exc:
                pytest.fail(
                    f"{doc_path}: python block containing warning copy is "
                    f"not valid Python: {exc}"
                )
