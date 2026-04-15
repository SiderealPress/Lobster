"""Tests for src/tts/piper.py

Tests are derived from the feature spec (issue #1575):
- piper binary + lessac-medium model as the TTS pipeline
- WAV synthesis → OGG/Opus conversion → TtsResult
- Fallback: TtsResult.ok=False when binary/model missing
- Failure propagation: synthesis error → TtsResult.ok=False
- Failure propagation: ffmpeg error → TtsResult.ok=False
- Cleanup: TtsResult.cleanup() deletes temp files

All tests mock subprocess.run and filesystem to avoid requiring piper/ffmpeg
to be installed during unit testing.
"""

import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch, call
import pytest

# ---------------------------------------------------------------------------
# Import the module under test
# ---------------------------------------------------------------------------
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent / "src"))

from tts.piper import (
    PiperConfig,
    TtsResult,
    DEFAULT_VOICE_MODEL_NAME,
    PIPER_SYNTHESIS_TIMEOUT_S,
    FFMPEG_CONVERT_TIMEOUT_S,
    find_piper_binary,
    find_voice_model,
    resolve_piper_config,
    synthesize_wav,
    convert_wav_to_ogg,
    text_to_voice_file,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fake_piper_config(tmp_path: Path) -> PiperConfig:
    """Create a PiperConfig pointing to real temp files for testing."""
    binary = tmp_path / "piper"
    binary.write_text("#!/bin/sh\necho fake piper\n")
    binary.chmod(0o755)
    model = tmp_path / f"{DEFAULT_VOICE_MODEL_NAME}.onnx"
    model.write_bytes(b"fake model data")
    return PiperConfig(piper_binary=binary, model_path=model)


# ---------------------------------------------------------------------------
# PiperConfig.is_usable
# ---------------------------------------------------------------------------

class TestPiperConfigIsUsable:
    def test_usable_when_binary_and_model_exist(self, tmp_path):
        cfg = _fake_piper_config(tmp_path)
        assert cfg.is_usable() is True

    def test_not_usable_when_binary_missing(self, tmp_path):
        model = tmp_path / f"{DEFAULT_VOICE_MODEL_NAME}.onnx"
        model.write_bytes(b"x")
        cfg = PiperConfig(
            piper_binary=tmp_path / "nonexistent_piper",
            model_path=model,
        )
        assert cfg.is_usable() is False

    def test_not_usable_when_model_missing(self, tmp_path):
        binary = tmp_path / "piper"
        binary.write_text("#!/bin/sh\n")
        binary.chmod(0o755)
        cfg = PiperConfig(
            piper_binary=binary,
            model_path=tmp_path / "nonexistent_model.onnx",
        )
        assert cfg.is_usable() is False

    def test_not_usable_when_binary_not_executable(self, tmp_path):
        binary = tmp_path / "piper"
        binary.write_text("#!/bin/sh\n")
        binary.chmod(0o644)  # not executable
        model = tmp_path / f"{DEFAULT_VOICE_MODEL_NAME}.onnx"
        model.write_bytes(b"x")
        cfg = PiperConfig(piper_binary=binary, model_path=model)
        assert cfg.is_usable() is False


# ---------------------------------------------------------------------------
# TtsResult.cleanup
# ---------------------------------------------------------------------------

class TestTtsResultCleanup:
    def test_cleanup_deletes_ogg_file(self, tmp_path):
        ogg = tmp_path / "voice.ogg"
        ogg.write_bytes(b"fake ogg data")
        result = TtsResult(ok=True, ogg_path=ogg, _cleanup_paths=[str(ogg)])

        assert ogg.exists()
        result.cleanup()
        assert not ogg.exists()

    def test_cleanup_is_idempotent(self, tmp_path):
        ogg = tmp_path / "voice.ogg"
        ogg.write_bytes(b"fake ogg data")
        result = TtsResult(ok=True, ogg_path=ogg, _cleanup_paths=[str(ogg)])

        result.cleanup()
        result.cleanup()  # second call should not raise

    def test_cleanup_clears_paths_list(self, tmp_path):
        ogg = tmp_path / "voice.ogg"
        ogg.write_bytes(b"x")
        result = TtsResult(ok=True, ogg_path=ogg, _cleanup_paths=[str(ogg)])
        result.cleanup()
        assert result._cleanup_paths == []


# ---------------------------------------------------------------------------
# find_piper_binary
# ---------------------------------------------------------------------------

class TestFindPiperBinary:
    def test_finds_binary_at_first_search_path(self, tmp_path):
        binary = tmp_path / "piper"
        binary.write_text("#!/bin/sh\n")
        binary.chmod(0o755)

        with patch("tts.piper.PIPER_BINARY_SEARCH_PATHS", [str(binary)]):
            result = find_piper_binary()
        assert result == binary

    def test_returns_none_when_not_found(self):
        with patch("tts.piper.PIPER_BINARY_SEARCH_PATHS", ["/nonexistent/piper"]):
            with patch("shutil.which", return_value=None):
                result = find_piper_binary()
        assert result is None

    def test_falls_back_to_path(self, tmp_path):
        binary = tmp_path / "piper"
        binary.write_text("#!/bin/sh\n")
        binary.chmod(0o755)

        with patch("tts.piper.PIPER_BINARY_SEARCH_PATHS", ["/nonexistent/piper"]):
            with patch("shutil.which", return_value=str(binary)):
                result = find_piper_binary()
        assert result == binary


# ---------------------------------------------------------------------------
# find_voice_model
# ---------------------------------------------------------------------------

class TestFindVoiceModel:
    def test_finds_model_in_search_dir(self, tmp_path):
        model = tmp_path / f"{DEFAULT_VOICE_MODEL_NAME}.onnx"
        model.write_bytes(b"model data")

        with patch("tts.piper.PIPER_MODEL_SEARCH_DIRS", [str(tmp_path)]):
            result = find_voice_model()
        assert result == model

    def test_returns_none_when_model_missing(self, tmp_path):
        with patch("tts.piper.PIPER_MODEL_SEARCH_DIRS", [str(tmp_path)]):
            result = find_voice_model()
        assert result is None

    def test_custom_model_name(self, tmp_path):
        custom_name = "en_US-amy-low"
        model = tmp_path / f"{custom_name}.onnx"
        model.write_bytes(b"model data")

        with patch("tts.piper.PIPER_MODEL_SEARCH_DIRS", [str(tmp_path)]):
            result = find_voice_model(model_name=custom_name)
        assert result == model


# ---------------------------------------------------------------------------
# synthesize_wav
# ---------------------------------------------------------------------------

class TestSynthesizeWav:
    def test_success_returns_none_error(self, tmp_path):
        cfg = _fake_piper_config(tmp_path)
        output_wav = tmp_path / "out.wav"

        def fake_run(cmd, **kwargs):
            # Simulate piper writing the output file
            output_wav.write_bytes(b"RIFF" + b"\x00" * 100)
            mock = MagicMock()
            mock.returncode = 0
            mock.stderr = ""
            return mock

        with patch("subprocess.run", side_effect=fake_run):
            err = synthesize_wav("Hello world", cfg, output_wav)

        assert err is None
        assert output_wav.exists()

    def test_returns_error_on_nonzero_exit(self, tmp_path):
        cfg = _fake_piper_config(tmp_path)
        output_wav = tmp_path / "out.wav"

        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "model error"

        with patch("subprocess.run", return_value=mock_result):
            err = synthesize_wav("Hello world", cfg, output_wav)

        assert err is not None
        assert "piper exited with code 1" in err
        assert "model error" in err

    def test_returns_error_on_timeout(self, tmp_path):
        import subprocess
        cfg = _fake_piper_config(tmp_path)
        output_wav = tmp_path / "out.wav"

        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd=["piper"], timeout=30)):
            err = synthesize_wav("Hello world", cfg, output_wav)

        assert err is not None
        assert "timed out" in err

    def test_returns_error_for_empty_text(self, tmp_path):
        cfg = _fake_piper_config(tmp_path)
        output_wav = tmp_path / "out.wav"

        err = synthesize_wav("", cfg, output_wav)
        assert err is not None
        assert "Empty text" in err

    def test_returns_error_when_config_not_usable(self, tmp_path):
        cfg = PiperConfig(
            piper_binary=tmp_path / "nonexistent",
            model_path=tmp_path / "nonexistent.onnx",
        )
        output_wav = tmp_path / "out.wav"

        err = synthesize_wav("Hello", cfg, output_wav)
        assert err is not None
        assert "not usable" in err

    def test_returns_error_when_output_empty(self, tmp_path):
        cfg = _fake_piper_config(tmp_path)
        output_wav = tmp_path / "out.wav"

        # piper exits 0 but writes nothing
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stderr = ""

        with patch("subprocess.run", return_value=mock_result):
            err = synthesize_wav("Hello world", cfg, output_wav)

        assert err is not None
        assert "no output" in err


