"""
Protocol definitions for the User Model subsystem's external dependencies.

The user model never imports from core Lobster modules directly.
It only accepts injected dependencies that satisfy these Protocol definitions.
This ensures clean architectural boundaries and enables independent testing.
"""

import sqlite3
from typing import Protocol


class DatabaseProvider(Protocol):
    """Provides a SQLite connection to memory.db."""

    def get_connection(self) -> sqlite3.Connection:
        """Return a SQLite connection to the shared memory.db."""
        ...


class EmbeddingProvider(Protocol):
    """Provides text → vector embedding (all-MiniLM-L6-v2)."""

    def embed(self, text: str) -> list[float]:
        """Embed a single text string, returning a vector."""
        ...

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts, returning a list of vectors."""
        ...

    @property
    def is_warm(self) -> bool:
        """Return True if the model is loaded and ready."""
        ...


class ContextTemplateReader(Protocol):
    """Reads user's canonical context templates (values.md, goals.md, etc.)."""

    def read_template(self, name: str) -> str | None:
        """Read a canonical template by name. Returns None if not found."""
        ...

    def list_templates(self) -> list[str]:
        """List all available template names."""
        ...


class MessageBus(Protocol):
    """Optional: publish events for other subsystems to consume."""

    def publish(self, event_type: str, payload: dict) -> None:
        """Publish an event to the bus."""
        ...
