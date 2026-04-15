"""Piper TTS integration for Lobster.

Pure functions for text-to-speech synthesis using piper (https://github.com/rhasspy/piper).

Flow:
    text → piper CLI → WAV file → ffmpeg → OGG/Opus file → Telegram send_voice

All functions are pure and side-effect-free except for file I/O. Callers manage
temporary file lifetimes using TtsResult.cleanup().
"""

import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration constants (named after spec requirements)
# ---------------------------------------------------------------------------

# Default voice model: lessac-medium — natural quality, moderate size (~30MB)
DEFAULT_VOICE_MODEL_NAME = "en_US-lessac-medium"

# Piper model file extension
PIPER_MODEL_EXTENSION = ".onnx"

# Search paths for the piper binary (in priority order)
PIPER_BINARY_SEARCH_PATHS = [
    "/usr/local/bin/piper",
    os.path.expanduser("~/.local/bin/piper"),
]

# Search paths for voice models (in priority order)
PIPER_MODEL_SEARCH_DIRS = [
    os.path.expanduser("~/lobster-workspace/piper-models"),
    os.path.expanduser("~/.local/share/piper"),
    "/usr/local/share/piper",
]

# Subprocess timeout for piper synthesis (seconds)
PIPER_SYNTHESIS_TIMEOUT_S = 30

# Subprocess timeout for ffmpeg OGG conversion (seconds)
FFMPEG_CONVERT_TIMEOUT_S = 30

# OGG/Opus codec settings for Telegram voice notes
# Telegram requires OGG/Opus; 24kbps is intelligible and small
FFMPEG_OPUS_BITRATE = "24k"
FFMPEG_OPUS_SAMPLE_RATE = "24000"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PiperConfig:
    """Resolved piper configuration (binary + model paths).

    Produced by find_piper_binary() + find_voice_model(). Immutable after
    construction so it can be passed through pure function chains safely.
    """
    piper_binary: Path
    model_path: Path

    def is_usable(self) -> bool:
        """Return True if both binary and model file exist and are executable."""
        return (
            self.piper_binary.exists()
            and os.access(str(self.piper_binary), os.X_OK)
            and self.model_path.exists()
        )


@dataclass
class TtsResult:
    """Result of a TTS synthesis pipeline run.

    On success: ogg_path points to the generated OGG/Opus file.
    On failure: error contains a human-readable message; ogg_path is None.

    Callers are responsible for calling cleanup() to delete the temp file.
    """
    ok: bool
    ogg_path: Optional[Path] = None
    error: Optional[str] = None
    _cleanup_paths: list = field(default_factory=list, repr=False)

    def cleanup(self) -> None:
        """Delete any temporary files created during synthesis."""
        for p in self._cleanup_paths:
            try:
                path = Path(p)
                if path.exists():
                    path.unlink()
            except OSError as e:
                log.warning(f"TtsResult.cleanup: failed to delete {p}: {e}")
        self._cleanup_paths.clear()


# ---------------------------------------------------------------------------
# Discovery functions
# ---------------------------------------------------------------------------

def find_piper_binary() -> Optional[Path]:
    """Return the path to the piper binary, or None if not found.

    Searches PIPER_BINARY_SEARCH_PATHS in order, then falls back to PATH.
    Pure: reads the filesystem but has no side effects.
    """
    for candidate in PIPER_BINARY_SEARCH_PATHS:
        p = Path(candidate)
        if p.exists() and os.access(str(p), os.X_OK):
            log.debug(f"piper binary found at {p}")
            return p

    # Fall back to shutil.which (searches PATH)
    found = shutil.which("piper")
    if found:
        log.debug(f"piper binary found via PATH at {found}")
        return Path(found)

    log.warning(
        "piper binary not found. "
        f"Searched: {PIPER_BINARY_SEARCH_PATHS} and PATH. "
        "Install piper to /usr/local/bin/piper or ~/.local/bin/piper."
    )
    return None


def find_voice_model(model_name: str = DEFAULT_VOICE_MODEL_NAME) -> Optional[Path]:
    """Return the path to a piper voice model file, or None if not found.

    Searches PIPER_MODEL_SEARCH_DIRS for a file named <model_name>.onnx.
    Pure: reads the filesystem but has no side effects.
    """
    filename = model_name + PIPER_MODEL_EXTENSION
    for search_dir in PIPER_MODEL_SEARCH_DIRS:
        candidate = Path(search_dir) / filename
        if candidate.exists():
            log.debug(f"piper model found at {candidate}")
            return candidate

    log.warning(
        f"piper voice model '{filename}' not found. "
        f"Searched: {PIPER_MODEL_SEARCH_DIRS}. "
        "Download from https://huggingface.co/rhasspy/piper-voices"
    )
    return None


def resolve_piper_config(
    model_name: str = DEFAULT_VOICE_MODEL_NAME,
) -> Optional[PiperConfig]:
    """Resolve binary + model into a PiperConfig, or None if either is missing.

    Composition of find_piper_binary() + find_voice_model().
    Pure: no side effects.
    """
    binary = find_piper_binary()
    if binary is None:
        return None
    model = find_voice_model(model_name)
    if model is None:
        return None
    cfg = PiperConfig(piper_binary=binary, model_path=model)
    if not cfg.is_usable():
        log.warning(f"PiperConfig not usable: binary={binary} model={model}")
        return None
    return cfg


# ---------------------------------------------------------------------------
# Synthesis functions
# ---------------------------------------------------------------------------

