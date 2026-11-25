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


def test_faster_whisper_is_not_a_stand_in(monkeypatch):
    # Even with faster-whisper importable, no Whistle means no transcription.
    calls = []
    fake_fw = types.SimpleNamespace(WhisperModel=lambda *a, **k: calls.append(a) or None)
    monkeypatch.setitem(sys.modules, "faster_whisper", fake_fw)
    monkeypatch.setitem(sys.modules, "cactus", None)
    with pytest.raises(VoiceUnavailableError, match="Whistle"):
        VoiceIngestionManager().transcribe_audio(b"RIFF\x24\x00\x00\x00WAVEfmt \x10\x00\x00\x00")
    assert calls == []


def test_whistle_failure_is_reported_not_covered(monkeypatch):
    def boom(path):
        raise RuntimeError("model crashed")
    monkeypatch.setitem(sys.modules, "cactus", types.SimpleNamespace(transcribe=boom))
    with pytest.raises(VoiceUnavailableError, match="model crashed"):
        VoiceIngestionManager().transcribe_audio(b"RIFF")
