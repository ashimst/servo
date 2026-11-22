"""Cactus Needle 3 Typed Tool Surface (@cactus.tool).

See Section 5.1 in SERVO_ARCHITECTURE_V1.md and SERVO_HARDENING_NOTES.md.
Integer-indexed typed actions driving deterministic OS input dispatch.

Every tool either completes or raises ``ActuationError`` / ``SandboxViolationException``.
Tools never report success for an action that did not happen.
"""

from __future__ import annotations

import logging
import re
import sys
import time
from typing import Any, Callable, List, Literal, Optional, Tuple

from servo.perception.scene_index import SceneElement, SceneIndex
from servo.actuation.sandboxing import InputSandbox, normalize_keys
from servo.actuation.trajectory import CursorTrajectory
from servo.actuation.overlay import stand_aside

logger = logging.getLogger(__name__)

try:
    import cactus  # type: ignore
    tool_decorator: Callable = cactus.tool
except Exception:
    def tool_decorator(fn: Callable) -> Callable:
        fn.__cactus_tool__ = True  # type: ignore
        return fn


class ActuationError(Exception):
    """Raised when an input action could not be delivered."""


# Characters with special meaning to pywinauto.keyboard.send_keys. Each must be
# wrapped in braces to be typed literally; previously only { and } were escaped,
# so typing "a+b" sent Shift+b and "50%" sent an Alt chord.
_SEND_KEYS_SPECIAL = set("+^%~(){}[]")


def escape_send_keys(text: str) -> str:
    return "".join(f"{{{c}}}" if c in _SEND_KEYS_SPECIAL else c for c in text)


_MODIFIER_CODES = {"ctrl": "^", "alt": "%", "shift": "+"}

# Virtual-key codes for system media keys, which pywinauto's send_keys doesn't know.
MEDIA_KEYS = {
    "volumeup": 0xAF, "volumedown": 0xAE, "volumemute": 0xAD, "mute": 0xAD,
    "playpause": 0xB3, "mediaplaypause": 0xB3, "nexttrack": 0xB0, "medianext": 0xB0,
    "prevtrack": 0xB1, "previoustrack": 0xB1, "mediaprev": 0xB1, "stopmedia": 0xB2, "mediastop": 0xB2,
}


def media_key_vk(keys: List[str]) -> Optional[int]:
    """VK code if ``keys`` is a single media key ('volumeup', 'volume up', 'vol+'), else None."""
    if len(keys) != 1:
        return None
    k = re.sub(r"[\s_\-]+", "", keys[0].strip().lower()).replace("vol+", "volumeup").replace("vol-", "volumedown")
    return MEDIA_KEYS.get(k)


def build_hotkey_sequence(keys: List[str]) -> str:
    """Translate ['ctrl', 'shift', 's'] into pywinauto send_keys syntax ('^+s')."""
    norm = normalize_keys(keys)
    modifiers = [k for k in norm if k in _MODIFIER_CODES]
    others = [k for k in norm if k not in _MODIFIER_CODES]
    if len(others) != 1:
        raise ActuationError(f"Hotkey must contain exactly one non-modifier key, got {keys}.")
    key = others[0]
    key_code = escape_send_keys(key) if len(key) == 1 else f"{{{key.upper()}}}"
    return "".join(_MODIFIER_CODES[m] for m in modifiers) + key_code


def _rect_area(rect: Any) -> int:
    return max(0, rect.right - rect.left) * max(0, rect.bottom - rect.top)


def find_innermost_control(win: Any, x: int, y: int) -> Optional[Any]:
    """Return the smallest UIA control whose rectangle contains (x, y).

    ``descendants()`` is ordered parent-first, so "first match" is almost always a
    container pane rather than the button/edit the scene element refers to.
    """
    best = None
    best_area = None
    for ctrl in win.descendants():
        try:
            rect = ctrl.rectangle()
        except Exception:
            continue
        if rect.left <= x <= rect.right and rect.top <= y <= rect.bottom:
            area = _rect_area(rect)
            if best is None or area < best_area:
                best, best_area = ctrl, area
    return best


