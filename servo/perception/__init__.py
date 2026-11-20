"""Perception package: Viewport transforms, UIA sensor, Mirage visual fallback, and Scene Index."""

from servo.perception.viewport import ViewportTransform, WindowBounds
from servo.perception.scene_index import SceneIndex, SceneElement, SceneDelta

__all__ = [
    "ViewportTransform",
    "WindowBounds",
    "SceneIndex",
    "SceneElement",
    "SceneDelta",
]
