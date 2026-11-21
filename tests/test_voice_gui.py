"""Unit tests for ASR Voice integration into the Copilot GUI and REST API."""

import base64
import pytest
from unittest.mock import patch, MagicMock

import httpx
from servo.daemon.copilot_app import CopilotSession, create_app, UI_FILE
from servo.daemon.voice import VoiceUnavailableError


def test_gui_html_contains_voice_elements():
    """Verify that index.html contains voice ASR button, wave indicators, and keyboard shortcut."""
    html = UI_FILE.read_text(encoding="utf-8")
    assert 'id="voiceBtn"' in html
    assert 'class="voice-btn"' in html
    assert 'id="voiceWave"' in html
    assert 'Voice Input (ASR)' in html
    assert 'Alt+V' in html
    assert 'MediaRecorder' in html
    assert '/api/voice/transcribe' in html


def test_voice_status_endpoint():
    """Verify /api/voice/status endpoint returns available local ASR backend."""
    session = CopilotSession()
    app = create_app(session)

    from fastapi.testclient import TestClient
    client = TestClient(app, base_url="http://127.0.0.1")

    # Missing token -> 403
    r = client.get("/api/voice/status")
    assert r.status_code == 403

    # Valid token -> 200
    r = client.get("/api/voice/status", headers={"X-Servo-Token": session.token})
    assert r.status_code == 200
    data = r.json()
    assert data.get("local") is True
    assert "backend" in data
    assert "available" in data


def test_voice_transcribe_endpoint_success():
    """Verify /api/voice/transcribe decodes audio, calls voice_manager, and returns text."""
    session = CopilotSession()
    app = create_app(session)

    from fastapi.testclient import TestClient
    client = TestClient(app, base_url="http://127.0.0.1")

    fake_audio_bytes = b"RIFFfake_audio_bytes_123"
    fake_b64 = base64.b64encode(fake_audio_bytes).decode("ascii")

    with patch("servo.daemon.voice.voice_manager.transcribe_audio", return_value="open notepad and write hello"):
        r = client.post(
            "/api/voice/transcribe",
            headers={"X-Servo-Token": session.token},
            json={"audio_b64": fake_b64, "auto_send": False},
        )
        assert r.status_code == 200
        data = r.json()
        assert data["text"] == "open notepad and write hello"
        assert data["status"] == "ok"
        assert data["run_id"] is None


def test_voice_transcribe_endpoint_auto_send():
    """Verify /api/voice/transcribe with auto_send=True dispatches task run_id."""
    session = CopilotSession()
    app = create_app(session)

    from fastapi.testclient import TestClient
    client = TestClient(app, base_url="http://127.0.0.1")

    fake_b64 = base64.b64encode(b"RIFFtest").decode("ascii")

    with patch("servo.daemon.voice.voice_manager.transcribe_audio", return_value="open calculator"), \
         patch.object(session, "send", return_value="run-voice-123") as mock_send:
        r = client.post(
            "/api/voice/transcribe",
            headers={"X-Servo-Token": session.token},
            json={"audio_b64": fake_b64, "auto_send": True},
        )
        assert r.status_code == 200
        data = r.json()
        assert data["text"] == "open calculator"
        assert data["run_id"] == "run-voice-123"
        mock_send.assert_called_once_with("open calculator")


def test_voice_transcribe_endpoint_handles_unavailable_error():
    """Verify /api/voice/transcribe returns 503 when backend is unavailable."""
    session = CopilotSession()
    app = create_app(session)

    from fastapi.testclient import TestClient
    client = TestClient(app, base_url="http://127.0.0.1")

    fake_b64 = base64.b64encode(b"RIFFtest").decode("ascii")

    with patch("servo.daemon.voice.voice_manager.transcribe_audio", side_effect=VoiceUnavailableError("No Whistle")):
        r = client.post(
            "/api/voice/transcribe",
            headers={"X-Servo-Token": session.token},
            json={"audio_b64": fake_b64, "auto_send": False},
        )
        assert r.status_code == 503
        assert "No Whistle" in r.json()["detail"]


def test_voice_transcribe_endpoint_rejects_invalid_audio():
    """Verify /api/voice/transcribe rejects empty or corrupt base64."""
    session = CopilotSession()
    app = create_app(session)

    from fastapi.testclient import TestClient
    client = TestClient(app, base_url="http://127.0.0.1")

    # Empty string
    r = client.post(
        "/api/voice/transcribe",
        headers={"X-Servo-Token": session.token},
        json={"audio_b64": "", "auto_send": False},
    )
    assert r.status_code == 400

    # Corrupt base64
    r = client.post(
        "/api/voice/transcribe",
        headers={"X-Servo-Token": session.token},
        json={"audio_b64": "!!!not_base64!!!", "auto_send": False},
    )
    assert r.status_code == 400


def test_voice_transcribe_endpoint_handles_empty_speech_gracefully():
    """Verify /api/voice/transcribe returns text='' without 503 when silence is recorded."""
    session = CopilotSession()
    app = create_app(session)

    from fastapi.testclient import TestClient
    client = TestClient(app, base_url="http://127.0.0.1")

    fake_b64 = base64.b64encode(b"RIFFsilence").decode("ascii")

    with patch("servo.daemon.voice.voice_manager.transcribe_audio", return_value=""):
        r = client.post(
            "/api/voice/transcribe",
            headers={"X-Servo-Token": session.token},
            json={"audio_b64": fake_b64, "auto_send": False},
        )
        assert r.status_code == 200
        data = r.json()
        assert data["text"] == ""
        assert data["status"] == "ok"
        assert data["run_id"] is None

