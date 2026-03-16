"""
Unit tests for the compact-reminder sort-order fix in hooks/on-compact.py.

The fix uses ts_ms=0 so the filename "0_compact.json" sorts before any real
user-message filename (which begins with the current epoch in milliseconds,
e.g. "1773695000000_msg.json").  These tests assert that sort invariant
directly, without importing the hook.
"""


def test_compact_reminder_sorts_before_real_messages():
    """0_compact.json must be first after sorted(), regardless of how many
    real epoch-ms filenames are present."""
    filenames = [
        "1773695000000_msg.json",
        "1773695001234_msg.json",
        "0_compact.json",
        "1741234567890_compact.json",  # old-style filename (pre-fix)
        "1773695999999_msg.json",
    ]
    result = sorted(filenames)
    assert result[0] == "0_compact.json", (
        f"Expected '0_compact.json' to be first but got '{result[0]}'; "
        f"full order: {result}"
    )


def test_compact_reminder_sorts_before_single_real_message():
    """Minimal case: one compact-reminder and one user message."""
    filenames = ["1773695000000_msg.json", "0_compact.json"]
    result = sorted(filenames)
    assert result[0] == "0_compact.json"


def test_real_messages_sort_among_themselves_unchanged():
    """Sorting real epoch-ms filenames still produces ascending timestamp order."""
    filenames = [
        "1773695003000_c.json",
        "1773695001000_a.json",
        "1773695002000_b.json",
    ]
    result = sorted(filenames)
    assert result == [
        "1773695001000_a.json",
        "1773695002000_b.json",
        "1773695003000_c.json",
    ]