# ---------------------------------------------------------------------------
# convert_wav_to_ogg
# ---------------------------------------------------------------------------

class TestConvertWavToOgg:
    def test_success_returns_none_error(self, tmp_path):
        wav = tmp_path / "voice.wav"
        wav.write_bytes(b"RIFF" + b"\x00" * 100)
        ogg = tmp_path / "voice.ogg"

        def fake_run(cmd, **kwargs):
            ogg.write_bytes(b"OggS" + b"\x00" * 200)
            mock = MagicMock()
            mock.returncode = 0
            mock.stderr = ""
            return mock

        with patch("shutil.which", return_value="/usr/bin/ffmpeg"):
            with patch("subprocess.run", side_effect=fake_run):
                err = convert_wav_to_ogg(wav, ogg)

        assert err is None
        assert ogg.exists()

    def test_returns_error_when_wav_missing(self, tmp_path):
        wav = tmp_path / "nonexistent.wav"
        ogg = tmp_path / "voice.ogg"

        err = convert_wav_to_ogg(wav, ogg)
        assert err is not None
        assert "not found" in err

    def test_returns_error_when_ffmpeg_missing(self, tmp_path):
        wav = tmp_path / "voice.wav"
        wav.write_bytes(b"RIFF" + b"\x00" * 100)
        ogg = tmp_path / "voice.ogg"

        with patch("shutil.which", return_value=None):
            err = convert_wav_to_ogg(wav, ogg)

        assert err is not None
        assert "ffmpeg not found" in err

    def test_returns_error_on_ffmpeg_nonzero_exit(self, tmp_path):
        wav = tmp_path / "voice.wav"
        wav.write_bytes(b"RIFF" + b"\x00" * 100)
        ogg = tmp_path / "voice.ogg"

        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "Invalid data found"

        with patch("shutil.which", return_value="/usr/bin/ffmpeg"):
            with patch("subprocess.run", return_value=mock_result):
                err = convert_wav_to_ogg(wav, ogg)

        assert err is not None
        assert "ffmpeg exited with code 1" in err

    def test_returns_error_on_timeout(self, tmp_path):
        import subprocess
        wav = tmp_path / "voice.wav"
        wav.write_bytes(b"RIFF" + b"\x00" * 100)
        ogg = tmp_path / "voice.ogg"

        with patch("shutil.which", return_value="/usr/bin/ffmpeg"):
            with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd=["ffmpeg"], timeout=30)):
                err = convert_wav_to_ogg(wav, ogg)

        assert err is not None
        assert "timed out" in err


