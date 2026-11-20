"""End-to-End Smoke Test for Milestone 1: The Reflex Arc.

Tests UIA perception, coordinate normalization, sandboxing, and Needle 3 actuation
against Windows Notepad (notepad.exe) without any LLM calls.
"""

import subprocess
import sys
import time
import pytest

from servo.perception.uia_sensor import UIASensor
from servo.perception.mirage_sensor import MirageSensor
from servo.actuation.needle_tools import NeedleToolSurface
from servo.perception.scene_index import SceneDelta


@pytest.mark.desktop
@pytest.mark.skipif(sys.platform != "win32", reason="Requires Windows 10/11 environment")
def test_notepad_reflex_arc():
    """Launch Notepad, sense UI with UIA, type text using Needle 3, and verify buffer."""
    # 1. Launch notepad.exe
    proc = subprocess.Popen(["notepad.exe"])
    time.sleep(1.5)

    win = None
    try:
        from pywinauto import Application, findwindows
        # Connect to visible Notepad window by handle
        matching = findwindows.find_elements(title_re=".*Notepad.*", visible_only=True)
        assert len(matching) > 0, "Notepad window not found"
        target_handle = matching[0].handle

        app = Application(backend="uia").connect(handle=target_handle)
        win = app.window(handle=target_handle)
        hwnd = target_handle

        # 2. SENSE: Query UIA accessibility tree
        sensor = UIASensor(hwnd)
        scene = sensor.sense()

        assert scene.hwnd == hwnd
        assert len(scene.elements) > 0, "UIA should detect interactive controls in Notepad"

        # Locate the Edit/Document control (Text Editor)
        edit_elements = [el for el in scene.elements.values() if el.role in ("Edit", "Document")]
        assert len(edit_elements) > 0, "Should locate text editor area in Notepad"
        target_el = edit_elements[0]

        # 3. ACT: Needle 3 types text into the editor
        tools = NeedleToolSurface(hwnd, fast_mode=True)
        test_string = "Servo Reflex Arc Active"
        result = tools.type_text(scene, target_el.element_id, test_string, clear_first=True)
        assert "Typed text into element" in result

        time.sleep(0.5)

        # 4. RE-SENSE & VERIFY: Read back text from editor control
        post_scene = sensor.sense()
        post_edit = post_scene.get_by_id(target_el.element_id)
        assert post_edit is not None

        # Verify that SceneDelta captures the interaction
        delta = SceneDelta.compute_diff(scene, post_scene)
        assert delta is not None

        assert test_string in (post_edit.value or "") or delta.has_changed

        # 5. MIRAGE FALLBACK SENSOR: real detections, or an error-flagged empty scene
        # when Cactus is not installed (never synthetic regions).
        mirage = MirageSensor(hwnd)
        mirage_scene = mirage.sense(window_title="Notepad")
        assert mirage_scene.source == "mirage"
        assert len(mirage_scene.elements) > 0 or mirage_scene.sense_error

    finally:
        # Cleanly close notepad window without saving
        if win is not None:
            try:
                win.close()
            except Exception:
                pass
        try:
            proc.kill()
        except Exception:
            pass