class NeedleToolSurface:
    """Typed tool bindings invoked by Needle 3 using element IDs."""

    def __init__(self, hwnd: int, fast_mode: bool = False) -> None:
        self.hwnd = hwnd
        self.fast_mode = fast_mode
        self.sandbox = InputSandbox(hwnd)

    def _resolve(self, scene: SceneIndex, element_id: int) -> Tuple[SceneElement, int, int]:
        element = scene.get_by_id(element_id)
        if element is None:
            raise ActuationError(f"Element ID {element_id} not found in current scene index.")
        x, y = self.sandbox.validate_target(*element.center_screen)
        return element, x, y

    def _uia_window(self) -> Any:
        from pywinauto import Application
        app = Application(backend="uia").connect(handle=self.hwnd)
        return app.window(handle=self.hwnd)

    def read_text(self, scene: SceneIndex, element_id: int, limit: int = 200_000) -> Optional[str]:
        """Read back the text of the control at an element's position, or None if unreadable.

        Used to verify typing directly. Labels are useless for that: Win11 Notepad, for
        one, renames its document to its contents, so label-keyed diffs never line up.
        Tries the smallest control under the point first and walks outward until one
        exposes ValuePattern or TextPattern.
        """
        if sys.platform != "win32":
            return None
        element = scene.get_by_id(element_id)
        if element is None:
            return None
        x, y = element.center_screen
        try:
            hits = []
            for ctrl in self._uia_window().descendants():
                try:
                    r = ctrl.rectangle()
                except Exception:
                    continue
                if r.left <= x <= r.right and r.top <= y <= r.bottom:
                    hits.append((_rect_area(r), ctrl))
            for _, ctrl in sorted(hits, key=lambda h: h[0]):
                try:
                    return str(ctrl.iface_value.CurrentValue or "")[:limit]
                except Exception:
                    pass
                try:
                    return str(ctrl.iface_text.DocumentRange.GetText(limit) or "")
                except Exception:
                    pass
        except Exception as e:
            logger.info("Text read-back unavailable: %s", e)
        return None

    def _uia_invoke(self, x: int, y: int) -> bool:
        """Activate the control at (x, y) through UIA patterns (no physical input)."""
        ctrl = find_innermost_control(self._uia_window(), x, y)
        if ctrl is None:
            return False
        for attempt in (
            lambda: ctrl.invoke(),
            lambda: ctrl.iface_invoke.Invoke(),
            lambda: ctrl.toggle(),
            lambda: ctrl.select(),
            lambda: ctrl.set_focus(),
        ):
            try:
                attempt()
                return True
            except Exception:
                continue
        return False

    @tool_decorator
    def click(
        self,
        scene: SceneIndex,
        element_id: int,
        button: Literal["left", "right", "double"] = "left",
    ) -> str:
        """Click on the target element using its integer ID."""
        element, x, y = self._resolve(scene, element_id)

        if sys.platform != "win32":
            raise ActuationError("Physical input is only supported on Windows.")

        # A physical click goes to whatever window is on top at (x, y). If another
        # window covers the target, clicking would act on *that* application, so we
        # only click physically when the hit-test confirms the target is on top.
        # Servo's own copilot panel is moved aside first if it covers the point.
        with stand_aside([(x, y)], exclude=[self.sandbox.hwnd]) as moved:
            if self.sandbox.point_hits_target(x, y):
                CursorTrajectory.move_to(x, y, fast_mode=self.fast_mode)
                import pywinauto.mouse as mouse
                try:
                    if button == "left":
                        mouse.click(coords=(x, y))
                    elif button == "right":
                        mouse.right_click(coords=(x, y))
                    else:
                        mouse.double_click(coords=(x, y))
                    time.sleep(0.05)  # let the click land before the copilot returns
                    aside = " (moved the copilot aside)" if moved else ""
                    return f"Clicked element {element_id} ('{element.label}') at ({x}, {y}) with {button} button{aside}."
                except Exception as e:
                    logger.warning("Physical click failed (%s); trying UIA invoke.", e)

        # Fallback: occluded window or non-interactive session. Only a left click maps
        # onto UIA Invoke; right/double clicks have no safe pattern equivalent.
        if button == "left" and self._uia_invoke(x, y):
            return f"Invoked element {element_id} ('{element.label}') via UIA pattern."

        raise ActuationError(
            f"Could not click element {element_id} ('{element.label}'): target is covered by another "
            "window or the session has no interactive desktop, and no UIA pattern was available."
        )

    @tool_decorator
    def invoke(self, scene: SceneIndex, element_id: int) -> str:
        """Activate the target element through UIA control patterns without physical mouse input."""
        element, x, y = self._resolve(scene, element_id)

        if sys.platform != "win32":
            raise ActuationError("UIA invocation is only supported on Windows.")

        if self._uia_invoke(x, y):
            return f"Invoked element {element_id} ('{element.label}') via UIA pattern."

        raise ActuationError(
            f"Could not invoke element {element_id} ('{element.label}'): no supported UIA pattern found "
            "(InvokePattern, TogglePattern, SelectionItemPattern, or SetFocus)."
        )

    @tool_decorator
    def type_text(
        self,
        scene: SceneIndex,
        element_id: int,
        text: str,
        press_enter: bool = False,
        clear_first: bool = False,
    ) -> str:
        """Focus the target element and enter text."""
        element, x, y = self._resolve(scene, element_id)

        if sys.platform != "win32":
            raise ActuationError("Text input is only supported on Windows.")

        # Attempt 1: UIA ValuePattern. Works without foreground activation and without
        # moving the physical cursor.
        ctrl = None
        try:
            ctrl = find_innermost_control(self._uia_window(), x, y)
            if ctrl is not None:
                iface = None
                try:
                    iface = ctrl.iface_value
                except Exception:
                    iface = None  # control does not support ValuePattern
                if iface is not None and not iface.CurrentIsReadOnly:
                    current = iface.CurrentValue or ""
                    iface.SetValue(text if clear_first else current + text)
                    if press_enter:
                        self.sandbox.ensure_target_in_foreground()
                        ctrl.set_focus()
                        from pywinauto.keyboard import send_keys
                        send_keys("{ENTER}")
                    return f"Set value of element {element_id} ('{element.label}') via UIA."
                else:
                    try:
                        ctrl.set_focus()
                    except Exception:
                        pass
        except Exception as e:
            logger.info("UIA ValuePattern typing unavailable (%s); falling back to keystrokes.", e)

        # Attempt 2: focus by click (or UIA focus), then synthetic keystrokes.
        try:
            self.click(scene, element_id, button="left")
        except Exception as e:
            logger.warning("Click to focus failed (%s); trying UIA focus.", e)
            if ctrl is not None:
                try:
                    ctrl.set_focus()
                except Exception:
                    pass

        try:
            self.sandbox.ensure_target_in_foreground()
        except Exception:
            pass

        from pywinauto.keyboard import send_keys
        if clear_first:
            send_keys("^a{BACKSPACE}")
        send_keys(escape_send_keys(text), with_spaces=True, with_tabs=True, with_newlines=True)
        if press_enter:
            send_keys("{ENTER}")
        return f"Typed text into element {element_id} ('{element.label}') via keystrokes."

    @tool_decorator
    def hotkey(self, keys: List[str]) -> str:
        """Send a validated key combination to the target window."""
        self.sandbox.validate_hotkey(keys)
        media = media_key_vk(keys)
        if media is not None:
            if sys.platform != "win32":
                raise ActuationError("Media keys are only supported on Windows.")
            # System-wide keys (volume, play/pause): no target window needed.
            import ctypes
            user32 = ctypes.windll.user32
            user32.keybd_event(media, 0, 0, 0)
            user32.keybd_event(media, 0, 2, 0)  # KEYEVENTF_KEYUP
            return f"Pressed media key {keys[0]}."
        sequence = build_hotkey_sequence(keys)
        if sys.platform != "win32":
            raise ActuationError("Hotkeys are only supported on Windows.")
        self.sandbox.ensure_target_in_foreground()
        from pywinauto.keyboard import send_keys
        send_keys(sequence)
        return f"Dispatched hotkey sequence: {'+'.join(keys)}."

    @tool_decorator
    def scroll(self, scene: SceneIndex, element_id: int, direction: Literal["up", "down"], amount: int = 3) -> str:
        """Scroll over the target element (wheel events go to the window under the cursor)."""
        element, x, y = self._resolve(scene, element_id)
        if sys.platform != "win32":
            raise ActuationError("Scrolling is only supported on Windows.")
        import pywinauto.mouse as mouse
        with stand_aside([(x, y)], exclude=[self.sandbox.hwnd]):
            if not self.sandbox.point_hits_target(x, y):
                raise ActuationError(f"Element {element_id} is covered by another window; refusing to scroll.")
            wheel_dist = amount if direction == "up" else -amount
            mouse.scroll(coords=(x, y), wheel_dist=wheel_dist)
            time.sleep(0.05)
        return f"Scrolled {direction} by {amount} units over element {element_id}."

    @tool_decorator
    def drag(self, scene: SceneIndex, source_id: int, target_id: int) -> str:
        """Drag from source element center to target element center."""
        _, sx, sy = self._resolve(scene, source_id)
        _, tx, ty = self._resolve(scene, target_id)
        if sys.platform != "win32":
            raise ActuationError("Drag is only supported on Windows.")
        import pywinauto.mouse as mouse
        with stand_aside([(sx, sy), (tx, ty)], exclude=[self.sandbox.hwnd]):
            if not (self.sandbox.point_hits_target(sx, sy) and self.sandbox.point_hits_target(tx, ty)):
                raise ActuationError("Drag endpoints are covered by another window; refusing to drag.")
            CursorTrajectory.move_to(sx, sy, fast_mode=self.fast_mode)
            mouse.press(coords=(sx, sy))
            try:
                CursorTrajectory.move_to(tx, ty, fast_mode=self.fast_mode)
            finally:
                mouse.release(coords=(tx, ty))
        return f"Dragged from element {source_id} to {target_id}."

    @tool_decorator
    def wait(self, seconds: float) -> str:
        """Pause execution (clamped to 0-10 s)."""
        actual = max(0.0, min(10.0, seconds))
        time.sleep(actual)
        return f"Waited {actual} seconds."