# ---------------------------------------------------------------------------
# text_to_voice_file (full pipeline)
# ---------------------------------------------------------------------------

class TestTextToVoiceFile:
    def test_returns_ok_result_with_ogg_path_on_success(self, tmp_path):
        """Full pipeline produces TtsResult.ok=True with a non-None ogg_path."""
        def fake_run(cmd, **kwargs):
            # Determine which tool is being called from the command
            if "piper" in str(cmd[0]):
                # Find output_file arg and write WAV there
                idx = cmd.index("--output_file")
                Path(cmd[idx + 1]).write_bytes(b"RIFF" + b"\x00" * 100)
            else:
                # ffmpeg: write OGG to last arg
                Path(cmd[-1]).write_bytes(b"OggS" + b"\x00" * 200)
            mock = MagicMock()
            mock.returncode = 0
            mock.stderr = ""
            return mock

        with patch("tts.piper.resolve_piper_config") as mock_cfg, \
             patch("shutil.which", return_value="/usr/bin/ffmpeg"), \
             patch("subprocess.run", side_effect=fake_run):
            cfg = _fake_piper_config(tmp_path)
            mock_cfg.return_value = cfg
            result = text_to_voice_file("Hello world")

        assert result.ok is True
        assert result.ogg_path is not None
        assert result.error is None
        result.cleanup()

    def test_returns_failed_result_when_piper_not_available(self):
        """When piper config cannot be resolved, TtsResult.ok=False."""
        with patch("tts.piper.resolve_piper_config", return_value=None):
            result = text_to_voice_file("Hello world")

        assert result.ok is False
        assert result.error is not None
        assert "TTS unavailable" in result.error

    def test_returns_failed_result_when_synthesis_fails(self, tmp_path):
        """When piper synthesis fails, TtsResult.ok=False with error."""
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "model load error"

        with patch("tts.piper.resolve_piper_config") as mock_cfg, \
             patch("subprocess.run", return_value=mock_result):
            cfg = _fake_piper_config(tmp_path)
            mock_cfg.return_value = cfg
            result = text_to_voice_file("Hello world")

        assert result.ok is False
        assert result.error is not None
        assert "synthesis failed" in result.error

    def test_returns_failed_result_when_ogg_conversion_fails(self, tmp_path):
        """When ffmpeg conversion fails, TtsResult.ok=False with error."""
        def fake_run(cmd, **kwargs):
            if "piper" in str(cmd[0]):
                idx = cmd.index("--output_file")
                Path(cmd[idx + 1]).write_bytes(b"RIFF" + b"\x00" * 100)
                mock = MagicMock()
                mock.returncode = 0
                mock.stderr = ""
                return mock
            else:
                # ffmpeg fails
                mock = MagicMock()
                mock.returncode = 1
                mock.stderr = "codec not found"
                return mock

        with patch("tts.piper.resolve_piper_config") as mock_cfg, \
             patch("shutil.which", return_value="/usr/bin/ffmpeg"), \
             patch("subprocess.run", side_effect=fake_run):
            cfg = _fake_piper_config(tmp_path)
            mock_cfg.return_value = cfg
            result = text_to_voice_file("Hello world")

        assert result.ok is False
        assert result.error is not None
        assert "OGG conversion failed" in result.error

    def test_cleanup_removes_ogg_file(self, tmp_path):
        """After cleanup, the OGG temp file should be deleted."""
        ogg_path = None

        def fake_run(cmd, **kwargs):
            if "piper" in str(cmd[0]):
                idx = cmd.index("--output_file")
                Path(cmd[idx + 1]).write_bytes(b"RIFF" + b"\x00" * 100)
            else:
                # Write OGG to last arg
                Path(cmd[-1]).write_bytes(b"OggS" + b"\x00" * 200)
            mock = MagicMock()
            mock.returncode = 0
            mock.stderr = ""
            return mock

        with patch("tts.piper.resolve_piper_config") as mock_cfg, \
             patch("shutil.which", return_value="/usr/bin/ffmpeg"), \
             patch("subprocess.run", side_effect=fake_run):
            cfg = _fake_piper_config(tmp_path)
            mock_cfg.return_value = cfg
            result = text_to_voice_file("Hello world")

        assert result.ok is True
        ogg_path = result.ogg_path
        assert ogg_path.exists()

        result.cleanup()
        assert not ogg_path.exists()
