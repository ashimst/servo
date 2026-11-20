"""Adaptive Settling and Mutation Debounce Engine.

See ADR-08 in SERVO_ARCHITECTURE_V1.md.
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
        self.base_delay_ms = base_delay_ms or settings.settling_base_ms
        self.max_poll_ms = max_poll_ms or settings.settling_max_ms
        self.poll_interval_ms = poll_interval_ms or settings.settling_poll_interval_ms

    def settle_and_diff(
        self,
        before_scene: SceneIndex,
        sensor_fn: Callable[[], SceneIndex],
    ) -> tuple[SceneIndex, SceneDelta]:
        """Wait base delay, then poll until a SceneDelta is observed or max timeout expires."""
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
            delta = SceneDelta.compute_diff(before_scene, current_scene)
            if delta.has_changed:
                break

        return current_scene, delta
