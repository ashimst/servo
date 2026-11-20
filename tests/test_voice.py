"""Unit tests for Cactus Whistle voice ingestion pipeline."""

from servo.daemon.voice import VoiceIngestionManager


def test_voice_transcribe_synthetic_fallback():
    mgr = VoiceIngestionManager()

    # Test with dummy audio bytes
    text = mgr.transcribe_audio(b"RIFF\x24\x00\x00\x00WAVEfmt \x10\x00\x00\x00")
    assert isinstance(text, str)
    assert len(text) > 0

    # Test with audio file path
    text_from_path = mgr.transcribe_audio("open_calculator_app.wav")
    assert "open calculator app" in text_from_path.lower()
