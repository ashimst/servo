"""Adaptive Settling and Mutation Debounce Engine.

Eliminates race conditions and animation lag by bounded polling for state changes.
"""

from __future__ import annotations

import time
from typing import Callable
from servo.config import settings
from servo.perception.scene_index import SceneIndex, SceneDelta


class SettlingEngine:
    """Debounces UI actions by waiting for dynamic transitions to settle."""

    def __init__(
        self,
        base_delay_ms: int | None = None,
        max_poll_ms: int | None = None,
        poll_interval_ms: int | None = None,
    ) -> None:
        # `is not None` rather than `or`: an explicit 0 (e.g. no base delay in tests or
        # harness runs) is a valid setting and must not silently become the default.
        self.base_delay_ms = settings.settling_base_ms if base_delay_ms is None else base_delay_ms
        self.max_poll_ms = settings.settling_max_ms if max_poll_ms is None else max_poll_ms
        self.poll_interval_ms = (
            settings.settling_poll_interval_ms if poll_interval_ms is None else poll_interval_ms
        )

    def settle_and_diff(
        self,
        before_scene: SceneIndex,
        sensor_fn: Callable[[], SceneIndex],
    ) -> tuple[SceneIndex, SceneDelta]:
        """Wait base delay, then poll until a SceneDelta is observed or max timeout expires.

        Scenes flagged with ``sense_error`` never count as a change (see
        SceneDelta.compute_diff), so a transient sensing failure keeps polling rather
        than ending the wait with a false "UI changed".
        """
        # Baseline delay (e.g. 100ms)
        time.sleep(self.base_delay_ms / 1000.0)

        start_time = time.perf_counter()
        timeout_sec = self.max_poll_ms / 1000.0
        interval_sec = self.poll_interval_ms / 1000.0

        current_scene = sensor_fn()
        delta = SceneDelta.compute_diff(before_scene, current_scene)

        # If already changed, return immediately without wasting polling time
        if delta.has_changed:
            return current_scene, delta

        # Otherwise, poll until a mutation occurs or timeout expires
        while (time.perf_counter() - start_time) < timeout_sec:
            time.sleep(interval_sec)
            current_scene = sensor_fn()
            if current_scene.sense_error == "window closed":
                break   # the window went away: polling it again until timeout only logs errors
            delta = SceneDelta.compute_diff(before_scene, current_scene)
            if delta.has_changed:
                break

        return current_scene, delta
