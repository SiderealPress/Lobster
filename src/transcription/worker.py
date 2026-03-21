#!/usr/bin/env python3
"""
Lobster Transcription Worker - Approach C

Watches ~/messages/pending-transcription/ for voice message JSON files,
runs whisper.cpp transcription, then moves completed messages to
~/messages/inbox/ so Claude only ever sees fully-transcribed messages.

Message flow:
  lobster_bot.py → pending-transcription/{id}.json
  worker.py      → runs whisper.cpp on audio_file
  worker.py      → inbox/{id}.json  (transcription populated, text replaced)

Error handling:
  - transient failures → retry up to MAX_RETRIES times with backoff
  - permanent failures (missing audio, whisper binary gone) → dead-letter
  - timeout (> TRANSCRIPTION_TIMEOUT_S) → treat as transient failure
"""

import asyncio
import json
import logging
import os
import re
import shutil
import signal
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
import sys as _sys
_SRC_DIR = str(Path(__file__).resolve().parent.parent)
if _SRC_DIR not in _sys.path:
    _sys.path.insert(0, _SRC_DIR)
from utils.fs import atomic_write_json  # noqa: E402
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_MESSAGES = Path(os.environ.get("LOBSTER_MESSAGES", Path.home() / "messages"))
_WORKSPACE = Path(os.environ.get("LOBSTER_WORKSPACE", Path.home() / "lobster-workspace"))
_CONFIG_DIR = Path(os.environ.get("LOBSTER_CONFIG_DIR", Path.home() / "lobster-config"))

PENDING_DIR = _MESSAGES / "pending-transcription"
INBOX_DIR = _MESSAGES / "inbox"
AUDIO_DIR = _MESSAGES / "audio"
DEAD_LETTER_DIR = _MESSAGES / "dead-letter"

FFMPEG_PATH = Path.home() / ".local" / "bin" / "ffmpeg"
WHISPER_CPP_PATH = _WORKSPACE / "whisper.cpp" / "build" / "bin" / "whisper-cli"
WHISPER_MODEL_PATH = _WORKSPACE / "whisper.cpp" / "models" / "ggml-small.bin"

