"""Lightweight System Tray & HUD Overlay launcher.

See Section 6 in SERVO_ARCHITECTURE_V1.md.
"""

from __future__ import annotations

import sys
from typing import Optional


class ServoTrayUI:
    """Provides a desktop tray notification and emergency pause toggle."""

    def __init__(self, on_pause: Optional[callable] = None, on_abort: Optional[callable] = None) -> None:
        self.on_pause = on_pause
        self.on_abort = on_abort

    def show_alert(self, title: str, message: str) -> None:
        """Display an OS notification message."""
        if sys.platform == "win32":
            try:
                import ctypes
                ctypes.windll.user32.MessageBoxW(0, message, title, 0x40 | 0x1)  # MB_ICONINFORMATION | MB_OKCANCEL
            except Exception:
                pass
        print(f"[{title}] {message}")
