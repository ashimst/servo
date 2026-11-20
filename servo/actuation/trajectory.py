"""Cubic Ease-Out Kinematics and Corner-Slam Failsafe.

See ADR-07 and Section 5.2 in SERVO_ARCHITECTURE_V1.md.
Handles both interactive desktop sessions and non-interactive / CI sessions gracefully.
"""

from __future__ import annotations

import math
import sys
import time
from typing import Tuple
from servo.actuation.sandboxing import FailsafeException


class CursorTrajectory:
    """Drives human-like cursor movements along cubic ease-out paths with corner-slam failsafe."""

    @staticmethod
    def get_cursor_pos() -> Tuple[int, int]:
        """Read live physical cursor coordinates via Win32 with fallback for background sessions."""
        if sys.platform != "win32":
            return 500, 500
        try:
            import ctypes
            from ctypes import wintypes
            pt = wintypes.POINT()
            if ctypes.windll.user32.GetCursorPos(ctypes.byref(pt)):
                return pt.x, pt.y
        except Exception:
            pass
        return 500, 500

    @staticmethod
    def set_cursor_pos(x: int, y: int) -> bool:
        """Directly update cursor position. Returns False if session has no interactive display."""
        if sys.platform == "win32":
            try:
                import ctypes
                return bool(ctypes.windll.user32.SetCursorPos(x, y))
            except Exception:
                return False
        return True

    @classmethod
    def check_failsafe(cls, start_pos: Tuple[int, int] | None = None) -> None:
        """Check if physical cursor has been slammed to top-left corner (0, 0) during movement to abort."""
        try:
            cur_x, cur_y = cls.get_cursor_pos()
            # Only trigger failsafe if cursor moved into (0, 0) from an active starting position
            if cur_x <= 2 and cur_y <= 2:
                if start_pos is not None and (start_pos[0] > 10 or start_pos[1] > 10):
                    raise FailsafeException("Emergency Failsafe Triggered: Operator slammed cursor to corner (0, 0).")
        except FailsafeException:
            raise
        except Exception:
            pass

    @classmethod
    def move_to(cls, target_x: int, target_y: int, fast_mode: bool = False, enable_failsafe: bool = True) -> None:
        """Move cursor from current position to target along cubic ease-out trajectory."""
        start_x, start_y = cls.get_cursor_pos()

        # In fast_mode or non-windows, snap directly
        if fast_mode or sys.platform != "win32":
            cls.set_cursor_pos(target_x, target_y)
            return

        if enable_failsafe:
            cls.check_failsafe(start_pos=(start_x, start_y))

        dx = target_x - start_x
        dy = target_y - start_y
        dist = math.hypot(dx, dy)

        # Interpolate between 10 and 35 steps based on distance
        steps = max(10, min(35, int(dist / 25)))
        sleep_interval = 0.005  # 5ms

        for step in range(1, steps + 1):
            t = step / float(steps)
            ease_t = 1.0 - math.pow(1.0 - t, 3)

            current_x = int(start_x + dx * ease_t)
            current_y = int(start_y + dy * ease_t)

            if enable_failsafe:
                cls.check_failsafe(start_pos=(start_x, start_y))

            cls.set_cursor_pos(current_x, current_y)
            time.sleep(sleep_interval)

        cls.set_cursor_pos(target_x, target_y)
