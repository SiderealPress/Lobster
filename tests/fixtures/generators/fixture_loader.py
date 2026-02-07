"""
Lobster Test Fixture Loader

Utilities for loading and managing test fixtures.
"""

import json
from pathlib import Path
from typing import Any, Optional


class FixtureLoader:
    """Load and manage test fixtures."""

    def __init__(self, fixtures_dir: Optional[Path] = None):
        """
        Initialize fixture loader.

        Args:
            fixtures_dir: Path to fixtures directory. Defaults to tests/fixtures.
        """
        if fixtures_dir is None:
            # Determine fixtures directory relative to this file
            fixtures_dir = Path(__file__).parent.parent
        self.fixtures_dir = fixtures_dir

    def load_json(self, relative_path: str) -> Any:
        """
        Load a JSON fixture file.

        Args:
            relative_path: Path relative to fixtures directory

        Returns:
            Parsed JSON content
        """
        full_path = self.fixtures_dir / relative_path
        with open(full_path, "r") as f:
            return json.load(f)

    def load_text_messages(self) -> list[dict]:
        """Load sample text messages."""
        return self.load_json("messages/text_messages.json")

    def load_voice_messages(self) -> list[dict]:
        """Load sample voice messages."""
        return self.load_json("messages/voice_messages.json")

    def load_edge_case_messages(self) -> list[dict]:
        """Load edge case messages."""
        return self.load_json("messages/edge_cases.json")

    def load_stress_messages(self) -> list[dict]:
        """Load stress test messages (1000+)."""
        return self.load_json("messages/stress_messages.json")

    def load_sample_tasks(self) -> dict:
        """Load sample tasks."""
        return self.load_json("tasks/sample_tasks.json")

    def load_scheduled_jobs(self) -> dict:
        """Load sample scheduled jobs."""
        return self.load_json("tasks/scheduled_jobs.json")

    def get_audio_file(self, duration: str = "5s") -> Path:
        """
        Get path to a test audio file.

        Args:
            duration: Duration string (5s, 30s, 2min)

        Returns:
            Path to audio file
        """
        audio_files = {
            "5s": "sample_voice_5s.ogg",
            "30s": "sample_voice_30s.ogg",
            "2min": "sample_voice_2min.ogg",
        }
        filename = audio_files.get(duration, "sample_voice_5s.ogg")
        return self.fixtures_dir / "audio" / filename

    def audio_file_exists(self, duration: str = "5s") -> bool:
        """Check if audio fixture exists."""
        return self.get_audio_file(duration).exists()


# Global loader instance for convenience
_default_loader: Optional[FixtureLoader] = None


def get_loader() -> FixtureLoader:
    """Get the default fixture loader."""
    global _default_loader
    if _default_loader is None:
        _default_loader = FixtureLoader()
    return _default_loader


def load_text_messages() -> list[dict]:
    """Convenience function to load text messages."""
    return get_loader().load_text_messages()


def load_voice_messages() -> list[dict]:
    """Convenience function to load voice messages."""
    return get_loader().load_voice_messages()


def load_edge_cases() -> list[dict]:
    """Convenience function to load edge cases."""
    return get_loader().load_edge_case_messages()


def load_stress_messages() -> list[dict]:
    """Convenience function to load stress messages."""
    return get_loader().load_stress_messages()
