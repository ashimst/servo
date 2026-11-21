"""Shared test configuration."""

import os

# Tests must not pick up the developer's saved copilot setup (~/.servo/settings.json).
os.environ["SERVO_NO_USER_SETTINGS"] = "1"

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _never_show_real_dialogs(monkeypatch):
    """Unit tests must never pop a real confirmation dialog on the developer's desktop."""
    from servo.config import settings
    monkeypatch.setattr(settings, "confirmation_channel", "deny")
