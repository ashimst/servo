"""UI Automation (UIA) sensor wrapping pywinauto.

Extracts semantic accessibility tree and visible interactive elements.
See ADR-01 and Section 2.1 in SERVO_ARCHITECTURE_V1.md.
"""

from __future__ import annotations

import logging
import sys
import time
from typing import List, Optional, Set
from servo.perception.viewport import ViewportTransform
from servo.perception.scene_index import SceneIndex, SceneElement

logger = logging.getLogger(__name__)

# A browser-hosted window with fewer interactive controls than this is probably showing
# only its frame (back/refresh/close) because page accessibility is still off.
_CHROMIUM_EMPTY = 15
_nudged: Set[int] = set()


def _is_chromium_host(hwnd: int) -> bool:
    try:
        from servo.perception import windows
        from servo.perception.app_identity import BROWSER_PROCESSES
        proc = windows.get_process_name(hwnd)
        return proc in BROWSER_PROCESSES and proc != "firefox.exe"
    except Exception:
        return False


def _request_accessibility(hwnd: int) -> bool:
    """Ask for the window's MSAA client object, as screen readers do.

    Chromium treats this request as "an assistive tool is running" and builds the
    page's accessibility tree, which UI Automation then exposes.
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        import comtypes
        iid = comtypes.GUID("{618736E0-3C3D-11CF-810C-00AA00389B71}")  # IID_IAccessible
        ptr = ctypes.c_void_p()
        ctypes.oledll.oleacc.AccessibleObjectFromWindow(
            hwnd, ctypes.c_long(-4), ctypes.byref(iid), ctypes.byref(ptr))  # OBJID_CLIENT
        if ptr.value:
            # Release the reference we were handed (IUnknown::Release is vtable slot 2).
            vtbl = ctypes.cast(ctypes.cast(ptr, ctypes.POINTER(ctypes.c_void_p))[0],
                               ctypes.POINTER(ctypes.c_void_p))
            ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtbl[2])(ptr)
        return True
    except Exception as e:  # noqa: BLE001
        logger.debug("Accessibility request failed for HWND %s: %s", hwnd, e)
        return False

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
        """Query accessibility tree and return normalized SceneIndex.

        Chromium (Edge, Chrome, installed web apps such as Facebook) keeps a page's
        accessibility tree switched off until it believes an assistive tool is asking.
        Until then only the window frame is visible (7 controls for a full Facebook
        feed). If a browser-hosted window looks empty, ask once the way screen readers
        do, then sense again.
        """
        scene = self._sense_once()
        page_hidden = len(scene.elements) < _CHROMIUM_EMPTY or not any(
            e.role == "Document" for e in scene.elements.values())
        if page_hidden and self.hwnd not in _nudged and _is_chromium_host(self.hwnd):
            _nudged.add(self.hwnd)
            if _request_accessibility(self.hwnd):
                for _ in range(4):
                    time.sleep(0.35)
                    again = self._sense_once()
                    if len(again.elements) > len(scene.elements):
                        logger.info("Chromium page accessibility enabled for HWND %s (%d -> %d controls)",
                                    self.hwnd, len(scene.elements), len(again.elements))
                        return again
        return scene

    def _sense_once(self) -> SceneIndex:
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
