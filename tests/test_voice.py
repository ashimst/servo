"""Unit tests for Cactus Whistle voice ingestion pipeline."""

import sys
import types

import pytest

from servo.daemon.voice import VoiceIngestionManager, VoiceUnavailableError


def test_no_synthetic_transcription_without_whistle(monkeypatch):
    # Simulate cactus not being installed.
    monkeypatch.setitem(sys.modules, "cactus", None)
    mgr = VoiceIngestionManager()
    with pytest.raises(VoiceUnavailableError):
        mgr.transcribe_audio(b"RIFF\x24\x00\x00\x00WAVEfmt \x10\x00\x00\x00")
    with pytest.raises(VoiceUnavailableError):
        mgr.transcribe_audio("open_calculator_app.wav")


def test_transcription_uses_whistle_when_available(monkeypatch):
    fake = types.SimpleNamespace(transcribe=lambda path: {"text": " click Save "})
    monkeypatch.setitem(sys.modules, "cactus", fake)
    assert VoiceIngestionManager().transcribe_audio(b"RIFF") == "click Save"


def test_voice_task_requires_explicit_hwnd(monkeypatch):
    from servo.daemon.tasks import TaskRequestError
    with pytest.raises(TaskRequestError):
        VoiceIngestionManager().ingest_voice_task(b"RIFF", hwnd=None)
