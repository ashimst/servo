"""Cactus Whistle (16.9 MB) Voice Ingestion Pipeline.

See ADR-13 in SERVO_ARCHITECTURE_V2.md and SERVO_HARDENING_NOTES.md.
Transcribes local audio clips and dispatches voice commands into the Servo reflex arc.
"""

from __future__ import annotations

import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, Optional

from servo.config import settings

logger = logging.getLogger(__name__)

_whisper_lock = threading.Lock()
_cached_whisper_model = None


def _get_whisper_model():
    global _cached_whisper_model
    if _cached_whisper_model is None:
        with _whisper_lock:
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


class VoiceIngestionManager:
    """Manages audio transcription and voice task dispatch."""

    def transcribe_audio(self, audio_bytes_or_path: bytes | str | Path) -> str:
        """Transcribe an audio clip using Cactus Whistle, Whisper, or configured ASR."""
        suffix = ".wav"
        if isinstance(audio_bytes_or_path, (bytes, bytearray)):
            b = bytes(audio_bytes_or_path[:16])
            if b.startswith(b"\x1a\x45\xdf\xa3"):
                suffix = ".webm"
            elif b.startswith(b"OggS"):
                suffix = ".ogg"
            elif b.startswith(b"RIFF"):
                suffix = ".wav"
            elif b.startswith(b"ID3") or (len(b) >= 2 and b[0] == 0xff and (b[1] & 0xe0) == 0xe0):
                suffix = ".mp3"

        # 1. Try Cactus Whistle if installed
        try:
            import cactus  # type: ignore
            if hasattr(cactus, "transcribe"):
                if isinstance(audio_bytes_or_path, (bytes, bytearray)):
                    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                        tmp.write(audio_bytes_or_path)
                        tmp_path = tmp.name
                    try:
                        text = _extract_text(cactus.transcribe(tmp_path))
                    finally:
                        try:
                            os.remove(tmp_path)
                        except OSError:
                            pass
                else:
                    path = Path(audio_bytes_or_path)
                    if not path.is_file():
                        raise VoiceUnavailableError(f"Audio file not found: {path}")
                    text = _extract_text(cactus.transcribe(str(path)))
                if not text:
                    raise VoiceUnavailableError("Transcription was empty.")
                return text
        except VoiceUnavailableError:
            raise
        except (ImportError, AttributeError):
            pass
        except Exception as e:
            logger.warning("Cactus transcription failed: %s", e)

        # 2. Try faster-whisper if installed
        try:
            model = _get_whisper_model()
            if isinstance(audio_bytes_or_path, (bytes, bytearray)):
                with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                    tmp.write(audio_bytes_or_path)
                    tmp_path = tmp.name
                try:
                    segments, _ = model.transcribe(tmp_path, beam_size=1)
                    text = " ".join(s.text for s in segments).strip()
                finally:
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass
            else:
                segments, _ = model.transcribe(str(audio_bytes_or_path), beam_size=1)
                text = " ".join(s.text for s in segments).strip()
            if text:
                return text
        except Exception as e:
            logger.debug("faster-whisper transcription error: %s", e)

        # 3. Try standard whisper if installed
        try:
            import whisper  # type: ignore
            model = whisper.load_model("tiny.en")
            if isinstance(audio_bytes_or_path, (bytes, bytearray)):
                with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                    tmp.write(audio_bytes_or_path)
                    tmp_path = tmp.name
                try:
                    res = model.transcribe(tmp_path)
                    text = res.get("text", "").strip()
                finally:
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass
            else:
                res = model.transcribe(str(audio_bytes_or_path))
                text = res.get("text", "").strip()
            if text:
                return text
        except Exception:
            pass

        # 4. Try OpenAI Audio API if API key is configured
        if getattr(settings, "openai_api_key", ""):
            try:
                import httpx
                if isinstance(audio_bytes_or_path, (bytes, bytearray)):
                    raw_bytes = bytes(audio_bytes_or_path)
                else:
                    raw_bytes = Path(audio_bytes_or_path).read_bytes()
                files = {"file": ("audio" + suffix, raw_bytes, "audio/octet-stream")}
                data = {"model": "whisper-1"}
                first_key = settings.openai_api_key.split(",")[0].strip()
                headers = {"Authorization": f"Bearer {first_key}"}
                url = "https://api.openai.com/v1/audio/transcriptions"
                with httpx.Client(timeout=30.0) as client:
                    r = client.post(url, headers=headers, data=data, files=files)
                if r.status_code == 200:
                    text = r.json().get("text", "").strip()
                    if text:
                        return text
            except Exception as e:
                logger.warning("OpenAI whisper transcription failed: %s", e)

        raise VoiceUnavailableError(
            "Cactus Whistle is not installed (pip install servo[cactus]), "
            "and no local Whisper or OpenAI transcription backend is available."
        )

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
