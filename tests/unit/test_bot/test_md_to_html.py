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
    """Load md_to_html (and its helpers) from the bot module without executing
    module-level side effects."""
    import re

    # Read and extract just the relevant function definitions and constants
    # to avoid import-time side effects (Telegram env checks, app start, etc.)
    with open(_BOT_PATH) as f:
        source = f.read()

    import ast
    tree = ast.parse(source)

    # Names to extract: the constant, both helper functions
    _NAMES_TO_EXTRACT = {"_LONG_URL_THRESHOLD", "_link_to_html", "md_to_html"}

    ns: dict = {"re": re}
    for node in tree.body:
        name = None
        if isinstance(node, ast.FunctionDef):
            name = node.name
        elif isinstance(node, ast.Assign):
            # Simple assignment like _LONG_URL_THRESHOLD = 200
            if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                name = node.targets[0].id
        if name in _NAMES_TO_EXTRACT:
            segment = ast.get_source_segment(source, node)
            exec(compile(segment, _BOT_PATH, "exec"), ns)  # noqa: S102

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


# ---------------------------------------------------------------------------
# Snake_case identifier escaping (issue #430)
# ---------------------------------------------------------------------------

class TestSnakeCaseNotItalicized:
    """Bare underscores in snake_case identifiers must not become italic spans.

    Telegram's HTML mode is used, so the risk is that md_to_html() converts
    _token_ sequences inside snake_case words to <i>token</i>.  The italic
    regex must require that the surrounding underscores are not adjacent to
    word characters.
    """

    def test_two_part_snake_case(self):
        # write_result should not become write<i>result</i>
        result = md_to_html("write_result")
        assert "<i>" not in result
        assert "write_result" in result

    def test_three_part_snake_case(self):
        # STALE_NO_FILE: the middle segment _NO_ must not become italic
        result = md_to_html("STALE_NO_FILE")
        assert "<i>" not in result
        assert "STALE_NO_FILE" in result

    def test_snake_case_in_sentence(self):
        result = md_to_html("Call send_reply to deliver the reply.")
        assert "<i>" not in result
        assert "send_reply" in result

    def test_snake_case_multiple_in_sentence(self):
        result = md_to_html("Use send_reply then write_result.")
        assert "<i>" not in result

    def test_underscore_env_var(self):
        result = md_to_html("Set LOBSTER_ADMIN_CHAT_ID in your env.")
        assert "<i>" not in result
        assert "LOBSTER_ADMIN_CHAT_ID" in result

    # --- Deliberate italic still works ---

    def test_italic_standalone(self):
        # Surrounded by spaces: _italic_ should still render as italic
        assert md_to_html("_italic_") == "<i>italic</i>"

    def test_italic_in_sentence(self):
        result = md_to_html("This is _very_ important.")
        assert "<i>very</i>" in result

    def test_italic_start_of_string(self):
        # Start of string counts as a non-word boundary
        result = md_to_html("_note_ this")
        assert "<i>note</i>" in result

    def test_italic_end_of_string(self):
        result = md_to_html("remember _this_")
        assert "<i>this</i>" in result

    def test_snake_case_in_inline_code_untouched(self):
        # Inside backticks, content is never processed by the italic regex
        result = md_to_html("`write_result`")
        assert result == "<code>write_result</code>"

    def test_snake_case_in_code_block_untouched(self):
        result = md_to_html("```\nwrite_result\n```")
        assert "<i>" not in result
        assert "write_result" in result


# ---------------------------------------------------------------------------
# Long URL handling (issue lobstertalk#20)
# ---------------------------------------------------------------------------

_SHORT_URL = "https://example.com/path"
_LONG_URL = "https://accounts.google.com/o/oauth2/auth?" + "x" * 180  # > 200 chars


class TestLongUrls:
    """URLs longer than _LONG_URL_THRESHOLD must be rendered as plain text
    so that users can long-press and copy them on Telegram mobile."""

    def test_short_url_rendered_as_hyperlink(self):
        result = md_to_html(f"[click]({_SHORT_URL})")
        assert f'<a href="{_SHORT_URL}">click</a>' in result

    def test_long_url_not_rendered_as_hyperlink(self):
        result = md_to_html(f"[Authorize]({_LONG_URL})")
        assert "<a href=" not in result

    def test_long_url_link_text_present_in_bold(self):
        result = md_to_html(f"[Authorize]({_LONG_URL})")
        assert "<b>Authorize</b>" in result

    def test_long_url_raw_url_present_in_pre(self):
        result = md_to_html(f"[Authorize]({_LONG_URL})")
        assert "<pre>" in result
        assert _LONG_URL in result

    def test_long_url_threshold_boundary_exactly_200(self):
        """A URL exactly 200 chars long is NOT long (threshold is > 200)."""
        url_200 = "https://example.com/" + "a" * 180  # total = 200 chars
        result = md_to_html(f"[click]({url_200})")
        assert f'<a href="{url_200}">click</a>' in result

    def test_long_url_threshold_boundary_exactly_201(self):
        """A URL exactly 201 chars long IS long — rendered as plain text."""
        url_201 = "https://example.com/" + "a" * 181  # total = 201 chars
        result = md_to_html(f"[click]({url_201})")
        assert "<a href=" not in result
        assert "<pre>" in result

    def test_long_url_mixed_with_short_url(self):
        """Short and long links can coexist in the same message."""
        text = f"See [docs]({_SHORT_URL}) and then [auth]({_LONG_URL})."
        result = md_to_html(text)
        assert f'<a href="{_SHORT_URL}">docs</a>' in result
        assert "<a href=" not in result.replace(f'<a href="{_SHORT_URL}">docs</a>', "")
        assert "<pre>" in result
        assert _LONG_URL in result

    def test_long_url_ampersand_params_escaped_once(self):
        """Query string & is escaped exactly once — not double-escaped."""
        url_with_amp = "https://example.com/auth?client_id=abc&redirect_uri=https%3A%2F%2F" + "x" * 150
        result = md_to_html(f"[Auth]({url_with_amp})")
        # Should appear escaped as &amp; (once) inside the <pre>
        assert "&amp;" in result
        # Must not be double-escaped to &amp;amp;
        assert "&amp;amp;" not in result

    def test_long_url_inside_code_block_not_affected(self):
        """Links inside code blocks are never processed (existing behaviour)."""
        result = md_to_html(f"```\n[Authorize]({_LONG_URL})\n```")
        assert "<pre><code>" in result
        assert "<b>Authorize</b>" not in result
