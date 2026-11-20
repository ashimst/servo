"""Unit tests for the desktop confirmation channel (Win32 dialog is stubbed)."""

import pytest

from servo.control import confirmation as c


def _req(**kw):
    base = dict(
        sub_goal="Send the message", tool_call="click(element_id=4)", element_label="Send",
        risk_level="high", reason="High risk keyword detected: 'Send'", hwnd=42,
        window_title="Chat - Inbox", process_name="chat.exe",
    )
    base.update(kw)
    return c.ConfirmationRequest(**base)


@pytest.fixture
def windows_platform(monkeypatch):
    monkeypatch.setattr(c.sys, "platform", "win32")


@pytest.mark.parametrize(
    "code, expected",
    [(c.IDYES, True), (7, False), (c.MB_TIMEDOUT, False), (0, False)],  # 7 = IDNO, 0 = failed to show
)
def test_dialog_result_mapping(monkeypatch, windows_platform, code, expected):
    dlg = c.DesktopDialogConfirmation(timeout_seconds=1)
    monkeypatch.setattr(dlg, "_show", lambda text, caption: code)
    assert dlg(_req()) is expected


def test_dialog_exception_rejects(monkeypatch, windows_platform):
    dlg = c.DesktopDialogConfirmation()

    def fail(text, caption):
        raise OSError("no desktop")

    monkeypatch.setattr(dlg, "_show", fail)
    assert dlg(_req()) is False


def test_non_windows_rejects(monkeypatch):
    monkeypatch.setattr(c.sys, "platform", "linux")
    assert c.DesktopDialogConfirmation()(_req()) is False


def test_message_shows_concrete_action_and_target():
    msg = c.format_confirmation_message(_req(tool_call="type_text(element_id=1, text='rm -rf\n')"))
    assert "HIGH-risk" in msg
    assert "type_text(element_id=1, text='rm -rf" in msg
    assert "Chat - Inbox  (chat.exe)" in msg
    assert "High risk keyword detected" in msg


def test_long_fields_are_clipped():
    msg = c.format_confirmation_message(_req(sub_goal="x" * 5000))
    assert len(msg) < 2000


def test_dialog_flags_default_to_no(monkeypatch, windows_platform):
    captured = {}

    class FakeFn:
        argtypes = None
        restype = None

        def __call__(self, owner, text, caption, flags, lang, ms):
            captured.update(flags=flags, ms=ms)
            return c.IDYES

    class FakeUser32:
        MessageBoxTimeoutW = FakeFn()

    class FakeWindll:
        user32 = FakeUser32()

    monkeypatch.setattr(c.ctypes, "windll", FakeWindll(), raising=False)
    assert c.DesktopDialogConfirmation(timeout_seconds=2)(_req()) is True
    assert captured["flags"] & c.MB_DEFBUTTON2  # "No" is the default button
    assert captured["flags"] & c.MB_TOPMOST
    assert captured["ms"] == 2000


def test_build_callback_from_settings():
    assert isinstance(c.build_confirmation_callback("dialog"), c.DesktopDialogConfirmation)
    assert c.build_confirmation_callback("deny") is c.deny_all
    with pytest.raises(ValueError):
        c.build_confirmation_callback("nope")
