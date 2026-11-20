"""Human confirmation channel for medium/high-risk actions in assistant mode.

See SERVO_HARDENING_NOTES.md §11.

The default channel is a native Windows dialog shown on the desktop being automated:
the person who can see the target window is the one who approves. Design rules:

* "No" is the default button. The dialog takes focus when it appears, so a user who
  is mid-typing in another app could otherwise approve with a stray Enter.
* No answer within ``confirmation_timeout_seconds`` counts as a rejection.
* Any failure to show the dialog (non-interactive session, service, non-Windows)
  counts as a rejection. The channel fails closed, like the rest of the gate.
* The dialog shows the risk reason, the exact tool call (including text to type),
  and the target window title and process, so the user approves a concrete action
  rather than a paraphrase.
"""

from __future__ import annotations

import ctypes
import logging
import sys
import threading
from typing import Callable, Literal, Optional

from pydantic import BaseModel

logger = logging.getLogger(__name__)


class ConfirmationRequest(BaseModel):
    """Everything a human needs to decide on one action."""
    sub_goal: str
    tool_call: str
    element_label: str
    risk_level: Literal["low", "medium", "high"]
    reason: str
    hwnd: int
    window_title: str = ""
    process_name: str = ""


ConfirmationCallback = Callable[[ConfirmationRequest], bool]

# MessageBox flags / return codes
MB_YESNO = 0x00000004
MB_ICONWARNING = 0x00000030
MB_DEFBUTTON2 = 0x00000100
MB_SETFOREGROUND = 0x00010000
MB_TOPMOST = 0x00040000
IDYES = 6
MB_TIMEDOUT = 32000

_MAX_FIELD = 300


def _clip(text: str, limit: int = _MAX_FIELD) -> str:
    text = text.replace("\r", " ").replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def format_confirmation_message(req: ConfirmationRequest) -> str:
    target = req.window_title or f"HWND {req.hwnd}"
    if req.process_name:
        target = f"{target}  ({req.process_name})"
    return (
        f"Servo wants to perform a {req.risk_level.upper()}-risk action.\n\n"
        f"Step:      {_clip(req.sub_goal)}\n"
        f"Action:    {_clip(req.tool_call)}\n"
        f"Element:   {_clip(req.element_label)}\n"
        f"Window:    {_clip(target)}\n"
        f"Why risky: {_clip(req.reason)}\n\n"
        f"Allow this action?"
    )


class DesktopDialogConfirmation:
    """Blocking Yes/No dialog on the interactive desktop."""

    def __init__(self, timeout_seconds: float = 60.0) -> None:
        self.timeout_seconds = timeout_seconds
        # One dialog at a time even if several loops share this channel.
        self._lock = threading.Lock()

    def _show(self, text: str, caption: str) -> int:
        flags = MB_YESNO | MB_ICONWARNING | MB_DEFBUTTON2 | MB_SETFOREGROUND | MB_TOPMOST
        user32 = ctypes.windll.user32
        timeout_ms = max(1, int(self.timeout_seconds * 1000))
        fn = getattr(user32, "MessageBoxTimeoutW", None)
        if fn is not None:
            # Undocumented but present in user32 since Windows XP; returns 32000 on timeout.
            fn.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p,
                           ctypes.c_uint, ctypes.c_ushort, ctypes.c_uint]
            fn.restype = ctypes.c_int
            return fn(None, text, caption, flags, 0, timeout_ms)
        logger.warning("MessageBoxTimeoutW unavailable; refusing rather than showing a dialog with no timeout.")
        return 0

    def __call__(self, req: ConfirmationRequest) -> bool:
        if sys.platform != "win32":
            logger.warning("Desktop confirmation is only available on Windows; rejecting.")
            return False
        with self._lock:
            try:
                result = self._show(format_confirmation_message(req), "Servo: confirm action")
            except Exception as e:
                logger.warning("Could not show confirmation dialog (%s); rejecting.", e)
                return False
        if result == MB_TIMEDOUT:
            logger.info("Confirmation timed out after %ss; rejecting.", self.timeout_seconds)
        elif result == 0:
            logger.warning("Confirmation dialog failed to display (no interactive desktop?); rejecting.")
        return result == IDYES


def deny_all(req: ConfirmationRequest) -> bool:
    """Channel for unattended deployments: every medium/high-risk action is refused."""
    return False


def build_confirmation_callback(channel: Optional[str] = None) -> Optional[ConfirmationCallback]:
    """Return the callback for SERVO_CONFIRMATION_CHANNEL ('dialog' or 'deny')."""
    from servo.config import settings

    name = channel or settings.confirmation_channel
    if name == "dialog":
        return DesktopDialogConfirmation(timeout_seconds=settings.confirmation_timeout_seconds)
    if name == "deny":
        return deny_all
    raise ValueError(f"Unknown confirmation channel '{name}'.")
