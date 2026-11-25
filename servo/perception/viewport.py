"""Viewport transforms, DPI v2 coordinate mapping, and HWND client bounds calculation.

"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
from typing import Tuple


@dataclass
class WindowBounds:
    """Represents the physical screen coordinates of a target window and its client area."""
    hwnd: int
    left: int
    top: int
    right: int
    bottom: int
    client_left: int
    client_top: int
    client_right: int
    client_bottom: int

    @property
    def width(self) -> int:
        return max(1, self.right - self.left)

    @property
    def height(self) -> int:
        return max(1, self.bottom - self.top)

    @property
    def client_width(self) -> int:
        return max(1, self.client_right - self.client_left)

    @property
    def client_height(self) -> int:
        return max(1, self.client_bottom - self.client_top)


class ViewportTransform:
    """Handles mapping between physical screen coordinates and normalized 0-1000 grid coordinates."""

    def __init__(self, hwnd: int) -> None:
        self.hwnd = hwnd
        self.bounds = self.get_window_bounds(hwnd)

    @staticmethod
    def get_window_bounds(hwnd: int) -> WindowBounds:
        """Query physical screen coordinates of window and its client rect via Win32."""
        rect = wintypes.RECT()
        ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))

        # Client rect is in local coordinates (0, 0, width, height)
        client_rect = wintypes.RECT()
        ctypes.windll.user32.GetClientRect(hwnd, ctypes.byref(client_rect))

        # Convert client top-left to screen coordinates
        pt = wintypes.POINT(0, 0)
        ctypes.windll.user32.ClientToScreen(hwnd, ctypes.byref(pt))

        return WindowBounds(
            hwnd=hwnd,
            left=rect.left,
            top=rect.top,
            right=rect.right,
            bottom=rect.bottom,
            client_left=pt.x,
            client_top=pt.y,
            client_right=pt.x + client_rect.right,
            client_bottom=pt.y + client_rect.bottom,
        )

    def refresh(self) -> None:
        """Update window bounds in case window moved or resized."""
        self.bounds = self.get_window_bounds(self.hwnd)

    def screen_to_norm(self, screen_x: int, screen_y: int) -> Tuple[int, int]:
        """Convert physical screen pixels to normalized 0-1000 client-relative coordinates."""
        b = self.bounds
        norm_x = int(((screen_x - b.client_left) / b.client_width) * 1000)
        norm_y = int(((screen_y - b.client_top) / b.client_height) * 1000)
        return max(0, min(1000, norm_x)), max(0, min(1000, norm_y))

    def bbox_screen_to_norm(self, bbox: Tuple[int, int, int, int]) -> Tuple[int, int, int, int]:
        """Convert [left, top, right, bottom] screen bbox to normalized 0-1000 bbox."""
        x1, y1 = self.screen_to_norm(bbox[0], bbox[1])
        x2, y2 = self.screen_to_norm(bbox[2], bbox[3])
        return x1, y1, x2, y2

    def is_point_inside_client(self, screen_x: int, screen_y: int) -> bool:
        """Verify if physical screen point lies strictly inside client area (Sandboxing)."""
        b = self.bounds
        return (
            b.client_left <= screen_x <= b.client_right
            and b.client_top <= screen_y <= b.client_bottom
        )
