"""
Dry Run Support — Pipeline Hygiene

Provides DryRunContext: a context manager and flag carrier that gates all
side-effecting operations behind a single --dry-run check. Integrates with
AuditLog to log what would happen without actually writing.

Usage:
    from pipeline.dry_run import DryRunContext

    ctx = DryRunContext(enabled=True)

    with ctx:
        if ctx.would_write("Jane Smith"):
            # This block is skipped in dry-run mode
            create_entity(...)
        else:
            # Dry-run path: log what would have happened
            ctx.log_skipped_write("Jane Smith", "createEntity")

    print(ctx.summary())
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Any


@dataclass
class SkippedWrite:
    """Record of a write that was skipped in dry-run mode."""
    entity_name: str
    operation: str
    details: dict[str, Any] = field(default_factory=dict)


class DryRunContext:
    """
    Gate side-effecting writes behind a dry-run flag.

    In dry-run mode:
    - would_write() returns False and records what would have been written
    - All skipped writes are logged to stderr and recorded internally

    In live mode:
    - would_write() returns True (write proceeds normally)

    Attributes:
        enabled: True if dry-run mode is active.
        skipped_writes: List of SkippedWrite records accumulated in dry-run mode.
    """

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled
        self.skipped_writes: list[SkippedWrite] = []

    def would_write(self, entity_name: str = "", operation: str = "") -> bool:
        """
        Gate a write operation.

        Returns True if the write should proceed (live mode).
        Returns False if in dry-run mode (records the skipped write).

        Args:
            entity_name: Human-readable name of the entity being written.
            operation: Name of the operation (e.g. "createEntity", "createEdge").

        Returns:
            True if caller should proceed with the write, False if it should skip.
        """
        if self.enabled:
            self._record_skip(entity_name, operation)
            return False
        return True

    def log_skipped_write(
        self,
        entity_name: str,
        operation: str,
        **details: Any,
    ) -> None:
        """
        Explicitly log a skipped write (call after would_write returns False).

        Args:
            entity_name: Human-readable name of the entity.
            operation: Operation name.
            **details: Extra details to include in the log record.
        """
        self._record_skip(entity_name, operation, **details)

    def summary(self) -> str:
        """Return a human-readable summary of all skipped writes."""
        if not self.enabled:
            return "Dry-run disabled — all writes executed."
        if not self.skipped_writes:
            return "Dry-run: no writes were attempted."
        lines = [f"Dry-run: {len(self.skipped_writes)} write(s) skipped:"]
        for sw in self.skipped_writes:
            lines.append(f"  • [{sw.operation}] {sw.entity_name}")
        return "\n".join(lines)

    def __enter__(self) -> "DryRunContext":
        return self

    def __exit__(self, *_: Any) -> None:
        if self.enabled:
            print(self.summary(), file=sys.stderr)

    def _record_skip(self, entity_name: str, operation: str, **details: Any) -> None:
        skip = SkippedWrite(entity_name=entity_name, operation=operation, details=details)
        self.skipped_writes.append(skip)
        print(
            f"[dry-run] SKIP {operation}: {entity_name}"
            + (f" | {details}" if details else ""),
            file=sys.stderr,
        )


def add_dry_run_arg(parser: Any) -> None:
    """
    Add --dry-run argument to an argparse.ArgumentParser.

    Usage:
        import argparse
        from pipeline.dry_run import add_dry_run_arg, DryRunContext

        parser = argparse.ArgumentParser()
        add_dry_run_arg(parser)
        args = parser.parse_args()
        ctx = DryRunContext(enabled=args.dry_run)
    """
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help=(
            "Simulate enrichment without writing to Kissinger. "
            "Logs what would happen. Audit log still written."
        ),
    )
