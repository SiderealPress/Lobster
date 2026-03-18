"""
Tests for MCP Server Audio Transcription

Tests transcribe_audio tool with various scenarios.
"""

import json
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock
import asyncio


class TestTranscribeAudio:
    """Tests for transcribe_audio tool."""

    @pytest.fixture
    def setup_voice_message(
        self, temp_messages_dir: Path, message_generator
    ):
        """Create a voice message with audio file."""
        inbox = temp_messages_dir / "inbox"
        audio_dir = temp_messages_dir / "audio"

        # Create voice message
        msg = message_generator.generate_voice_message(duration=10)
        msg_id = msg["id"]

        # Create a fake audio file
        audio_path = audio_dir / f"{msg_id}.ogg"
        audio_path.write_bytes(b"\x00" * 1000)  # Fake OGG data
        msg["audio_file"] = str(audio_path)

        (inbox / f"{msg_id}.json").write_text(json.dumps(msg))

        return inbox, audio_dir, msg_id, audio_path

    def test_transcribes_voice_message(self, setup_voice_message, temp_messages_dir):
        """Test successful voice message transcription via whisper.cpp."""
        inbox, audio_dir, msg_id, audio_path = setup_voice_message
        processed = temp_messages_dir / "processed"

        # Transcription now goes through run_whisper_cpp (returns (success, text) tuple).
        # The old get_whisper_model / model.transcribe() API was replaced with the
        # whisper.cpp CLI integration.
        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            PROCESSED_DIR=processed,
            AUDIO_DIR=audio_dir,
        ):
            with patch(
                "src.mcp.inbox_server.run_whisper_cpp",
                new_callable=AsyncMock,
                return_value=(True, "Hello, this is a test"),
            ):
                with patch(
                    "src.mcp.inbox_server.convert_ogg_to_wav",
                    new_callable=AsyncMock,
                    return_value=True,
                ):
                    from src.mcp.inbox_server import handle_transcribe_audio

                    result = asyncio.run(
                        handle_transcribe_audio({"message_id": msg_id})
                    )

                    assert "Transcription complete" in result[0].text
                    assert "Hello, this is a test" in result[0].text

                    # Verify message was updated with transcription
                    msg_file = inbox / f"{msg_id}.json"
                    content = json.loads(msg_file.read_text())
                    assert content["transcription"] == "Hello, this is a test"

    def test_not_voice_message_returns_error(
        self, temp_messages_dir, message_generator
    ):
        """Test that non-voice message returns error."""
        inbox = temp_messages_dir / "inbox"

        # Create text message (not voice)
        msg = message_generator.generate_text_message()
        msg_id = msg["id"]
        (inbox / f"{msg_id}.json").write_text(json.dumps(msg))

        with patch("src.mcp.inbox_server.INBOX_DIR", inbox):
            from src.mcp.inbox_server import handle_transcribe_audio

            result = asyncio.run(handle_transcribe_audio({"message_id": msg_id}))

            assert "Error" in result[0].text
            assert "not a voice message" in result[0].text

    def test_already_transcribed_returns_existing(
        self, setup_voice_message, temp_messages_dir
    ):
        """Test that already transcribed message returns existing transcription."""
        inbox, audio_dir, msg_id, audio_path = setup_voice_message
        processed = temp_messages_dir / "processed"

        # Update message with existing transcription
        msg_file = inbox / f"{msg_id}.json"
        msg = json.loads(msg_file.read_text())
        msg["transcription"] = "Existing transcription"
        msg_file.write_text(json.dumps(msg))

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            PROCESSED_DIR=processed,
        ):
            from src.mcp.inbox_server import handle_transcribe_audio

            result = asyncio.run(handle_transcribe_audio({"message_id": msg_id}))

            assert "Already transcribed" in result[0].text
            assert "Existing transcription" in result[0].text

    def test_message_not_found_returns_error(self, temp_messages_dir):
        """Test that missing message returns error."""
        inbox = temp_messages_dir / "inbox"
        processed = temp_messages_dir / "processed"

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            PROCESSED_DIR=processed,
        ):
            from src.mcp.inbox_server import handle_transcribe_audio

            result = asyncio.run(
                handle_transcribe_audio({"message_id": "nonexistent"})
            )

            assert "Error" in result[0].text
            assert "not found" in result[0].text.lower()

    def test_missing_audio_file_returns_error(
        self, temp_messages_dir, message_generator
    ):
        """Test that missing audio file returns error."""
        inbox = temp_messages_dir / "inbox"
        processed = temp_messages_dir / "processed"

        # Create voice message pointing to nonexistent audio
        msg = message_generator.generate_voice_message()
        msg["audio_file"] = "/nonexistent/audio.ogg"
        msg_id = msg["id"]
        (inbox / f"{msg_id}.json").write_text(json.dumps(msg))

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            PROCESSED_DIR=processed,
        ):
            from src.mcp.inbox_server import handle_transcribe_audio

            result = asyncio.run(handle_transcribe_audio({"message_id": msg_id}))

            assert "Error" in result[0].text
            assert "not found" in result[0].text.lower()

    def test_requires_message_id(self, temp_messages_dir):
        """Test that message_id is required."""
        inbox = temp_messages_dir / "inbox"

        with patch("src.mcp.inbox_server.INBOX_DIR", inbox):
            from src.mcp.inbox_server import handle_transcribe_audio

            result = asyncio.run(handle_transcribe_audio({}))

            assert "Error" in result[0].text
            assert "required" in result[0].text.lower()

    def test_handles_transcription_error(self, setup_voice_message, temp_messages_dir):
        """Test that transcription errors are handled gracefully."""
        inbox, audio_dir, msg_id, audio_path = setup_voice_message
        processed = temp_messages_dir / "processed"

        # Simulate run_whisper_cpp raising an unexpected exception (e.g. subprocess crash).
        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            PROCESSED_DIR=processed,
            AUDIO_DIR=audio_dir,
        ):
            with patch(
                "src.mcp.inbox_server.run_whisper_cpp",
                new_callable=AsyncMock,
                side_effect=Exception("whisper.cpp process crashed"),
            ):
                with patch(
                    "src.mcp.inbox_server.convert_ogg_to_wav",
                    new_callable=AsyncMock,
                    return_value=True,
                ):
                    from src.mcp.inbox_server import handle_transcribe_audio

                    result = asyncio.run(
                        handle_transcribe_audio({"message_id": msg_id})
                    )

                    assert "Error" in result[0].text

    def test_finds_message_in_processed_dir(
        self, temp_messages_dir, message_generator
    ):
        """Test that messages can be found in processed directory."""
        inbox = temp_messages_dir / "inbox"
        processed = temp_messages_dir / "processed"
        audio_dir = temp_messages_dir / "audio"

        # Create voice message in processed dir
        msg = message_generator.generate_voice_message()
        msg_id = msg["id"]

        audio_path = audio_dir / f"{msg_id}.ogg"
        audio_path.write_bytes(b"\x00" * 1000)
        msg["audio_file"] = str(audio_path)
        msg["transcription"] = "Already done"

        (processed / f"{msg_id}.json").write_text(json.dumps(msg))

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            PROCESSED_DIR=processed,
        ):
            from src.mcp.inbox_server import handle_transcribe_audio

            result = asyncio.run(handle_transcribe_audio({"message_id": msg_id}))

            # Should find it and return existing transcription
            assert "Already transcribed" in result[0].text


