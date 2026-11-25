"""The copilot panel steps aside for clicks, scrolls, drags and screenshots beneath it."""

import pytest

from servo.actuation import overlay

PANEL = 77
PANEL_RECT = (1380, 41, 1923, 1038)


@pytest.fixture
def panel(monkeypatch):
    moves = []
    pos = {PANEL: PANEL_RECT}
    monkeypatch.setattr(overlay, "own_windows", lambda: {PANEL})
    monkeypatch.setattr(overlay, "window_rect", lambda h: pos.get(h))

    def move(h, x, y):
        moves.append((h, x, y))
        l, t, r, b = pos[h]
        pos[h] = (x, y, x + r - l, y + b - t)

    monkeypatch.setattr(overlay, "_move", move)
    monkeypatch.setattr(overlay.windows, "is_window", lambda h: True)
    monkeypatch.setattr(overlay.time, "sleep", lambda s: None)
    monkeypatch.setattr(overlay.sys, "platform", "win32")
    return moves, pos


def test_covering_points_and_areas(panel):
    assert overlay.covering([(1769, 68)]) == [(PANEL, PANEL_RECT)]
    assert overlay.covering([(400, 300)]) == []
    assert overlay.covering(area=(0, 0, 1920, 1080)) == [(PANEL, PANEL_RECT)]
    assert overlay.covering(area=(0, 0, 1000, 1000)) == []
    assert overlay.covering([(1769, 68)], exclude=[PANEL]) == []   # the target itself


def test_stand_aside_moves_and_restores(panel):
    moves, pos = panel
    with overlay.stand_aside([(1769, 68)]) as moved:
        assert moved == [PANEL]
        assert pos[PANEL][0] == overlay._OFFSCREEN
        assert overlay.covering([(1769, 68)]) == []
    assert pos[PANEL] == PANEL_RECT


def test_stand_aside_restores_after_an_error(panel):
    moves, pos = panel
    with pytest.raises(RuntimeError):
        with overlay.stand_aside([(1769, 68)]):
            raise RuntimeError("click failed")
    assert pos[PANEL] == PANEL_RECT


def test_nothing_moves_when_nothing_overlaps(panel):
    moves, _ = panel
    with overlay.stand_aside([(400, 300)]) as moved:
        assert moved == []
    assert moves == []


def test_scroll_under_the_panel_moves_it_aside(panel, monkeypatch):
    """Before: 'covered by another window; refusing to scroll'."""
    import pywinauto.mouse as mouse
    from servo.actuation import needle_tools
    from servo.perception.scene_index import SceneElement, SceneIndex
    moves, pos = panel
    el = SceneElement(element_id=0, role="Text", label="Birthdays", bbox_screen=(1600, 480, 1700, 520),
                      bbox_norm=(0, 0, 1, 1), center_screen=(1650, 500))
    scene = SceneIndex(hwnd=5, window_title="Facebook", source="uia", elements={0: el})
    surface = needle_tools.NeedleToolSurface.__new__(needle_tools.NeedleToolSurface)
    surface.fast_mode = True

    class Sandbox:
        hwnd = 5

        def point_hits_target(self, x, y):   # on top only while the panel is away
            return not overlay._contains(pos[PANEL], (x, y))

    surface.sandbox = Sandbox()
    monkeypatch.setattr(surface, "_resolve", lambda s, i: (el, 1650, 500), raising=False)
    scrolled = []
    monkeypatch.setattr(mouse, "scroll", lambda coords, wheel_dist: scrolled.append(coords))
    monkeypatch.setattr(needle_tools.time, "sleep", lambda s: None)
    monkeypatch.setattr(needle_tools.sys, "platform", "win32")
    assert "Scrolled down" in str(surface.scroll(scene, 0, "down"))
    assert scrolled == [(1650, 500)] and pos[PANEL] == PANEL_RECT


def test_router_ignores_servo_windows_without_a_list(monkeypatch):
    from servo.cognition.router import CascadeRouter
    from servo.control.handoff import SystemHandoffCoordinator
    monkeypatch.setattr(overlay, "own_windows", lambda: {PANEL})
    from servo.cognition.trace import NullTracer
    r = CascadeRouter(tracer=NullTracer())
    assert PANEL in r.exclude_hwnds()
    assert PANEL in SystemHandoffCoordinator(target_hwnd=None).exclude_hwnds()
