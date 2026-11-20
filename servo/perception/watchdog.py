"""Win32 Event Watchdog listening for window focus and creation events.

See Section 2.3 in SERVO_ARCHITECTURE_V1.md.
"""

from __future__ import annotations

import sys
import threading
import time
from typing import Callable, Optional


class WindowEventWatchdog:
    """Listens for active window focus and creation events via Win32 SetWinEventHook."""

    def __init__(self, callback: Optional[Callable[[int, str], None]] = None) -> None:
        self.callback = callback
        self._thread: Optional[threading.Thread] = None
        self._running = False

    def start(self) -> None:
        """Start listening for window events on background thread."""
        if self._running or sys.platform != "win32":
            return
        self._running = True
        self._thread = threading.Thread(target=self._hook_loop, name="ServoWatchdog", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop watchdog."""
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None

    def _hook_loop(self) -> None:
        """Poll foreground window changes."""
        import ctypes
        user32 = ctypes.windll.user32
        last_hwnd = 0

        while self._running:
            try:
                cur_hwnd = user32.GetForegroundWindow()
                if cur_hwnd != last_hwnd and cur_hwnd != 0:
                    last_hwnd = cur_hwnd
                    if self.callback is not None:
                        # Query window title
                        buf = ctypes.create_unicode_buffer(512)
                        user32.GetWindowTextW(cur_hwnd, buf, 512)
                        title = buf.value
                        self.callback(cur_hwnd, title)
            except Exception:
                pass
            time.sleep(0.2)
