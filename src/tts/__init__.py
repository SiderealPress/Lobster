"""Text-to-speech synthesis for Lobster.

Provides local TTS using piper (neural TTS, high quality, no cloud dependency).
Falls back gracefully when the binary or model is unavailable.
"""
from .piper import (
    PiperConfig,
    TtsResult,
    find_piper_binary,
    find_voice_model,
    synthesize_wav,
    convert_wav_to_ogg,
    text_to_voice_file,
)

__all__ = [
    "PiperConfig",
    "TtsResult",
    "find_piper_binary",
    "find_voice_model",
    "synthesize_wav",
    "convert_wav_to_ogg",
    "text_to_voice_file",
]
