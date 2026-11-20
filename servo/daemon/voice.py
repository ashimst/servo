"""Cactus Whistle (16.9 MB) Voice Ingestion Pipeline.

See ADR-13 in SERVO_ARCHITECTURE_V2.md and SERVO_HARDENING_NOTES.md.
Transcribes local audio clips and dispatches voice commands into the Servo reflex arc.
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


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
        """Transcribe an audio clip using Cactus Whistle.

        There is no synthetic fallback. The previous version returned a hard-coded
        command ("Click search settings and type bluetooth") or a phrase built from the
        file name when Whistle was missing, and that invented text was then executed
        on the user's desktop.
        """
        try:
            import cactus  # type: ignore
        except ImportError as e:
            raise VoiceUnavailableError("Cactus Whistle is not installed (pip install servo[cactus]).") from e
        if not hasattr(cactus, "transcribe"):
            raise VoiceUnavailableError("Installed cactus package has no 'transcribe' function.")

        try:
            if isinstance(audio_bytes_or_path, (bytes, bytearray)):
                with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
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
        except VoiceUnavailableError:
            raise
        except Exception as e:
            raise VoiceUnavailableError(f"Transcription failed: {e}") from e

        if not text:
            raise VoiceUnavailableError("Transcription was empty.")
        return text

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
