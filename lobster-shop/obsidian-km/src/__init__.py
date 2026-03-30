# obsidian-km: Obsidian Knowledge Management skill for Lobster
#
# Pure Python vault operations using filesystem + frontmatter + ripgrep.
# See docs/cli-approach.md for design rationale.

from .vault_ops import (
    resolve_vault_path,
    sanitize_title,
    create_note,
    read_note,
    search_notes,
    append_to_note,
    list_notes,
)

__all__ = [
    "resolve_vault_path",
    "sanitize_title",
    "create_note",
    "read_note",
    "search_notes",
    "append_to_note",
    "list_notes",
]
