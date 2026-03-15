"""Tests for md_to_html() — Markdown-to-HTML conversion used before Telegram send."""
import sys
import os

# Allow importing the bot module without triggering Telegram env checks
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("TELEGRAM_ALLOWED_USERS", "12345")

import pytest

# Import only the pure function, not the whole bot (which starts the app)
import importlib.util, types

_BOT_PATH = os.path.join(os.path.dirname(__file__), "../../../src/bot/lobster_bot.py")


def _load_md_to_html():
    """Load md_to_html from the bot module without executing module-level side effects."""
    import re

    spec = importlib.util.spec_from_file_location("lobster_bot_partial", _BOT_PATH)
    # Read and extract just the function source to avoid import-time side effects
    with open(_BOT_PATH) as f:
        source = f.read()

    # Execute only up to the first non-function/non-import statement
    # We grab the function by compiling the relevant lines
    ns: dict = {"re": re}
    # Extract the md_to_html function definition
    import ast
    tree = ast.parse(source)
    func_def = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "md_to_html"
    )
    func_source = ast.get_source_segment(source, func_def)
    exec(compile(func_source, _BOT_PATH, "exec"), ns)  # noqa: S102
    return ns["md_to_html"]


md_to_html = _load_md_to_html()


# ---------------------------------------------------------------------------
# Headers
# ---------------------------------------------------------------------------

class TestHeaders:
    def test_h1_becomes_bold(self):
        assert md_to_html("# Title") == "<b>Title</b>"

    def test_h2_becomes_bold(self):
        assert md_to_html("## Section") == "<b>Section</b>"

    def test_h3_becomes_bold(self):
        assert md_to_html("### Subsection") == "<b>Subsection</b>"

    def test_header_mid_text(self):
        result = md_to_html("Intro\n\n## Section\n\nBody text")
        assert "<b>Section</b>" in result
        assert "Intro" in result
        assert "Body text" in result

    def test_header_strips_hashes_only(self):
        result = md_to_html("## Hello World")
        assert result == "<b>Hello World</b>"
        assert "#" not in result

    def test_multiple_headers(self):
        result = md_to_html("## First\n## Second")
        assert "<b>First</b>" in result
        assert "<b>Second</b>" in result

    def test_header_not_triggered_without_space(self):
        # ##nospace should not become a header (standard Markdown spec)
        result = md_to_html("##nospace")
        assert "<b>" not in result


# ---------------------------------------------------------------------------
# Horizontal rules
# ---------------------------------------------------------------------------

class TestHorizontalRules:
    def test_triple_dash_removed(self):
        result = md_to_html("Before\n---\nAfter")
        assert "---" not in result
        assert "Before" in result
        assert "After" in result

    def test_longer_dash_run_removed(self):
        result = md_to_html("Before\n------\nAfter")
        assert "------" not in result

    def test_hr_inline_not_removed(self):
        # A dash sequence that is not on its own line should be preserved
        result = md_to_html("some --- text")
        assert "---" in result

    def test_hr_with_trailing_spaces(self):
        result = md_to_html("Before\n---   \nAfter")
        assert "---" not in result


# ---------------------------------------------------------------------------
# Existing behaviour must be preserved
# ---------------------------------------------------------------------------

class TestExistingBehaviourPreserved:
    def test_bold_double_star(self):
        assert md_to_html("**bold**") == "<b>bold</b>"

    def test_italic_underscore(self):
        assert md_to_html("_italic_") == "<i>italic</i>"

    def test_link(self):
        assert md_to_html("[click](https://example.com)") == '<a href="https://example.com">click</a>'

    def test_inline_code(self):
        assert md_to_html("`code`") == "<code>code</code>"

    def test_code_block_not_formatted(self):
        result = md_to_html("```\n## not a header\n---\n```")
        assert "<b>" not in result
        assert "## not a header" in result

    def test_html_entities_escaped(self):
        result = md_to_html("a < b & c > d")
        assert "&lt;" in result
        assert "&amp;" in result
        assert "&gt;" in result
