"""
Regression test: compaction recovery notification is sent deterministically by
the compact-catchup agent itself, not manually by the dispatcher (issue #1983).

Before this fix, the "🔄 Back online" notification was sent by the dispatcher
after it received compact-catchup's write_result, gated on LOBSTER_DEBUG. This
made the signal contingent on dispatcher behavior: if the dispatcher skipped
the step, crashed before reaching it, or mishandled the result, the
notification silently never fired -- while looking, when it did fire, like
confirmation that recovery succeeded. Issue #1983 calls this "epistemic rot":
a signal that fires "most of the time" is worse than no signal, because its
presence cannot be trusted and its absence is ambiguous.

The fix moves the notification into compact-catchup.md as a new Phase 5,
sent directly by the agent before write_result -- so it fires (or doesn't)
based on the agent's own completion, not on downstream dispatcher handling.

These are characterization tests over the agent instruction files (markdown,
not Python) -- the behavior lives in LLM-followed instructions, so the test
asserts on the structural presence/absence of the relevant instructions
rather than executing code. This mirrors the existing regression-guard
pattern in test_dispatcher_bootup_single_pass_read.py.
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).parents[3]
_COMPACT_CATCHUP = _REPO_ROOT / ".claude" / "agents" / "compact-catchup.md"
_DISPATCHER_BOOTUP = _REPO_ROOT / ".claude" / "sys.dispatcher.bootup.md"


def test_compact_catchup_file_exists() -> None:
    assert _COMPACT_CATCHUP.exists(), f"compact-catchup.md not found at {_COMPACT_CATCHUP}"


def test_dispatcher_bootup_file_exists() -> None:
    assert _DISPATCHER_BOOTUP.exists(), f"sys.dispatcher.bootup.md not found at {_DISPATCHER_BOOTUP}"


def test_compact_catchup_sends_notification_itself() -> None:
    """compact-catchup.md must instruct the agent to send the recovery
    notification itself (Phase 5), gated on LOBSTER_DEBUG, before write_result.
    """
    content = _COMPACT_CATCHUP.read_text()
    assert "Phase 5" in content, (
        "compact-catchup.md is missing the 'Phase 5' debug recovery notification "
        "phase (issue #1983) -- the agent must send the 'Back online' notification "
        "itself so it fires deterministically."
    )
    assert "LOBSTER_DEBUG" in content, (
        "compact-catchup.md's Phase 5 must gate the notification on LOBSTER_DEBUG, "
        "matching the existing debug-only behavior."
    )
    assert "Back online" in content, (
        "compact-catchup.md must contain the 'Back online' recovery notification text "
        "as part of Phase 5."
    )
    assert "proactive=True" in content, (
        "compact-catchup.md's Phase 5 send_reply must pass proactive=True -- this "
        "notification has no originating user message to thread against "
        "(hooks/require-reply-to-message-id.py)."
    )


def test_compact_catchup_rules_document_send_reply_exception() -> None:
    """The 'Do NOT call send_reply' rule must document the Phase 5 exception,
    otherwise the blanket rule contradicts the new Phase 5 instruction.
    """
    content = _COMPACT_CATCHUP.read_text()
    assert "except" in content and "Phase 5" in content, (
        "compact-catchup.md's Rules section must carve out an explicit exception "
        "for the Phase 5 debug notification send_reply call."
    )


def test_dispatcher_no_longer_sends_notification_manually() -> None:
    """sys.dispatcher.bootup.md must NOT instruct the dispatcher to manually
    send the 'Back online' notification -- that responsibility now lives
    entirely inside the compact-catchup agent (Phase 5).
    """
    content = _DISPATCHER_BOOTUP.read_text()
    assert "send a brief status to ADMIN_CHAT_ID" not in content, (
        "sys.dispatcher.bootup.md still instructs the dispatcher to manually send "
        "the recovery notification. Issue #1983's fix moves this into "
        "compact-catchup.md Phase 5 so it fires deterministically regardless of "
        "dispatcher behavior -- remove the manual instruction."
    )
    assert "Back online. Context recovered from [window_start]" not in content, (
        "sys.dispatcher.bootup.md still contains the manual 'Back online' "
        "notification template. This must be removed -- the compact-catchup agent "
        "sends it directly now (issue #1983)."
    )


def test_dispatcher_bootup_references_agent_owned_notification() -> None:
    """sys.dispatcher.bootup.md's compact_catchup-result handler must note that
    the notification is agent-owned, so a reader doesn't wonder where it went.
    """
    content = _DISPATCHER_BOOTUP.read_text()
    assert "#1983" in content, (
        "sys.dispatcher.bootup.md should reference issue #1983 near the "
        "compact_catchup result handler to explain why no manual send is needed."
    )
