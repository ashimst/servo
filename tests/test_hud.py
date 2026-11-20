"""Unit tests for Transparent HUD Overlay SVG generation."""

from servo.daemon.ui import ServoHUDOverlay
from servo.perception.scene_index import SceneIndex, SceneElement


def test_hud_svg_generation():
    hud = ServoHUDOverlay()

    scene = SceneIndex(
        hwnd=5001,
        window_title="Settings Window",
        source="uia",
        viewport={"x": 100, "y": 100, "width": 800, "height": 600},
        elements={
            0: SceneElement(
                element_id=0,
                label="System",
                role="ListItem",
                bbox_screen=(120, 140, 240, 180),
                bbox_norm=(20, 40, 140, 80),
                center_screen=(180, 160),
            ),
            1: SceneElement(
                element_id=1,
                label="Bluetooth",
                role="ListItem",
                bbox_screen=(120, 190, 240, 230),
                bbox_norm=(20, 90, 140, 130),
                center_screen=(180, 210),
            ),
        },
    )

    svg = hud.generate_svg_overlay(scene, highlight_id=1)
    assert "<svg" in svg
    assert "</svg>" in svg
    assert 'class="box"' in svg
    assert "[0] System" in svg
    assert "[1] Bluetooth" in svg
    # Highlight color for element 1
    assert "#10b981" in svg


def test_hud_svg_escapes_labels():
    hud = ServoHUDOverlay()
    scene = SceneIndex(
        hwnd=1, window_title="x", source="uia",
        elements={0: SceneElement(
            element_id=0, label='<script>alert(1)</script>', role="Button",
            bbox_screen=(0, 0, 9, 9), bbox_norm=(0, 0, 9, 9), center_screen=(4, 4),
        )},
    )
    svg = hud.generate_svg_overlay(scene)
    assert "<script>" not in svg
    assert "&lt;script&gt;" in svg
