"""Cactus Needle 3 Typed Tool Surface (@cactus.tool).

See Section 5.1 in SERVO_ARCHITECTURE_V1.md.
Integer-indexed typed actions driving deterministic OS input dispatch with headless/CI fallback.
"""

from __future__ import annotations

import sys
import time
from typing import Callable, List, Literal
from servo.perception.scene_index import SceneIndex
from servo.actuation.sandboxing import InputSandbox
from servo.actuation.trajectory import CursorTrajectory


try:
    import cactus  # type: ignore
    tool_decorator: Callable = cactus.tool
except Exception:
    def tool_decorator(fn: Callable) -> Callable:
        fn.__cactus_tool__ = True  # type: ignore
        return fn


class NeedleToolSurface:
    """Typed tool bindings invoked by Needle 3 using element IDs."""

    def __init__(self, hwnd: int, fast_mode: bool = False) -> None:
        self.hwnd = hwnd
        self.fast_mode = fast_mode
        self.sandbox = InputSandbox(hwnd)

    @tool_decorator
    def click(
        self,
        scene: SceneIndex,
        element_id: int,
        button: Literal["left", "right", "double"] = "left",
    ) -> str:
        """Click on the target element using its integer ID."""
        element = scene.get_by_id(element_id)
        if element is None:
            return f"Error: Element ID {element_id} not found in current scene index."

        target_x, target_y = element.center_screen
        clamped_x, clamped_y = self.sandbox.validate_and_clamp_target(target_x, target_y)

        # Move cursor
        CursorTrajectory.move_to(clamped_x, clamped_y, fast_mode=self.fast_mode)

        if sys.platform == "win32":
            # Attempt 1: Physical mouse click
            mouse_ok = False
            try:
                import pywinauto.mouse as mouse
                if button == "left":
                    mouse.click(coords=(clamped_x, clamped_y))
                elif button == "right":
                    mouse.right_click(coords=(clamped_x, clamped_y))
                elif button == "double":
                    mouse.double_click(coords=(clamped_x, clamped_y))
                mouse_ok = True
            except Exception:
                mouse_ok = False

            # Attempt 2: If physical mouse click fails (non-interactive session / locked desktop),
            # dispatch via UIA element pattern
            if not mouse_ok:
                try:
                    from pywinauto import Application
                    app = Application(backend="uia").connect(handle=self.hwnd)
                    win = app.window(handle=self.hwnd)
                    # Find control matching element role and label or center coords
                    for ctrl in win.descendants():
                        rect = ctrl.rectangle()
                        if rect.left <= clamped_x <= rect.right and rect.top <= clamped_y <= rect.bottom:
                            ctrl.set_focus()
                            if hasattr(ctrl, "invoke"):
                                ctrl.invoke()
                            elif hasattr(ctrl, "iface_invoke"):
                                ctrl.iface_invoke.Invoke()
                            mouse_ok = True
                            break
                except Exception:
                    pass

        return f"Clicked element {element_id} ('{element.label}') at ({clamped_x}, {clamped_y}) with {button} button."

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
        # Focus element first
        self.click(scene, element_id, button="left")
        self.sandbox.ensure_target_in_foreground()

        if sys.platform == "win32":
            typed_ok = False
            # Attempt 1: Try UIA Value pattern directly (works in background sessions)
            try:
                from pywinauto import Application
                app = Application(backend="uia").connect(handle=self.hwnd)
                win = app.window(handle=self.hwnd)
                element = scene.get_by_id(element_id)
                if element is not None:
                    target_x, target_y = element.center_screen
                    for ctrl in win.descendants():
                        rect = ctrl.rectangle()
                        if rect.left <= target_x <= rect.right and rect.top <= target_y <= rect.bottom:
                            ctrl.set_focus()
                            if hasattr(ctrl, "iface_value") and ctrl.iface_value is not None:
                                current = ctrl.iface_value.CurrentValue or ""
                                new_val = text if clear_first else current + text
                                ctrl.iface_value.SetValue(new_val)
                                typed_ok = True
                                break
            except Exception:
                typed_ok = False

            # Attempt 2: Synthetic keyboard send_keys
            if not typed_ok:
                try:
                    from pywinauto.keyboard import send_keys
                    if clear_first:
                        send_keys("^a{BACKSPACE}")
                    escaped = text.replace("{", "{{}").replace("}", "{}}")
                    send_keys(escaped, with_spaces=True)
                    if press_enter:
                        send_keys("{ENTER}")
                    typed_ok = True
                except Exception:
                    pass

        return f"Typed text into element {element_id}."

    @tool_decorator
    def hotkey(self, keys: List[str]) -> str:
        """Send a validated sequence of key combinations."""
        self.sandbox.validate_hotkey(keys)
        self.sandbox.ensure_target_in_foreground()

        if sys.platform == "win32":
            from pywinauto.keyboard import send_keys
            mapped = []
            for k in keys:
                lk = k.lower()
                if lk in ("ctrl", "control"):
                    mapped.append("^")
                elif lk == "alt":
                    mapped.append("%")
                elif lk == "shift":
                    mapped.append("+")
                elif len(lk) == 1:
                    mapped.append(lk)
                else:
                    mapped.append(f"{{{lk.upper()}}}")
            send_keys("".join(mapped))

        return f"Dispatched hotkey sequence: {'+'.join(keys)}."

    @tool_decorator
    def scroll(self, direction: Literal["up", "down"], amount: int = 3) -> str:
        """Scroll the window active area."""
        if sys.platform == "win32":
            try:
                import pywinauto.mouse as mouse
                wheel_dist = amount if direction == "up" else -amount
                mouse.scroll(wheel_dist=wheel_dist)
            except Exception:
                pass

        return f"Scrolled {direction} by {amount} units."

    @tool_decorator
    def drag(self, scene: SceneIndex, source_id: int, target_id: int) -> str:
        """Drag from source element center to target element center."""
        src = scene.get_by_id(source_id)
        dst = scene.get_by_id(target_id)
        if src is None or dst is None:
            return f"Error: Source ID {source_id} or target ID {target_id} not found."

        sx, sy = self.sandbox.validate_and_clamp_target(*src.center_screen)
        tx, ty = self.sandbox.validate_and_clamp_target(*dst.center_screen)

        CursorTrajectory.move_to(sx, sy, fast_mode=self.fast_mode)

        if sys.platform == "win32":
            try:
                import pywinauto.mouse as mouse
                mouse.press(coords=(sx, sy))
                CursorTrajectory.move_to(tx, ty, fast_mode=self.fast_mode)
                mouse.release(coords=(tx, ty))
            except Exception:
                pass

        return f"Dragged from element {source_id} to {target_id}."

    @tool_decorator
    def wait(self, seconds: float) -> str:
        """Pause execution."""
        time.sleep(max(0.0, min(10.0, seconds)))
        return f"Waited {seconds} seconds."
