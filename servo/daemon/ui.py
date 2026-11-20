"""Desktop UI, System Tray notifications, and Transparent HUD Overlay.

See ADR-14 in SERVO_ARCHITECTURE_V2.md.
Renders real-time bounding boxes and integer element tags over target windows.
"""

from __future__ import annotations

import html
import sys
import threading
from typing import Any, Dict, List, Optional
from servo.perception.scene_index import SceneIndex, SceneElement


class ServoHUDOverlay:
    """Manages rendering transparent bounding box overlays on top of the active window."""

    def __init__(self) -> None:
        self._overlay_active = False
        self._lock = threading.Lock()

    def generate_svg_overlay(self, scene: SceneIndex, highlight_id: Optional[int] = None) -> str:
        """Generate a lightweight SVG overlay representing detected elements."""
        w = int(scene.viewport.get("width", 800))
        h = int(scene.viewport.get("height", 600))
        lines = [f'<svg width="{w}" height="{h}" xmlns="http://www.w3.org/2000/svg">']
        lines.append('<style>.box { fill: none; stroke-width: 2; } .txt { font-family: sans-serif; font-size: 12px; fill: white; }</style>')

        for el in scene.elements.values():
            s_left, s_top, s_right, s_bottom = el.bbox_screen
            # Convert to local window viewport coords
            x = s_left - int(scene.viewport.get("x", 0))
            y = s_top - int(scene.viewport.get("y", 0))
            bw = max(2, s_right - s_left)
            bh = max(2, s_bottom - s_top)

            is_highlight = el.element_id == highlight_id
            stroke_color = "#10b981" if is_highlight else "#3b82f6"
            fill_color = "rgba(16, 185, 129, 0.2)" if is_highlight else "rgba(59, 130, 246, 0.15)"

            lines.append(
                f'<rect x="{x}" y="{y}" width="{bw}" height="{bh}" '
                f'stroke="{stroke_color}" fill="{fill_color}" class="box"/>'
            )
            # Tag label badge
            lines.append(
                f'<rect x="{x}" y="{max(0, y - 18)}" width="{min(120, bw)}" height="18" fill="{stroke_color}"/>'
            )
            # Labels come from arbitrary application/web content; escape them so a label
            # like "<script>..." cannot inject markup into the served SVG.
            label_text = html.escape(f"[{el.element_id}] {el.label[:12]}", quote=True)
            lines.append(f'<text x="{x + 4}" y="{max(12, y - 5)}" class="txt">{label_text}</text>')

        lines.append("</svg>")
        return "\n".join(lines)


class ServoTrayUI:
    """Provides a desktop tray notification and emergency pause toggle."""

    def __init__(self, on_pause: Optional[callable] = None, on_abort: Optional[callable] = None) -> None:
        self.on_pause = on_pause
        self.on_abort = on_abort
        self.hud = ServoHUDOverlay()

    def show_alert(self, title: str, message: str) -> None:
        """Display an OS notification message."""
        if sys.platform == "win32":
            try:
                import ctypes
                ctypes.windll.user32.MessageBoxW(0, message, title, 0x40 | 0x1)  # MB_ICONINFORMATION | MB_OKCANCEL
            except Exception:
                pass
        print(f"[{title}] {message}")


hud_overlay = ServoHUDOverlay()
tray_ui = ServoTrayUI()
