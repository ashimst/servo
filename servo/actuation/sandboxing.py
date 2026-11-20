"""Input Sandboxing and Safety Guardrails.

See ADR-07 in SERVO_ARCHITECTURE_V1.md.
Clamps coordinates to target HWND client rect and blocks destructive hotkeys.
"""

from __future__ import annotations

import ctypes
from typing import List, Tuple
from servo.perception.viewport import ViewportTransform


class SandboxViolationException(Exception):
    """Raised when an action attempts an out-of-bounds click or prohibited input."""
    pass


class FailsafeException(Exception):
    """Raised when the operator triggers an emergency interrupt (e.g. corner-slam or Esc)."""
    pass


# Prohibited destructive / OS-interfering key combinations
HOTKEY_BLACKLIST = {
    ("alt", "f4"),
    ("win", "l"),
    ("ctrl", "alt", "del"),
    ("alt", "tab"),
    ("win", "d"),
    ("ctrl", "shift", "esc"),
}


def is_hotkey_blacklisted(keys: List[str]) -> bool:
    """Check if key combination matches the prohibited blacklist."""
    normalized = tuple(sorted(k.strip().lower() for k in keys))
    for blacklisted in HOTKEY_BLACKLIST:
        if normalized == tuple(sorted(blacklisted)):
            return True
    return False


class InputSandbox:
    """Enforces target window boundary clamping and hotkey restrictions."""

    def __init__(self, hwnd: int) -> None:
        self.hwnd = hwnd
        self.viewport = ViewportTransform(hwnd)

    def validate_and_clamp_target(self, screen_x: int, screen_y: int) -> Tuple[int, int]:
        """Validate and clamp target pixel coordinates strictly within client area."""
        self.viewport.refresh()
        b = self.viewport.bounds

        if b.client_width <= 0 or b.client_height <= 0:
            raise SandboxViolationException(f"Target window HWND {self.hwnd} has invalid zero client dimensions.")

        # Margin of 2 pixels from edge to avoid dragging outside
        margin = 2
        clamped_x = max(b.client_left + margin, min(b.client_right - margin, screen_x))
        clamped_y = max(b.client_top + margin, min(b.client_bottom - margin, screen_y))

        return clamped_x, clamped_y

    def validate_hotkey(self, keys: List[str]) -> None:
        """Validate that proposed hotkey does not violate the safety blacklist."""
        if is_hotkey_blacklisted(keys):
            raise SandboxViolationException(f"Hotkey combination {keys} is blacklisted by Servo safety policy.")

    def ensure_target_in_foreground(self) -> None:
        """Ensure the target window is active and in foreground before keyboard inputs."""
        user32 = ctypes.windll.user32
        active_hwnd = user32.GetForegroundWindow()
        if active_hwnd != self.hwnd:
            user32.SetForegroundWindow(self.hwnd)
