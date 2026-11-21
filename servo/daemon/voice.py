"""Cactus Whistle (16.9 MB) Voice Ingestion Pipeline.

See ADR-13 in SERVO_ARCHITECTURE_V2.md and SERVO_HARDENING_NOTES.md.
Transcribes local audio clips on CPU using Cactus Whistle and dispatches
voice commands into the Servo reflex arc. Completely on-device and local.
"""

from __future__ import annotations

import io
import logging
import os
import tempfile
import threading
import wave
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_whistle_lock = threading.Lock()
_cached_whistle = None
_cached_whisper_model = None


def _get_whistle():
    global _cached_whistle
    if _cached_whistle is None:
        with _whistle_lock:
            if _cached_whistle is None:
                import cactus  # type: ignore
                if hasattr(cactus, "Whistle"):
                    _cached_whistle = cactus.Whistle()
                elif hasattr(cactus, "transcribe"):
                    _cached_whistle = cactus
    return _cached_whistle


def _get_whisper_model():
    global _cached_whisper_model
    if _cached_whisper_model is None:
        with _whistle_lock:
            if _cached_whisper_model is None:
                from faster_whisper import WhisperModel  # type: ignore
                _cached_whisper_model = WhisperModel("tiny.en", device="cpu", compute_type="int8")
    return _cached_whisper_model


class VoiceUnavailableError(RuntimeError):
    """Raised when no speech-to-text backend is available or transcription failed."""


def _extract_text(res: object) -> str:
    if isinstance(res, dict) and isinstance(res.get("text"), str):
        return res["text"].strip()
    if isinstance(res, str):
        return res.strip()
    raise VoiceUnavailableError(f"Unexpected transcription result type: {type(res).__name__}")


def _ensure_16khz_wav(audio_bytes_or_path: bytes | str | Path) -> str:
    """Ensure audio is formatted as a 16 kHz mono 16-bit PCM WAV file for Cactus Whistle."""
    if isinstance(audio_bytes_or_path, (str, Path)):
        path = Path(audio_bytes_or_path)
        if not path.is_file():
            raise VoiceUnavailableError(f"Audio file not found: {path}")
        raw_bytes = path.read_bytes()
    else:
        raw_bytes = bytes(audio_bytes_or_path)

    if not raw_bytes:
        raise VoiceUnavailableError("Audio data is empty.")

    # Check if already a 16 kHz mono WAV
    if raw_bytes.startswith(b"RIFF") and len(raw_bytes) > 44:
        try:
            with wave.open(io.BytesIO(raw_bytes), "rb") as wf:
                if wf.getnchannels() == 1 and wf.getframerate() == 16000 and wf.getsampwidth() == 2:
                    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                        tmp.write(raw_bytes)
                        return tmp.name
        except Exception:
            pass

    # Transcode using PyAV into 16 kHz mono 16-bit PCM WAV
    try:
        import av  # type: ignore
        inp = av.open(io.BytesIO(raw_bytes))
        out_buf = io.BytesIO()
        out_container = av.open(out_buf, "w", format="wav")
        resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
        out_stream = out_container.add_stream("pcm_s16le", rate=16000)
        out_stream.layout = "mono"

        for frame in inp.decode(audio=0):
            resampled = resampler.resample(frame)
            if resampled:
                for r_frame in resampled:
                    for packet in out_stream.encode(r_frame):
                        out_container.mux(packet)
        for packet in out_stream.encode(None):
            out_container.mux(packet)
        out_container.close()
        inp.close()

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp.write(out_buf.getvalue())
            return tmp.name
    except Exception as e:
        logger.debug("PyAV transcode failed (%s); writing raw bytes to temp file", e)
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp.write(raw_bytes)
            return tmp.name


class VoiceIngestionManager:
    """Manages audio transcription and voice task dispatch."""

    def transcribe_audio(self, audio_bytes_or_path: bytes | str | Path) -> str:
        """Transcribe an audio clip locally using Cactus Whistle (16.9 MB).

        Converts input audio (WAV, WebM, Ogg) to 16 kHz mono PCM WAV and transcribes
        completely on-device. No cloud STT or OpenAI backend is used.
        """
        wav_path = _ensure_16khz_wav(audio_bytes_or_path)
        try:
            # 1. Try local Cactus Whistle
            try:
                whistle = _get_whistle()
                if whistle is not None:
                    res = whistle.transcribe(wav_path)
                    text = _extract_text(res)
                    if not text:
                        raise VoiceUnavailableError("Transcription was empty.")
                    return text
            except VoiceUnavailableError:
                raise
            except (ImportError, AttributeError):
                pass
            except Exception as e:
                logger.warning("Cactus Whistle transcription failed: %s", e)

            # 2. Fall back to local on-device faster-whisper if cactus is not loaded
            try:
                model = _get_whisper_model()
                segments, _ = model.transcribe(wav_path, beam_size=1)
                text = " ".join(s.text for s in segments).strip()
                if not text:
                    raise VoiceUnavailableError("Transcription was empty.")
                return text
            except VoiceUnavailableError:
                raise
            except Exception as e:
                logger.debug("faster-whisper transcription error: %s", e)

            raise VoiceUnavailableError("Cactus Whistle is not installed (pip install servo[cactus]).")
        finally:
            try:
                os.remove(wav_path)
            except OSError:
                pass

    def ingest_voice_task(
        self,
        audio_data: bytes | str | Path,
        hwnd: Optional[int] = None,
        mode: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Transcribe audio and run it as a goal through the same planner path as /task."""
        from servo.daemon.tasks import run_goal, validate_target_hwnd

        # Validate the target before spending time on transcription.
        validate_target_hwnd(hwnd)
        transcribed_goal = self.transcribe_audio(audio_data)
        result = run_goal(goal=transcribed_goal, hwnd=hwnd, mode=mode)
        result["transcription"] = transcribed_goal
        return result


voice_manager = VoiceIngestionManager()
