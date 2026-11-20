"""Unit tests for Input Sandboxing and Hotkey Blacklist enforcement."""

import pytest
from servo.actuation.sandboxing import (
    is_hotkey_blacklisted,
    InputSandbox,
    SandboxViolationException,
)
from servo.perception.viewport import WindowBounds


def test_hotkey_blacklist():
    assert is_hotkey_blacklisted(["alt", "f4"]) is True
    assert is_hotkey_blacklisted(["Alt", "Tab"]) is True
    assert is_hotkey_blacklisted(["WIN", "L"]) is True
    assert is_hotkey_blacklisted(["ctrl", "alt", "del"]) is True

    # Safe hotkeys
    assert is_hotkey_blacklisted(["ctrl", "c"]) is False
    assert is_hotkey_blacklisted(["ctrl", "v"]) is False
    assert is_hotkey_blacklisted(["ctrl", "s"]) is False
    assert is_hotkey_blacklisted(["enter"]) is False


def test_coordinate_clamping():
    sandbox = InputSandbox.__new__(InputSandbox)
    sandbox.hwnd = 1001

    class MockViewport:
        bounds = WindowBounds(
            hwnd=1001,
            left=100,
            top=100,
            right=600,
            bottom=500,
            client_left=110,
            client_top=130,
            client_right=590,
            client_bottom=490,
        )
        def refresh(self):
            pass

    sandbox.viewport = MockViewport()

    # In-bounds target: returns exact coordinate
    cx, cy = sandbox.validate_and_clamp_target(300, 300)
    assert cx == 300
    assert cy == 300

    # Out-of-bounds target (far left / top): clamps to client margin
    cx, cy = sandbox.validate_and_clamp_target(0, 0)
    assert cx == 112
    assert cy == 132

    # Out-of-bounds target (far right / bottom): clamps to client margin
    cx, cy = sandbox.validate_and_clamp_target(2000, 2000)
    assert cx == 588
    assert cy == 488
