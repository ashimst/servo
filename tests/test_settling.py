"""Unit tests for Adaptive Settling and Debounce Engine."""

import time
from servo.control.settling import SettlingEngine
from servo.perception.scene_index import SceneIndex, SceneElement


def test_settling_engine_immediate_mutation():
    engine = SettlingEngine(base_delay_ms=20, max_poll_ms=100, poll_interval_ms=10)

    before = SceneIndex(
        hwnd=10,
        window_title="Initial",
        source="uia",
        elements={},
    )

    # Sensor returns changed scene on first call
    after = SceneIndex(
        hwnd=10,
        window_title="Mutated",
        source="uia",
        elements={},
    )

    t0 = time.perf_counter()
    res_scene, delta = engine.settle_and_diff(before, lambda: after)
    elapsed = time.perf_counter() - t0

    assert delta.has_changed is True
    assert delta.title_changed is True
    # Should complete shortly after base_delay without waiting max_poll_ms
    assert elapsed < 0.20


def test_settling_engine_delayed_mutation():
    engine = SettlingEngine(base_delay_ms=20, max_poll_ms=250, poll_interval_ms=20)

    before = SceneIndex(hwnd=10, window_title="Start", source="uia", elements={})
    call_count = 0

    def delayed_sensor():
        nonlocal call_count
        call_count += 1
        if call_count >= 3:
            return SceneIndex(hwnd=10, window_title="Delayed Change", source="uia", elements={})
        return before

    res_scene, delta = engine.settle_and_diff(before, delayed_sensor)
    assert delta.has_changed is True
    assert delta.new_title == "Delayed Change"
    assert call_count >= 3