def synthesize_wav(
    text: str,
    config: PiperConfig,
    output_path: Path,
) -> Optional[str]:
    """Run piper to synthesize text into a WAV file at output_path.

    Returns None on success, or an error message string on failure.
    Side effect: writes a WAV file at output_path.
    """
    if not text.strip():
        return "Empty text provided for TTS synthesis"

    if not config.is_usable():
        return f"PiperConfig not usable: binary={config.piper_binary} model={config.model_path}"

    cmd = [
        str(config.piper_binary),
        "--model", str(config.model_path),
        "--output_file", str(output_path),
    ]

    log.debug(f"synthesize_wav: running {cmd!r} with text len={len(text)}")

    try:
        result = subprocess.run(
            cmd,
            input=text,
            capture_output=True,
            text=True,
            timeout=PIPER_SYNTHESIS_TIMEOUT_S,
        )
        if result.returncode != 0:
            stderr = result.stderr.strip()
            return f"piper exited with code {result.returncode}: {stderr}"
        if not output_path.exists() or output_path.stat().st_size == 0:
            return f"piper produced no output at {output_path}"
        log.debug(f"synthesize_wav: success, WAV size={output_path.stat().st_size} bytes")
        return None
    except subprocess.TimeoutExpired:
        return f"piper timed out after {PIPER_SYNTHESIS_TIMEOUT_S}s synthesizing {len(text)} chars"
    except FileNotFoundError:
        return f"piper binary not found at {config.piper_binary}"
    except OSError as e:
        return f"piper subprocess error: {e}"


def convert_wav_to_ogg(wav_path: Path, ogg_path: Path) -> Optional[str]:
    """Convert a WAV file to OGG/Opus format using ffmpeg.

    Telegram voice notes require OGG/Opus. This function wraps ffmpeg with
    settings appropriate for voice (24kHz, 24kbps Opus).

    Returns None on success, or an error message string on failure.
    Side effect: writes an OGG file at ogg_path.
    """
    if not wav_path.exists():
        return f"WAV file not found: {wav_path}"

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return "ffmpeg not found in PATH — required for OGG/Opus conversion"

    cmd = [
        ffmpeg,
        "-y",                          # overwrite output without asking
        "-i", str(wav_path),           # input WAV
        "-c:a", "libopus",             # Opus codec
        "-b:a", FFMPEG_OPUS_BITRATE,   # bitrate
        "-ar", FFMPEG_OPUS_SAMPLE_RATE,  # sample rate
        "-ac", "1",                    # mono
        "-vbr", "on",                  # variable bitrate (better quality)
        "-compression_level", "10",    # max compression
        str(ogg_path),
    ]

    log.debug(f"convert_wav_to_ogg: running ffmpeg WAV→OGG")

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=FFMPEG_CONVERT_TIMEOUT_S,
        )
        if result.returncode != 0:
            stderr = result.stderr.strip()[-500:]  # last 500 chars of ffmpeg stderr
            return f"ffmpeg exited with code {result.returncode}: {stderr}"
        if not ogg_path.exists() or ogg_path.stat().st_size == 0:
            return f"ffmpeg produced no output at {ogg_path}"
        log.debug(f"convert_wav_to_ogg: success, OGG size={ogg_path.stat().st_size} bytes")
        return None
    except subprocess.TimeoutExpired:
        return f"ffmpeg timed out after {FFMPEG_CONVERT_TIMEOUT_S}s"
    except FileNotFoundError:
        return "ffmpeg binary not found"
    except OSError as e:
        return f"ffmpeg subprocess error: {e}"


def text_to_voice_file(
    text: str,
    model_name: str = DEFAULT_VOICE_MODEL_NAME,
) -> TtsResult:
    """Full TTS pipeline: text → OGG/Opus file ready for Telegram.

    Composes resolve_piper_config() + synthesize_wav() + convert_wav_to_ogg()
    into a single call. Creates temporary files managed by the returned TtsResult.

    On failure at any stage, returns TtsResult(ok=False, error=...) without
    raising. Caller must call result.cleanup() after use.

    Args:
        text: Text to synthesize. Must be non-empty.
        model_name: Piper voice model name (default: lessac-medium).

    Returns:
        TtsResult with ok=True and ogg_path set on success,
        or ok=False and error set on failure.
    """
    result = TtsResult(ok=False)

    # Resolve piper config
    config = resolve_piper_config(model_name)
    if config is None:
        result.error = (
            "TTS unavailable: piper binary or voice model not found. "
            "Run install.sh to install piper and the lessac-medium model."
        )
        return result

    # Create temp directory for intermediate files
    tmp_dir = Path(tempfile.mkdtemp(prefix="lobster-tts-"))
    result._cleanup_paths.append(str(tmp_dir / "voice.wav"))
    result._cleanup_paths.append(str(tmp_dir / "voice.ogg"))

    wav_path = tmp_dir / "voice.wav"
    ogg_path = tmp_dir / "voice.ogg"

    # Step 1: synthesize WAV
    err = synthesize_wav(text, config, wav_path)
    if err:
        result.error = f"TTS synthesis failed: {err}"
        # Clean up tmp_dir itself
        try:
            import shutil as _shutil
            _shutil.rmtree(str(tmp_dir), ignore_errors=True)
        except Exception:
            pass
        result._cleanup_paths.clear()
        return result

    # Step 2: convert WAV → OGG/Opus
    err = convert_wav_to_ogg(wav_path, ogg_path)
    if err:
        result.error = f"OGG conversion failed: {err}"
        try:
            import shutil as _shutil
            _shutil.rmtree(str(tmp_dir), ignore_errors=True)
        except Exception:
            pass
        result._cleanup_paths.clear()
        return result

    # Clean WAV (no longer needed), keep OGG
    try:
        wav_path.unlink()
    except OSError:
        pass
    result._cleanup_paths = [str(ogg_path)]

    result.ok = True
    result.ogg_path = ogg_path
    return result
