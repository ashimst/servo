"""Unit tests for Input Sandboxing and Hotkey Blacklist enforcement."""

import pytest
from servo.actuation.needle_tools import build_hotkey_sequence, escape_send_keys
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


def test_hotkey_blacklist_cannot_be_bypassed_with_aliases_or_extra_keys():
    assert is_hotkey_blacklisted(["control", "alt", "delete"]) is True
    assert is_hotkey_blacklisted(["shift", "alt", "f4"]) is True
    assert is_hotkey_blacklisted(["windows", "r"]) is True
    assert is_hotkey_blacklisted(["cmd", "x"]) is True
    assert is_hotkey_blacklisted(["escape", "ctrl"]) is True


def test_send_keys_escaping_and_hotkey_mapping():
    assert escape_send_keys("a+b 50% (x)") == "a{+}b 50{%} {(}x{)}"
    assert escape_send_keys("{}") == "{{}{}}"
    assert build_hotkey_sequence(["ctrl", "shift", "s"]) == "^+s"
    assert build_hotkey_sequence(["Control", "F5"]) == "^{F5}"
    with pytest.raises(Exception):
        build_hotkey_sequence(["ctrl", "a", "b"])


def _sandbox():
    sandbox = InputSandbox.__new__(InputSandbox)
    sandbox.hwnd = 1001

    from servo.perception.viewport import ViewportTransform

    class MockViewport(ViewportTransform):
        def __init__(self):
            self.hwnd = 1001
            self.bounds = WindowBounds(
                hwnd=1001, left=100, top=100, right=600, bottom=500,
                client_left=110, client_top=130, client_right=590, client_bottom=490,
            )

        def refresh(self):
            pass

    sandbox.viewport = MockViewport()
    return sandbox


def test_in_bounds_target_is_returned_unchanged():
    assert _sandbox().validate_target(300, 300) == (300, 300)


def test_out_of_bounds_target_is_rejected_not_clamped():
    sandbox = _sandbox()
    with pytest.raises(SandboxViolationException):
        sandbox.validate_target(0, 0)
    with pytest.raises(SandboxViolationException):
        sandbox.validate_target(2000, 2000)
