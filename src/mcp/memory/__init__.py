"""
Lobster Three-Layer Memory System

Layer 1: Real-Time Event Store (VectorMemory - SQLite + sqlite-vec + FTS5)
Layer 2: Nightly Consolidation (cron job -> inbox message)
Layer 3: Canonical Memory (Static markdown files)

Usage:
    from memory import create_memory_provider
    memory = create_memory_provider()
    memory.store(event)
    results = memory.search("what was that project about?")

The factory returns VectorMemory if available, else falls back to StaticMemory.
Static files are always the source of truth; the vector DB is an acceleration layer.
"""

import logging

from .provider import MemoryProvider, MemoryEvent
from .vector_memory import VectorMemory
from .static_memory import StaticMemory

log = logging.getLogger("lobster-memory")

__all__ = [
    "MemoryProvider",
    "MemoryEvent",
    "VectorMemory",
    "StaticMemory",
    "create_memory_provider",
]


def create_memory_provider(use_vector: bool = True) -> MemoryProvider:
    """Factory that returns VectorMemory if available, else StaticMemory.

    Args:
        use_vector: Whether to attempt VectorMemory initialization.
                    Set False to force StaticMemory.

    Returns:
        A MemoryProvider instance (VectorMemory or StaticMemory).
    """
    if use_vector:
        try:
            provider = VectorMemory()
            log.info("Memory provider: VectorMemory (SQLite + sqlite-vec + FTS5)")
            return provider
        except Exception as e:
            log.warning(f"VectorMemory unavailable ({e}), falling back to StaticMemory")
    provider = StaticMemory()
    log.info("Memory provider: StaticMemory (grep over canonical files)")
    return provider
