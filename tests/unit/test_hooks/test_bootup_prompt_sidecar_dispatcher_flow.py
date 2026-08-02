"""
Regression test: sys.dispatcher.bootup.md reads the reflection prompt from the
bootup-prompt.md sidecar file at startup, not from an inbox message (#1998).

Before this fix, on-compact.py / on-fresh-start.py wrote a reflection_prompt
message to the inbox in debug mode, and the dispatcher had to mark_processing +
mark_processed it like any other message -- 2 extra MCP round-trips on the
startup critical path, just to read a one-shot prompt once and discard it.

The fix adds a startup step (2e) that reads and deletes a plain sidecar file
(~/messages/bootup-prompt.md) directly -- one Read call, zero claim/process
overhead -- and marks the old inbox-message handler as legacy.

Like test_dispatcher_bootup_single_pass_read.py, this is a characterization
test over the agent instruction file (markdown, not Python) -- the behavior
lives in LLM-followed instructions, so the test asserts on the structural
presence/absence of the relevant instructions.
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).parents[3]
_DISPATCHER_BOOTUP = _REPO_ROOT / ".claude" / "sys.dispatcher.bootup.md"


def test_bootup_file_exists() -> None:
    assert _DISPATCHER_BOOTUP.exists(), f"sys.dispatcher.bootup.md not found at {_DISPATCHER_BOOTUP}"


def test_startup_reads_bootup_prompt_sidecar() -> None:
    """Step 2e must instruct the dispatcher to read (and delete) the sidecar
    file directly, instead of relying on an inbox message + claim/process.
    """
    content = _DISPATCHER_BOOTUP.read_text()
    assert "bootup-prompt.md" in content, (
        "sys.dispatcher.bootup.md is missing the bootup-prompt.md sidecar file "
        "reference (issue #1998) -- the dispatcher must read the debug reflection "
        "prompt directly from this file at startup."
    )
    assert "2e" in content, (
        "sys.dispatcher.bootup.md should add the sidecar-read as an explicit "
        "startup step (2e) alongside the other lettered startup sub-steps."
    )


def test_reflection_prompt_handler_marked_legacy() -> None:
    """The old inbox-message reflection_prompt handler must be marked legacy
    and must not instruct the dispatcher to reflect on it -- reflection now
    happens once, at step 2e, from the sidecar file.
    """
    content = _DISPATCHER_BOOTUP.read_text()
    assert "legacy" in content.lower(), (
        "sys.dispatcher.bootup.md's reflection_prompt message handler must be "
        "marked as legacy/superseded now that step 2e (sidecar file) is the "
        "canonical path (issue #1998)."
    )


def test_legacy_handler_does_not_reflect_on_stale_messages() -> None:
    """The legacy reflection_prompt handler must tell the dispatcher to drop a
    stale/queued message silently (mark_processing + mark_processed, no
    reflection) -- not to reflect on it. Reflection now happens exactly once,
    at step 2e, from the sidecar file; re-reflecting on a stale dequeued copy
    risks duplicate or contradictory GitHub activity.
    """
    content = _DISPATCHER_BOOTUP.read_text()
    assert "Reflect genuinely: were there friction points" not in content, (
        "sys.dispatcher.bootup.md's legacy reflection_prompt handler still contains "
        "the old 'Reflect genuinely...' instruction. It should instead say to drop "
        "the message without reflecting -- see step 2e for the one canonical "
        "reflection path (issue #1998)."
    )
    assert "without reflecting" in content or "do not act on it" in content, (
        "sys.dispatcher.bootup.md's legacy reflection_prompt handler must explicitly "
        "instruct the dispatcher not to reflect on a stale/queued message."
    )
