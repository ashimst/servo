"""Tests for the Windows Context Engine, Desktop RAG, and Mirage Native Windows OCR."""

import sys
import pytest
from PIL import Image, ImageDraw, ImageFont

from servo.cognition.needle import parse_grammar
from servo.cognition.windows_context import (
    AppInfo,
    WindowsCapability,
    WindowsContextEngine,
    windows_context,
)
from servo.perception.mirage_sensor import _run_windows_ocr, describe_image


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


def test_windows_context_web_fallback():
    engine = WindowsContextEngine()
    url_spotify = engine.get_web_fallback("spotify")
    assert url_spotify == "https://open.spotify.com"

    url_slack = engine.get_web_fallback("slack")
    assert url_slack == "https://app.slack.com"

    url_none = engine.get_web_fallback("non_existent_app_random")
    assert url_none is None


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


def test_needle_uninstalled_app_rerouting():
    # An app with a web fallback like Spotify reroutes to open_url if not installed
    installed, _ = windows_context.is_installed("spotify")
    if not installed:
        plan = parse_grammar("open spotify")
        assert plan is not None
        assert len(plan.steps) == 1
        assert plan.steps[0].tool_name == "open_url"
        assert "open.spotify.com" in plan.steps[0].arguments.get("url", "")

    # An unknown uninstalled app escalates to System 2 (returns None) rather than blind launch
    plan_fake = parse_grammar("open completely_fake_app_xyz_888")
    assert plan_fake is None


@pytest.mark.skipif(sys.platform != "win32", reason="Windows OCR requires Windows")
def test_mirage_native_windows_ocr():
    img = Image.new("RGB", (350, 100), color="white")
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype(r"C:\Windows\Fonts\arial.ttf", 24)
        d.text((20, 30), "Confirm Changes", fill="black", font=font)
    except Exception:
        d.text((20, 30), "Confirm Changes", fill="black")

    dets = _run_windows_ocr(img)
    assert dets is not None
    assert len(dets) > 0
    labels = [d.label.lower() for d in dets]
    assert any("confirm" in l or "changes" in l for l in labels)

    raw_desc = describe_image(img)
    assert raw_desc is not None
    assert "text" in raw_desc or "objects" in raw_desc
