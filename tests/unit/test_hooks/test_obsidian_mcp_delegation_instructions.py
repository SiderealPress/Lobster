"""
Regression test: mcp__obsidian__* tool calls must be explicitly named as a
background-delegation requirement in the dispatcher bootup instructions
(issue #2119).

Root cause recap: mcp__obsidian__* calls are backed by a stdio MCP server
(`npx -y obsidian-mcp <vault-path>`) with no application-level timeout. A
hung call (confirmed hangs of 2304s and 6462s in production) that runs
inline on the main dispatcher thread freezes the *entire* dispatcher loop
until the ~2h04m session-age SIGTERM kills the whole session — which is
exactly what happened when Wallace told a user "saving it to the vault now"
and then went silent for hours, because create-note/edit-note was never
reached and the promise died with the killed session.

This is a behavioral/instructional fix, not a runtime code fix — the
dispatcher is an LLM reading these bootup instructions at session start.
The "automated test that would fail if reverted" for this kind of change is
a content assertion against the instruction file: it fails if the required
guidance is missing, and passes once it is present. This does not prove the
dispatcher will always obey the instruction (that would require behavioral
observation over live traffic), but it does prove the instruction the
dispatcher is supposed to obey is actually present and specific.
"""

from __future__ import annotations

from pathlib import Path

# Path to the actual dispatcher bootup file (relative to the repo root).
_REPO_ROOT = Path(__file__).parents[3]
_DISPATCHER_BOOTUP = _REPO_ROOT / ".claude" / "sys.dispatcher.bootup.md"


def _bootup_content() -> str:
    assert _DISPATCHER_BOOTUP.exists(), (
        f"sys.dispatcher.bootup.md not found at {_DISPATCHER_BOOTUP}"
    )
    return _DISPATCHER_BOOTUP.read_text()


def test_obsidian_tools_named_under_background_delegation_rule() -> None:
    """mcp__obsidian__* must be an explicit named example of the
    'ANY file read/write' background-delegation rule, not left implicit."""
    content = _bootup_content()
    assert "ANY file read/write" in content, (
        "The 'ANY file read/write' background-delegation rule is missing "
        "from sys.dispatcher.bootup.md — cannot anchor the obsidian carve-out."
    )
    assert "mcp__obsidian__" in content, (
        "sys.dispatcher.bootup.md does not name mcp__obsidian__* tools "
        "anywhere. Issue #2119: obsidian MCP calls hang indefinitely "
        "(stdio server, no app-level timeout) and must be an explicit "
        "named example of the background-delegation rule, not left for "
        "the dispatcher to infer from the generic 'file read/write' bullet."
    )


def test_obsidian_violation_example_present() -> None:
    """The 'Violations that have occurred' list must include a concrete
    mcp__obsidian__ example, mirroring the existing violation examples for
    Read/Bash/mcp__github__ calls made inline on the dispatcher thread."""
    content = _bootup_content()
    assert "VIOLATION" in content, "Violations section missing entirely."
    # The violation example must actually reference an obsidian tool call.
    violation_section = content[content.index("Violations that have occurred") :]
    assert "mcp__obsidian__" in violation_section, (
        "The 'Violations that have occurred' section does not include an "
        "mcp__obsidian__ example (issue #2119's incident: "
        "mcp__obsidian__list-available-vaults called inline, hung 2304s, "
        "froze the whole dispatcher loop)."
    )


def test_obsidian_write_requires_readback_before_success_claim() -> None:
    """Item (d): a background subagent delegated an Obsidian write must
    verify the write landed (read-back / search-vault) before calling
    write_result with a success message. Success must never be claimed
    before the write is confirmed — this is what silently produced
    "said it saved something, went silent" in the original incident,
    because create-note/edit-note was never even reached."""
    content = _bootup_content()
    assert "search-vault" in content or "read-note" in content, (
        "No read-back verification step (search-vault / read-note) is "
        "documented for Obsidian writes."
    )
    assert "before" in content.lower() and (
        "report success" in content.lower()
        or "claiming success" in content.lower()
        or "before this read-back confirms" in content.lower()
        or "before you report success" in content.lower()
        or "never report success before" in content.lower()
    ), (
        "The instructions do not clearly state that read-back verification "
        "must happen BEFORE reporting success on an Obsidian write."
    )


def test_obsidian_delegation_mirrors_link_capture_pattern() -> None:
    """The obsidian delegation instructions should follow the same
    acknowledge -> Task(background) -> write_result shape as the existing
    link-capture pattern (lobster-shop/obsidian-km/context/obsidian-km.md),
    so the dispatcher applies a pattern it has already seen elsewhere in
    the codebase rather than inventing new behavior."""
    content = _bootup_content()
    obsidian_section_start = content.find("mcp__obsidian__")
    assert obsidian_section_start != -1, "mcp__obsidian__ not mentioned at all."
    # Look at the guidance following the first mention for the three-step shape.
    tail = content[obsidian_section_start:]
    assert "Task(" in tail, "No Task(...) delegation call shown near the obsidian guidance."
    assert "run_in_background" in tail or "background: true" in tail, (
        "The obsidian delegation guidance does not show background=true framing."
    )
    assert "write_result" in tail, (
        "The obsidian delegation guidance does not mention write_result for "
        "reporting back, unlike the rest of the delegation patterns in this file."
    )


def test_link_capture_pattern_still_exists_for_reference() -> None:
    """Sanity check that the reference pattern this fix mirrors still exists
    at the location the issue points to. If this moves, the mirrored
    guidance above may drift out of sync with the canonical example."""
    link_capture_doc = (
        _REPO_ROOT / "lobster-shop" / "obsidian-km" / "context" / "obsidian-km.md"
    )
    assert link_capture_doc.exists(), (
        f"Reference link-capture pattern doc not found at {link_capture_doc} "
        "— the obsidian delegation guidance in sys.dispatcher.bootup.md was "
        "written to mirror this pattern."
    )
    lc_content = link_capture_doc.read_text()
    assert "run_in_background=True" in lc_content
    assert "Task(" in lc_content
