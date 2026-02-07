"""Lobster test mocks."""

from .mock_telegram import MockTelegramServer, MockTelegramBot
from .mock_claude_cli import MockClaudeCLI, install_mock_claude
from .mock_whisper import MockWhisperModel

__all__ = [
    "MockTelegramServer",
    "MockTelegramBot",
    "MockClaudeCLI",
    "install_mock_claude",
    "MockWhisperModel",
]
