"""Tests for the Windows Context Engine and Desktop RAG."""

from servo.cognition.windows_context import (
    AppInfo,
    WindowsCapability,
    WindowsContextEngine,
    windows_context,
)


def test_windows_context_catalog_initialization():
    engine = WindowsContextEngine()
    engine.refresh_catalog()
    assert len(engine._app_catalog) > 5
    assert "notepad" in engine._app_catalog
    assert "calculator" in engine._app_catalog


def test_windows_context_is_installed():
    engine = WindowsContextEngine()
    installed, info = engine.is_installed("notepad")
    assert installed is True
    assert info is not None
    assert info.name.lower() in ("notepad", "notepad.exe")

    installed_fake, info_fake = engine.is_installed("completely_fake_app_xyz_999")
    assert installed_fake is False
    assert info_fake is None


def test_windows_context_retrieval_for_capabilities():
    engine = WindowsContextEngine()
    ctx_sound = engine.retrieve_context("adjust volume or sound")
    assert any(c.uri_or_command == "ms-settings:sound" for c in ctx_sound.matched_capabilities)

    prompt_text = ctx_sound.to_prompt_text()
    assert "[Windows Desktop Context]" in prompt_text
    assert "ms-settings:sound" in prompt_text


def test_windows_context_retrieval_for_uninstalled_app():
    engine = WindowsContextEngine()
    # Assuming spotify is either installed or not, test retrieval of web fallback
    ctx = engine.retrieve_context("open spotify and play songs")
    # If not installed, it must appear in uninstalled_apps with web fallback
    installed, _ = engine.is_installed("spotify")
    if not installed:
        assert any(u.name == "Spotify" for u in ctx.uninstalled_apps)
        assert any("open.spotify.com" in (u.web_fallback or "") for u in ctx.uninstalled_apps)
        text = ctx.to_prompt_text()
        assert "Spotify" in text
        assert "open.spotify.com" in text
