"""Input Sandboxing and Safety Guardrails.

See ADR-07 in SERVO_ARCHITECTURE_V1.md and SERVO_HARDENING_NOTES.md.

The sandbox *rejects* input that would land outside the target window instead of
clamping it: clamping silently turns "click element X" into "click whatever is at
the edge of the window", which is a different action than the one that was gated.
"""

from __future__ import annotations

import ctypes
import sys
import time
from typing import Iterable, List, Tuple

from servo.perception import windows
from servo.perception.viewport import ViewportTransform


class SandboxViolationException(Exception):
    """Raised when an action attempts an out-of-bounds click or prohibited input."""


class FailsafeException(Exception):
    """Raised when the operator triggers an emergency interrupt (e.g. corner-slam or Esc)."""


# Canonical key names. Anything not listed maps to itself (lowercased).
KEY_ALIASES = {
    "control": "ctrl",
    "ctl": "ctrl",
    "lctrl": "ctrl",
    "rctrl": "ctrl",
    "menu": "alt",
    "lalt": "alt",
    "ralt": "alt",
    "altgr": "alt",
    "option": "alt",
    "lshift": "shift",
    "rshift": "shift",
    "windows": "win",
    "lwin": "win",
    "rwin": "win",
    "meta": "win",
    "super": "win",
    "cmd": "win",
    "command": "win",
    "delete": "del",
    "escape": "esc",
    "return": "enter",
}

# A combo is blocked if it CONTAINS any of these sets (so adding shift to alt+f4
# does not get around the rule). Any combo involving the Windows key is blocked
# because every Win shortcut acts on the OS shell, never on the target window
# (Win+R alone is an arbitrary-command launcher).
HOTKEY_BLACKLIST: Tuple[frozenset, ...] = (
    frozenset({"win"}),
    frozenset({"alt", "f4"}),
    frozenset({"alt", "tab"}),
    frozenset({"alt", "esc"}),
    frozenset({"ctrl", "esc"}),
    frozenset({"ctrl", "alt", "del"}),
    frozenset({"ctrl", "shift", "esc"}),
)


def normalize_keys(keys: Iterable[str]) -> List[str]:
    out = []
    for k in keys:
        lk = k.strip().lower()
        out.append(KEY_ALIASES.get(lk, lk))
    return out


def is_hotkey_blacklisted(keys: List[str]) -> bool:
    """Check if key combination contains any prohibited combination."""
    combo = set(normalize_keys(keys))
    return any(blocked <= combo for blocked in HOTKEY_BLACKLIST)


def check_target_allowed(hwnd: int, mode: str) -> None:
    """Enforce harness-mode allowlists (spec: 'strictly sandboxed to a whitelisted HWND
    or process name'). Fails closed: harness mode with no allowlist configured refuses
    to act, because 'no allowlist' would otherwise mean 'every window on the desktop'.
    """
    if mode != "harness":
        return
    from servo.config import settings

    hwnds = set(settings.whitelisted_hwnds)
    procs = {p.lower() for p in settings.whitelisted_process_names}
    if not hwnds and not procs:
        raise SandboxViolationException(
            "Harness mode requires SERVO_WHITELISTED_HWNDS or SERVO_WHITELISTED_PROCESS_NAMES to be set."
        )
    if hwnd in hwnds:
        return
    proc = windows.get_process_name(hwnd)
    if proc and proc in procs:
        return
    raise SandboxViolationException(
        f"HWND {hwnd} (process '{proc or 'unknown'}') is not on the harness allowlist."
    )


class InputSandbox:
    """Enforces target window boundary checks and hotkey restrictions."""

    def __init__(self, hwnd: int) -> None:
        self.hwnd = hwnd
        self.viewport = ViewportTransform(hwnd)

    def validate_target(self, screen_x: int, screen_y: int) -> Tuple[int, int]:
        """Validate that the target point lies inside the client area; raise otherwise."""
        self.viewport.refresh()
        b = self.viewport.bounds

        if b.client_right - b.client_left <= 0 or b.client_bottom - b.client_top <= 0:
            raise SandboxViolationException(f"Target window HWND {self.hwnd} has invalid zero client dimensions.")

        if not self.viewport.is_point_inside_client(screen_x, screen_y):
            raise SandboxViolationException(
                f"Target point ({screen_x}, {screen_y}) is outside the client area of HWND {self.hwnd}; "
                "element is likely scrolled out of view or the window moved."
            )
        return screen_x, screen_y

    def point_hits_target(self, screen_x: int, screen_y: int) -> bool:
        """True if a physical click at this point would be delivered to the target window
        (i.e. no other window is covering it)."""
        if sys.platform != "win32":
            return True
        return windows.window_at_point(screen_x, screen_y) == windows.get_root_window(self.hwnd)

    def validate_hotkey(self, keys: List[str]) -> None:
        """Validate that proposed hotkey does not violate the safety blacklist."""
        if is_hotkey_blacklisted(keys):
            raise SandboxViolationException(f"Hotkey combination {keys} is blacklisted by Servo safety policy.")

    def ensure_target_in_foreground(self) -> None:
        """Bring the target to the foreground and VERIFY it got there.

        Windows frequently refuses SetForegroundWindow (foreground lock). Synthetic
        keystrokes always go to the foreground window, so if activation failed we
        must not type: the keys would land in some other application.
        """
        if sys.platform != "win32":
            return
        target = windows.get_root_window(self.hwnd)
        if windows.get_foreground_window() == target:
            return
        ctypes.windll.user32.SetForegroundWindow(target)
        for _ in range(10):
            if windows.get_foreground_window() == target:
                return
            time.sleep(0.02)
        raise SandboxViolationException(
            f"Could not bring HWND {self.hwnd} to the foreground; refusing to send keystrokes to another window."
        )