class TestOggConversion:
    """Tests for OGG to WAV conversion."""

    def test_convert_ogg_to_wav_success(self, temp_dir):
        """Test successful OGG to WAV conversion."""
        ogg_path = temp_dir / "test.ogg"
        wav_path = temp_dir / "test.wav"

        # Create fake OGG file
        ogg_path.write_bytes(b"\x00" * 100)

        # Mock successful ffmpeg execution
        with patch("asyncio.create_subprocess_exec") as mock_exec:
            mock_proc = MagicMock()
            mock_proc.communicate = AsyncMock(return_value=(b"", b""))
            mock_proc.returncode = 0
            mock_exec.return_value = mock_proc

            from src.mcp.inbox_server import convert_ogg_to_wav

            result = asyncio.run(convert_ogg_to_wav(ogg_path, wav_path))

            assert result is True
            mock_exec.assert_called_once()

    def test_convert_ogg_to_wav_failure(self, temp_dir):
        """Test failed OGG to WAV conversion."""
        ogg_path = temp_dir / "test.ogg"
        wav_path = temp_dir / "test.wav"

        ogg_path.write_bytes(b"\x00" * 100)

        # Mock failed ffmpeg execution
        with patch("asyncio.create_subprocess_exec") as mock_exec:
            mock_proc = MagicMock()
            mock_proc.communicate = AsyncMock(return_value=(b"", b"Error"))
            mock_proc.returncode = 1
            mock_exec.return_value = mock_proc

            from src.mcp.inbox_server import convert_ogg_to_wav

            result = asyncio.run(convert_ogg_to_wav(ogg_path, wav_path))

            assert result is False
