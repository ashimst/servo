"""UI Automation (UIA) sensor wrapping pywinauto.

Extracts semantic accessibility tree and visible interactive elements.
See ADR-01 and Section 2.1 in SERVO_ARCHITECTURE_V1.md.
"""

from __future__ import annotations

import logging
import sys
from typing import List, Optional
from servo.perception.viewport import ViewportTransform
from servo.perception.scene_index import SceneIndex, SceneElement

logger = logging.getLogger(__name__)

# Whitelisted interactive control roles for UIA filtering
INTERACTIVE_CONTROL_TYPES = {
    "Button",
    "Edit",
    "CheckBox",
    "RadioButton",
    "ComboBox",
    "ListItem",
    "MenuItem",
    "TabItem",
    "Hyperlink",
    "Document",
    "SplitButton",
    "TreeItem",
}


class UIASensor:
    """Extracts interactive controls from target window via Windows UI Automation."""

    def __init__(self, hwnd: int) -> None:
        self.hwnd = hwnd
        self.viewport = ViewportTransform(hwnd)

    def sense(self) -> SceneIndex:
        """Query accessibility tree and return normalized SceneIndex."""
        self.viewport.refresh()
        elements_map = {}
        window_title = "Unknown Window"
        sense_error: Optional[str] = None

        if sys.platform != "win32":
            return SceneIndex(
                hwnd=self.hwnd,
                window_title="Non-Windows Stub",
                source="uia",
                viewport={"x": 0, "y": 0, "width": 800, "height": 600},
                elements={},
            )

        try:
            from pywinauto import Application
            app = Application(backend="uia").connect(handle=self.hwnd)
            win = app.window(handle=self.hwnd)
            window_title = win.window_text() or f"HWND:{self.hwnd}"

            # Query descendants with safety filter
            element_id = 0
            descendants = win.descendants()

            for ctrl in descendants:
                try:
                    role = ctrl.element_info.control_type or "Unknown"
                    if role not in INTERACTIVE_CONTROL_TYPES:
                        continue

                    # Filter visible controls with positive dimensions
                    if not ctrl.is_visible():
                        continue

                    rect = ctrl.rectangle()
                    width = rect.width()
                    height = rect.height()
                    if width <= 0 or height <= 0:
                        continue

                    label = (ctrl.window_text() or ctrl.element_info.name or "").strip()
                    # If label is empty, try getting help text or automation id
                    if not label:
                        label = (ctrl.element_info.automation_id or f"{role}_{element_id}").strip()

                    screen_bbox = (rect.left, rect.top, rect.right, rect.bottom)
                    norm_bbox = self.viewport.bbox_screen_to_norm(screen_bbox)
                    center_screen = ((rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2)

                    # Extract value if available (e.g. Edit controls)
                    value: Optional[str] = None
                    if role in ("Edit", "Document", "ComboBox"):
                        try:
                            value = ctrl.iface_value.CurrentValue if hasattr(ctrl, "iface_value") else ctrl.window_text()
                        except Exception:
                            value = ctrl.window_text()

                    is_focusable = False
                    try:
                        is_focusable = bool(ctrl.element_info.element.CurrentIsKeyboardFocusable)
                    except Exception:
                        pass

                    elements_map[element_id] = SceneElement(
                        element_id=element_id,
                        label=label,
                        role=role,
                        bbox_screen=screen_bbox,
                        bbox_norm=norm_bbox,
                        center_screen=center_screen,
                        is_keyboard_focusable=is_focusable,
                        value=value,
                    )
                    element_id += 1
                except Exception:
                    continue

        except Exception as e:
            # Previously the error text was written into window_title, which made the
            # next diff report "title changed" and the post-action gate count a COM
            # failure as a successful UI transition. Flag it explicitly instead.
            logger.warning("UIA sensing failed for HWND %s: %s", self.hwnd, e)
            sense_error = str(e) or e.__class__.__name__

        b = self.viewport.bounds
        return SceneIndex(
            hwnd=self.hwnd,
            window_title=window_title,
            source="uia",
            viewport={
                "x": b.left,
                "y": b.top,
                "width": b.width,
                "height": b.height,
                "client_width": b.client_width,
                "client_height": b.client_height,
            },
            elements=elements_map,
            sense_error=sense_error,
        )
