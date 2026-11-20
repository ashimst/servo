"""Shared test configuration."""

import pytest


@pytest.fixture(autouse=True)
def _never_show_real_dialogs(monkeypatch):
    """Unit tests must never pop a real confirmation dialog on the developer's desktop."""
    from servo.config import settings
    monkeypatch.setattr(settings, "confirmation_channel", "deny")
