"""
Tests for issue #2269 — conversation history must surface Telegram reply_to
threading so a short affirmative reply cannot be mis-paired with an unrelated
prior question.

Spec (from the issue):
  - A short affirmative reply is confirmation ONLY of the message it is a
    Telegram reply_to of.
  - When the reply_to metadata is present, the history output must show which
    message_id was replied to and quote its text, so the reader can verify the
    pairing instead of inferring it from adjacency.
  - When the reply_to metadata is absent on a short affirmative inbound
    message, the history output must say so explicitly, so the reader does not
    silently pair it with whatever happens to be adjacent.

These tests exercise pure formatting/classification helpers — no DB, no
filesystem, no network.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_SRC_DIR = Path(__file__).parent.parent.parent.parent / "src"
_MCP_DIR = _SRC_DIR / "mcp"
for _d in (_SRC_DIR, _MCP_DIR):
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))

import src.mcp.inbox_server  # noqa: F401

from src.mcp.inbox_server import (
    _CONFIRMATION_MAX_WORDS,
    _REPLY_QUOTE_DISPLAY_LIMIT,
    _UNTHREADED_CONFIRMATION_WARNING,
    _coerce_reply_to,
    _format_history_output,
    _format_reply_to_block,
    _is_short_affirmative,
)


# ---------------------------------------------------------------------------
# Fixtures modelled on the real incident message shape
# ---------------------------------------------------------------------------

def _received(text: str, **overrides) -> dict:
    msg = {
        "_direction": "received",
        "source": "telegram",
        "chat_id": 8305714125,
        "timestamp": "2026-09-18T11:36:31+00:00",
        "user_name": "TestUser",
        "text": text,
    }
    msg.update(overrides)
    return msg


def _sent(text: str, **overrides) -> dict:
    msg = {
        "_direction": "sent",
        "source": "telegram",
        "chat_id": 8305714125,
        "timestamp": "2026-09-18T11:30:00+00:00",
        "text": text,
    }
    msg.update(overrides)
    return msg


# The two competing questions from the incident: the one actually replied to,
# and the unrelated consequential one that adjacency wrongly paired with "Sure".
CATEGORY_QUESTION = "PSP Classifieds Monitor hashtag breakdown — want me to dig into a specific category?"
DEPLOY_QUESTION = "Want me to add a #ForFree/#FF unconditional-alert bypass rule and deploy it?"


# ---------------------------------------------------------------------------
# _is_short_affirmative
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text",
    ["Sure", "sure", "yes", "Yep!", "ok", "do it", "go ahead", "lgtm", "👍"],
)
def test_short_affirmative_recognised(text):
    assert _is_short_affirmative(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "",
        "no",
        "nope",
        "don't do it",
        "What! I don't want you to serve up every for free item!",
        "Sure, but only for the category breakdown and definitely not the deploy",
    ],
)
def test_non_confirmations_not_flagged(text):
    assert _is_short_affirmative(text) is False


def test_confirmation_word_budget_is_the_documented_constant():
    """A phrase longer than the documented budget is not a bare confirmation."""
    long_phrase = " ".join(["yes"] * (_CONFIRMATION_MAX_WORDS + 1))
    assert _is_short_affirmative(long_phrase) is False
    at_budget = " ".join(["yes"] * _CONFIRMATION_MAX_WORDS)
    assert _is_short_affirmative(at_budget) is True


# ---------------------------------------------------------------------------
# _coerce_reply_to — the DB path hands back a JSON string, the filesystem
# path hands back a dict. Both must resolve to the same dict.
# ---------------------------------------------------------------------------

def test_reply_to_accepts_dict_from_filesystem_path():
    payload = {"text": CATEGORY_QUESTION, "message_id": 32601}
    assert _coerce_reply_to(payload) == payload


def test_reply_to_accepts_json_string_from_db_path():
    payload = {"text": CATEGORY_QUESTION, "message_id": 32601}
    assert _coerce_reply_to(json.dumps(payload)) == payload


@pytest.mark.parametrize("value", [None, "", "not json", 42, [1, 2]])
def test_reply_to_returns_none_for_unusable_values(value):
    assert _coerce_reply_to(value) is None


# ---------------------------------------------------------------------------
# _format_reply_to_block
# ---------------------------------------------------------------------------

def test_threaded_reply_names_the_message_it_confirms():
    msg = _received(
        "Sure",
        telegram_message_id=32605,
        reply_to={
            "text": CATEGORY_QUESTION,
            "message_id": 32601,
            "username": "lobstertown_bot",
        },
    )
    block = _format_reply_to_block(msg)

    assert "32601" in block, "must name the message_id being replied to"
    assert "dig into a specific category" in block, "must quote the replied-to text"
    assert _UNTHREADED_CONFIRMATION_WARNING not in block


def test_threaded_reply_quote_is_truncated_at_the_documented_limit():
    long_question = "x" * (_REPLY_QUOTE_DISPLAY_LIMIT + 500)
    msg = _received("Sure", reply_to={"text": long_question, "message_id": 1})
    block = _format_reply_to_block(msg)

    assert "[truncated]" in block
    assert len(block) < _REPLY_QUOTE_DISPLAY_LIMIT + 400


def test_unthreaded_short_affirmative_is_marked_as_unverified():
    """The incident case: 'Sure' with no reply_to must not read as confirmation."""
    msg = _received("Sure", telegram_message_id=32605)
    block = _format_reply_to_block(msg)

    assert _UNTHREADED_CONFIRMATION_WARNING in block


def test_unthreaded_substantive_message_gets_no_warning():
    msg = _received("Please regenerate the weekly digest with the new template.")
    assert _format_reply_to_block(msg) == ""


def test_outbound_message_records_the_message_it_threaded_to():
    msg = _sent("Reading it now — that's a serious one.", reply_to_message_id=32630)
    block = _format_reply_to_block(msg)

    assert "32630" in block
    assert _UNTHREADED_CONFIRMATION_WARNING not in block


def test_inbound_reply_to_message_id_column_alone_counts_as_threaded():
    """The DB stores reply_to_message_id as its own column; a row that has the
    id but no reply_to blob is still a genuine threaded reply, not an
    unverified one."""
    msg = _received("Sure", reply_to_message_id=32601)
    block = _format_reply_to_block(msg)

    assert "32601" in block
    assert _UNTHREADED_CONFIRMATION_WARNING not in block


# ---------------------------------------------------------------------------
# _format_history_output — end-to-end rendering
# ---------------------------------------------------------------------------

def test_history_lets_reader_pair_confirmation_with_the_right_question():
    """Reconstruction of the incident: the bot asked two questions; the user's
    'Sure' threaded to the harmless one. History must make that visible."""
    messages = [
        _received(
            "Sure",
            telegram_message_id=32605,
            reply_to={
                "text": CATEGORY_QUESTION,
                "message_id": 32601,
                "username": "lobstertown_bot",
            },
        ),
        _sent(DEPLOY_QUESTION, telegram_message_id=32603),
        _sent(CATEGORY_QUESTION, telegram_message_id=32601),
    ]
    output = _format_history_output(messages, total_count=3, offset=0, limit=20)

    assert "32601" in output
    assert "dig into a specific category" in output
    # The deploy question is present in history but is NOT what was replied to.
    assert "#ForFree" in output


def test_history_flags_the_ambiguous_case_the_incident_actually_hit():
    messages = [
        _received("Sure", telegram_message_id=32605),
        _sent(DEPLOY_QUESTION, telegram_message_id=32603),
    ]
    output = _format_history_output(messages, total_count=2, offset=0, limit=20)

    assert _UNTHREADED_CONFIRMATION_WARNING in output


def test_history_output_unchanged_for_ordinary_untargeted_messages():
    """No regression: plain conversation renders without threading noise."""
    messages = [
        _received("Can you summarise yesterday's PRs?"),
        _sent("Here are the three that merged."),
    ]
    output = _format_history_output(messages, total_count=2, offset=0, limit=20)

    assert "↩️" not in output
    assert _UNTHREADED_CONFIRMATION_WARNING not in output
    assert "summarise yesterday's PRs" in output


def test_history_tolerates_malformed_reply_to_without_raising():
    messages = [_received("Sure", reply_to="{not valid json")]
    output = _format_history_output(messages, total_count=1, offset=0, limit=20)

    # Falls back to the unverified warning rather than crashing or pretending
    # the message was threaded.
    assert _UNTHREADED_CONFIRMATION_WARNING in output
