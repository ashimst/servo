"""Shared test configuration.

Everything here runs before any servo module is imported, so module-level paths
(``Path.home() / ".servo" / ...``) already point at the sandbox.
"""

import os
import tempfile

# A private Servo data folder for the whole test session. Without it, tests wrote fake
# runs into the developer's real ~/.servo/traces, chat memory and graph.json. (Only
# SERVO_HOME: overriding USERPROFILE also moved the home of browsers Playwright starts.)
os.environ["SERVO_HOME"] = tempfile.mkdtemp(prefix="servo-test-home-")

# Tests must not pick up the developer's saved copilot setup (~/.servo/settings.json).
os.environ["SERVO_NO_USER_SETTINGS"] = "1"
# Web steps must never drive the developer's real browser (user mode types into it).
os.environ["SERVO_BROWSER_MODE"] = "playwright"
os.environ.pop("SERVO_PREFERRED_BROWSER", None)

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _never_show_real_dialogs(monkeypatch):
    """Unit tests must never pop a real confirmation dialog on the developer's desktop."""
    from servo.config import settings
    monkeypatch.setattr(settings, "confirmation_channel", "deny")


@pytest.fixture(autouse=True)
def _no_real_start_menu(monkeypatch):
    """The Start-menu app list (Get-StartApps) is this machine's; tests use a fixed one."""
    from servo.perception import app_identity
    monkeypatch.setattr(app_identity, "start_apps", lambda refresh=False: [])


@pytest.fixture(autouse=True)
def _never_move_the_real_copilot(monkeypatch):
    """Tests must not find (and move aside) a copilot open on the developer's desktop."""
    from servo.actuation import overlay
    monkeypatch.setattr(overlay, "own_windows", lambda: set())