# ---------------------------------------------------------------------------
# Tuning constants
# ---------------------------------------------------------------------------
MAX_RETRIES = 3
BASE_RETRY_DELAY_S = 5       # first retry after 5 s, doubles each time
TRANSCRIPTION_TIMEOUT_S = 300  # default 5 min; overridden dynamically by audio duration
MIN_TIMEOUT_S = 120            # floor for short messages (startup overhead dominates)
TIMEOUT_MULTIPLIER = 6         # timeout = max(MIN_TIMEOUT_S, duration * this)
BRAIN_DUMP_THRESHOLD_S = 120   # audio_duration ≥ this → is_brain_dump: true
POLL_INTERVAL_S = 2            # fallback polling period if watchdog misses an event
WORKER_LOOP_INTERVAL_S = 0.25  # main asyncio loop tick

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("transcription-worker")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def get_audio_duration(audio_path: Path) -> float:
    """Get audio duration in seconds using ffprobe. Returns 0 on failure."""
    ffprobe = "ffprobe"
    if FFMPEG_PATH.exists():
        ffprobe = str(FFMPEG_PATH.parent / "ffprobe")
    try:
        proc = await asyncio.create_subprocess_exec(
            ffprobe, "-v", "quiet", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(audio_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        return float(stdout.decode().strip())
    except Exception:
        return 0


def compute_timeout(audio_duration: float) -> int:
    """Compute transcription timeout scaled to audio length."""
    if audio_duration > 0:
        return max(MIN_TIMEOUT_S, int(audio_duration * TIMEOUT_MULTIPLIER))
    return TRANSCRIPTION_TIMEOUT_S



async def convert_ogg_to_wav(ogg_path: Path, wav_path: Path, timeout_s: int = TRANSCRIPTION_TIMEOUT_S) -> bool:
    """Convert OGG/OPUS audio to 16 kHz mono WAV for whisper.cpp.

    Writes to a temp file first and renames atomically so a partial or failed
    conversion never leaves a corrupt WAV that would be mistaken for a valid one
    on a subsequent retry.
    """
    ffmpeg = str(FFMPEG_PATH) if FFMPEG_PATH.exists() else "ffmpeg"

    # Write to a temp file in the same directory so rename is atomic.
    fd, tmp_wav = tempfile.mkstemp(dir=str(wav_path.parent), suffix=".wav.tmp")
    os.close(fd)
    tmp_wav_path = Path(tmp_wav)

    cmd = [
        ffmpeg, "-i", str(ogg_path),
        "-ar", "16000",   # 16 kHz
        "-ac", "1",       # mono
        "-y",             # overwrite
        "-f", "wav",      # explicit output format (file has .wav.tmp extension)
        str(tmp_wav_path),
    ]
    proc: asyncio.subprocess.Process | None = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(
            proc.communicate(),
            timeout=timeout_s,
        )
    except asyncio.TimeoutError:
        if proc is not None:
            try:
                proc.kill()
            except Exception:
                pass
            try:
                await proc.wait()
            except Exception:
                pass
        log.warning(f"ffmpeg conversion timed out after {timeout_s}s")
        tmp_wav_path.unlink(missing_ok=True)
        return False
    except Exception as e:
        log.warning(f"ffmpeg conversion error: {e}")
        tmp_wav_path.unlink(missing_ok=True)
        return False

    if proc.returncode != 0:
        log.warning(f"ffmpeg conversion failed: {stderr.decode().strip()}")
        tmp_wav_path.unlink(missing_ok=True)
        return False

    # Success — atomically promote the temp file to the real WAV path.
    tmp_wav_path.rename(wav_path)
    return True


async def run_whisper_cpp(audio_path: Path, timeout_s: int = TRANSCRIPTION_TIMEOUT_S) -> tuple[bool, str]:
    """Run whisper.cpp CLI. Returns (success, transcription_or_error)."""
    if not WHISPER_CPP_PATH.exists():
        return False, f"whisper.cpp binary not found at {WHISPER_CPP_PATH}"
    if not WHISPER_MODEL_PATH.exists():
        return False, f"Whisper model not found at {WHISPER_MODEL_PATH}"

    cmd = [
        str(WHISPER_CPP_PATH),
        "-m", str(WHISPER_MODEL_PATH),
        "-f", str(audio_path),
        "-l", "en",
        # NOTE: do NOT pass -nt / --no-timestamps here.
        # With that flag, whisper.cpp collapses all segments and only emits the
        # last one, silently truncating long audio.  We let whisper output the
        # default "[HH:MM:SS.mmm --> HH:MM:SS.mmm]  text" format; the
        # post-processing below strips timestamp lines to produce clean text.
        "--no-prints",  # suppress progress noise
    ]

    proc: asyncio.subprocess.Process | None = None
    try:
        # Use a single deadline for both spawn and communicate so the effective
        # wall-clock limit is exactly TRANSCRIPTION_TIMEOUT_S, not 2x.
        async with asyncio.timeout(timeout_s):
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
    except asyncio.TimeoutError:
        # Kill the child and reap it so it doesn't become a zombie.
        if proc is not None:
            try:
                proc.kill()
            except Exception:
                pass
            try:
                await proc.wait()
            except Exception:
                pass
        return False, f"whisper.cpp timed out after {timeout_s}s"

    if proc.returncode != 0:
        error_msg = stderr.decode().strip() if stderr else "unknown error"
        return False, f"whisper.cpp exited {proc.returncode}: {error_msg}"

    raw = stdout.decode().strip()
    # whisper-cli emits lines in the form:
    #   [HH:MM:SS.mmm --> HH:MM:SS.mmm]   transcription text here
    # The transcription text lives on the same line as the timestamp bracket,
    # so we must extract the text after "]" rather than discard the whole line.
    # Lines that contain no timestamp (e.g. blank lines) are included as-is.
    _TIMESTAMP_RE = re.compile(r"^\[[\d:.,\s\-–>]+\]\s*")
    lines = []
    for ln in raw.split("\n"):
        stripped = ln.strip()
        if not stripped:
            continue
        text = _TIMESTAMP_RE.sub("", stripped).strip()
        if text:
            lines.append(text)
    transcription = " ".join(lines).strip()
    return True, transcription


def _read_admin_chat_id() -> int | None:
    """Read the first TELEGRAM_ALLOWED_USERS entry from config.env as the admin chat_id.

    Returns None if config is unavailable or the value cannot be parsed.
    This mirrors the same lookup in inbox_server.py (_resolve_debug_config).
    """
    try:
        config_file = _CONFIG_DIR / "config.env"
        if not config_file.exists():
            return None
        for line in config_file.read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith("TELEGRAM_ALLOWED_USERS="):
                val = stripped.split("=", 1)[1].strip().strip('"').strip("'")
                first = val.split(",")[0].strip()
                if first.lstrip("-").isdigit():
                    return int(first)
    except Exception:
        pass
    return None


def notify_dispatcher_dead_letter(msg_data: dict, reason: str) -> None:
    """Write a system_error observation to the dispatcher inbox on dead-letter.

    Drops a subagent_observation JSON file directly into ~/messages/inbox/ so
    the dispatcher picks it up on its next wait_for_messages() call. This
    mirrors the format written by handle_write_observation() in inbox_server.py.

    Uses LOBSTER_ADMIN_CHAT_ID env var if set, otherwise reads the first entry
    of TELEGRAM_ALLOWED_USERS from config.env. Silent on any failure — alerting
    must never block or crash the transcription pipeline.
    """
    try:
        # Resolve admin chat_id: env override takes priority
        chat_id: int | None = None
        env_val = os.environ.get("LOBSTER_ADMIN_CHAT_ID", "").strip()
        if env_val.lstrip("-").isdigit():
            chat_id = int(env_val)
        else:
            chat_id = _read_admin_chat_id()

        if chat_id is None:
            log.warning("notify_dispatcher_dead_letter: no admin chat_id found, skipping alert")
            return

        msg_id = msg_data.get("id", "unknown")
        # Use _pending_file hint if set by move_to_dead_letter, fall back to msg_id
        pending_file_name = msg_data.get("_pending_file", msg_id)
        now = datetime.now(timezone.utc)
        ts_ms = int(now.timestamp() * 1000)
        observation_id = f"{ts_ms}_observation_{uuid.uuid4().hex[:8]}"

        alert_text = (
            f"[system_error] Transcription dead-letter: {pending_file_name}\n"
            f"Message ID: {msg_id}\n"
            f"Reason: {reason}"
        )

        observation: dict = {
            "id": observation_id,
            "type": "subagent_observation",
            "source": "telegram",
            "chat_id": chat_id,
            "text": alert_text,
            "category": "system_error",
            "timestamp": now.isoformat(),
            "task_id": f"transcription-dead-letter-{msg_id}",
        }

        INBOX_DIR.mkdir(parents=True, exist_ok=True)
        inbox_file = INBOX_DIR / f"{observation_id}.json"
        atomic_write_json(inbox_file, observation)
        log.info(f"Dead-letter alert queued for dispatcher (chat_id={chat_id}): {observation_id}")
    except Exception as e:
        log.warning(f"notify_dispatcher_dead_letter failed (non-fatal): {e}")


def move_to_dead_letter(pending_file: Path, msg_data: dict, reason: str) -> None:
    """Move a failed file to dead-letter, annotating with failure reason."""
    DEAD_LETTER_DIR.mkdir(parents=True, exist_ok=True)
    msg_data["transcription_failure_reason"] = reason
    msg_data["transcription_failed_at"] = datetime.now(timezone.utc).isoformat()
    # Stash the filename so notify_dispatcher_dead_letter can include it in the alert.
    msg_data["_pending_file"] = pending_file.name
    dest = DEAD_LETTER_DIR / pending_file.name
    # Pop internal field before writing to disk so it doesn't leak into stored JSON.
    payload = {k: v for k, v in msg_data.items() if k != "_pending_file"}
    atomic_write_json(dest, payload)
    pending_file.unlink(missing_ok=True)
    log.error(f"Moved {pending_file.name} to dead-letter: {reason}")
    notify_dispatcher_dead_letter(msg_data, reason)


# ---------------------------------------------------------------------------
# Core transcription logic
# ---------------------------------------------------------------------------

async def transcribe_pending_file(pending_file: Path) -> None:
    """
    Process one pending-transcription JSON file end-to-end.

    Retry loop:
      attempt 1..MAX_RETRIES  →  exponential backoff on transient errors
      permanent errors        →  dead-letter immediately, no retry
    """
    log.info(f"Processing: {pending_file.name}")

    # Load the message
    try:
        with open(pending_file) as f:
            msg_data = json.load(f)
    except Exception as e:
        log.error(f"Cannot read {pending_file}: {e}")
        return  # leave file in place — it may still be mid-write

    # Validate it's a voice message ("audio" normalized to "voice" at ingest; issue #635)
    if msg_data.get("type") != "voice":
        move_to_dead_letter(
            pending_file, msg_data,
            f"Unexpected type in pending-transcription: {msg_data.get('type')!r}"
        )
        return

    audio_path = Path(msg_data.get("audio_file", ""))
    if not audio_path.exists():
        move_to_dead_letter(
            pending_file, msg_data,
            f"Audio file not found: {audio_path}"
        )
        return

    # Get audio duration — prefer metadata from bot, fall back to ffprobe
    audio_duration = msg_data.get("audio_duration", 0)
    if not audio_duration:
        audio_duration = await get_audio_duration(audio_path)
    is_brain_dump = audio_duration >= BRAIN_DUMP_THRESHOLD_S

    # Compute timeout scaled to audio length
    timeout_s = compute_timeout(audio_duration)
    log.info(f"  Audio duration: {audio_duration:.0f}s → timeout: {timeout_s}s")

    # Audio → WAV conversion for any non-WAV format (once; reuse on retries).
    # convert_ogg_to_wav writes atomically (temp-then-rename), so any existing
    # file at wav_path is guaranteed to be a complete, valid conversion.
    wav_path: Path | None = None
    if audio_path.suffix.lower() not in (".wav",):
        wav_path = audio_path.with_suffix(".wav")
        if not wav_path.exists():
            ok = await convert_ogg_to_wav(audio_path, wav_path, timeout_s=timeout_s)
            if not ok:
                # ffmpeg failure is generally permanent (bad file or binary missing)
                move_to_dead_letter(
                    pending_file, msg_data,
                    f"ffmpeg audio→WAV conversion failed ({audio_path.suffix})"
                )
                return
    transcribe_path = wav_path if wav_path else audio_path

    # Retry loop
    last_error = ""
    try:
        for attempt in range(1, MAX_RETRIES + 1):
            log.info(f"  whisper attempt {attempt}/{MAX_RETRIES} for {pending_file.name}")
            success, result = await run_whisper_cpp(transcribe_path, timeout_s=timeout_s)

            if success and result:
                # --- SUCCESS ---
                transcription = result
                msg_data["transcription"] = transcription
                msg_data["text"] = transcription          # replace placeholder
                msg_data["transcribed_at"] = datetime.now(timezone.utc).isoformat()
                msg_data["transcription_model"] = "whisper.cpp-small"
                msg_data["transcription_worker"] = "approach-c"
                if is_brain_dump:
                    msg_data["is_brain_dump"] = True
                    log.info(f"  Tagged as brain dump (duration={audio_duration}s)")

                # Write to inbox atomically, then remove from pending
                inbox_file = INBOX_DIR / pending_file.name
                atomic_write_json(inbox_file, msg_data)
                pending_file.unlink(missing_ok=True)
                log.info(f"  Transcription done → inbox/{pending_file.name}")
                return

            elif success and not result:
                last_error = "whisper returned empty transcription"
            else:
                last_error = result  # error message from run_whisper_cpp

            log.warning(f"  Attempt {attempt} failed: {last_error}")

            if attempt < MAX_RETRIES:
                delay = BASE_RETRY_DELAY_S * (2 ** (attempt - 1))
                log.info(f"  Retrying in {delay}s...")
                await asyncio.sleep(delay)

        # All retries exhausted
        move_to_dead_letter(
            pending_file, msg_data,
            f"All {MAX_RETRIES} whisper attempts failed. Last error: {last_error}"
        )
    finally:
        # Clean up the WAV file we created; the original OGG/OPUS is kept as-is.
        if wav_path is not None:
            wav_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Watchdog integration
# ---------------------------------------------------------------------------

class PendingDirHandler(FileSystemEventHandler):
    """Feeds newly-created .json files into the async work queue."""

    def __init__(self, queue: asyncio.Queue, loop: asyncio.AbstractEventLoop):
        super().__init__()
        self._queue = queue
        self._loop = loop

    def on_created(self, event):
        if event.is_directory:
            return
        p = Path(event.src_path)
        if p.suffix == ".json" and not p.name.startswith(".") and not p.name.endswith(".tmp"):
            log.debug(f"Watchdog: new file {p.name}")
            asyncio.run_coroutine_threadsafe(self._queue.put(p), self._loop)

    # Also catch moves-into-directory (e.g. from atomic_write_json rename)
    def on_moved(self, event):
        if event.is_directory:
            return
        p = Path(event.dest_path)
        if p.parent == PENDING_DIR and p.suffix == ".json" and not p.name.endswith(".tmp"):
            log.debug(f"Watchdog: moved-in {p.name}")
            asyncio.run_coroutine_threadsafe(self._queue.put(p), self._loop)


# ---------------------------------------------------------------------------
# Main event loop
# ---------------------------------------------------------------------------

async def main() -> None:
    # Ensure all required directories exist
    for d in (PENDING_DIR, INBOX_DIR, AUDIO_DIR, DEAD_LETTER_DIR):
        d.mkdir(parents=True, exist_ok=True)

    log.info(f"Transcription worker starting")
    log.info(f"  pending-transcription : {PENDING_DIR}")
    log.info(f"  inbox                 : {INBOX_DIR}")
    log.info(f"  whisper.cpp           : {WHISPER_CPP_PATH}")
    log.info(f"  model                 : {WHISPER_MODEL_PATH}")

    if not WHISPER_CPP_PATH.exists():
        log.error(f"FATAL: whisper-cli not found at {WHISPER_CPP_PATH}")
        sys.exit(1)
    if not WHISPER_MODEL_PATH.exists():
        log.error(f"FATAL: model not found at {WHISPER_MODEL_PATH}")
        sys.exit(1)

    queue: asyncio.Queue[Path] = asyncio.Queue()
    loop = asyncio.get_running_loop()

    # Start watchdog observer
    handler = PendingDirHandler(queue, loop)
    observer = Observer()
    observer.schedule(handler, str(PENDING_DIR), recursive=False)
    observer.start()
    log.info("Watchdog observer started")

    # Drain any files that arrived before we started (e.g. after a crash restart)
    existing = sorted(PENDING_DIR.glob("*.json"))
    if existing:
        log.info(f"Found {len(existing)} pre-existing file(s) in pending-transcription, queuing...")
    for f in existing:
        if not f.name.endswith(".tmp"):
            await queue.put(f)

    # Graceful shutdown on SIGTERM / SIGINT
    shutdown = asyncio.Event()

    def _stop(signum, frame):
        log.info(f"Signal {signum} received, shutting down...")
        shutdown.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    # Worker loop: drain queue; also do a periodic poll to catch any files
    # watchdog may have missed (belt-and-suspenders).
    last_poll = time.monotonic()

    while not shutdown.is_set():
        # Drain all immediately-available items from the queue
        drained: list[Path] = []
        try:
            while True:
                drained.append(queue.get_nowait())
        except asyncio.QueueEmpty:
            pass

        for pending_file in drained:
            if not pending_file.exists():
                continue  # already processed or removed
            await transcribe_pending_file(pending_file)

        # Periodic fallback poll
        now = time.monotonic()
        if now - last_poll >= POLL_INTERVAL_S:
            last_poll = now
            for f in sorted(PENDING_DIR.glob("*.json")):
                if f.name.endswith(".tmp"):
                    continue
                # Only queue if not already being tracked
                # (put_nowait is fine; transcribe_pending_file is idempotent on missing files)
                try:
                    queue.put_nowait(f)
                except asyncio.QueueFull:
                    pass

        await asyncio.sleep(WORKER_LOOP_INTERVAL_S)

    observer.stop()
    observer.join()
    log.info("Transcription worker stopped.")


if __name__ == "__main__":
    asyncio.run(main())
