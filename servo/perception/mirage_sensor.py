"""Cactus Mirage (22 MB) Visual Fallback Wrapper.

See ADR-01 and Section 2.1 in SERVO_ARCHITECTURE_V1.md.
Invoked automatically when UIA controls are sparse (< 3 elements) or post-action checks fail.
"""

from __future__ import annotations

import io
import logging
from typing import Dict, Optional
from PIL import Image, ImageGrab

from servo.perception.viewport import ViewportTransform
from servo.perception.scene_index import SceneIndex, SceneElement

logger = logging.getLogger(__name__)


class MirageSensor:
    """Visual sensor fallback powered by Cactus Mirage (22 MB) or deterministic synthetic fallback."""

    def __init__(self, hwnd: int) -> None:
        self.hwnd = hwnd
        self.viewport = ViewportTransform(hwnd)

    def capture_window_image(self) -> Optional[Image.Image]:
        """Capture screenshot strictly cropped to target window bounds."""
        self.viewport.refresh()
        b = self.viewport.bounds
        if b.width <= 1 or b.height <= 1:
            return None
        try:
            # Screen bbox: (left, top, right, bottom)
            img = ImageGrab.grab(bbox=(b.left, b.top, b.right, b.bottom))
            return img
        except Exception:
            return None

    def sense(self, window_title: str = "Target Window") -> SceneIndex:
        """Perform a visual pass via Cactus Mirage and produce a SceneIndex.

        Returns an empty scene with ``sense_error`` set when Mirage is unavailable.
        """
        self.viewport.refresh()
        img = self.capture_window_image()
        elements_map: Dict[int, SceneElement] = {}

        # 1. Attempt Cactus Mirage if installed
        cactus_available = False
        try:
            import cactus  # type: ignore
            if hasattr(cactus, "Mirage"):
                mirage = cactus.Mirage()
                if img is not None:
                    # Save image to bytes
                    buf = io.BytesIO()
                    img.save(buf, format="PNG")
                    buf.seek(0)
                    desc = mirage.describe(buf.getvalue())
                    # If Mirage returns structured parsed detections, map them
                    if isinstance(desc, dict) and "elements" in desc:
                        for idx, item in enumerate(desc["elements"]):
                            norm_x1 = int(item.get("x1", 0))
                            norm_y1 = int(item.get("y1", 0))
                            norm_x2 = int(item.get("x2", 100))
                            norm_y2 = int(item.get("y2", 100))
                            s_x1, s_y1 = self.viewport.norm_to_screen(norm_x1, norm_y1)
                            s_x2, s_y2 = self.viewport.norm_to_screen(norm_x2, norm_y2)
                            center = ((s_x1 + s_x2) // 2, (s_y1 + s_y2) // 2)
                            elements_map[idx] = SceneElement(
                                element_id=idx,
                                label=item.get("label", f"Visual_{idx}"),
                                role=item.get("role", "VisualControl"),
                                bbox_screen=(s_x1, s_y1, s_x2, s_y2),
                                bbox_norm=(norm_x1, norm_y1, norm_x2, norm_y2),
                                center_screen=center,
                            )
                        cactus_available = True
        except ImportError:
            cactus_available = False
        except Exception as e:
            logger.warning("Cactus Mirage visual pass failed: %s", e)
            cactus_available = False

        # No synthetic fallback. The previous version invented three fixed regions
        # ("Visual Area Center", ...) when Mirage was unavailable, and the control loop
        # then physically clicked them as if they were real controls. An empty,
        # error-flagged scene makes the loop fail and escalate instead.
        sense_error = None
        if not cactus_available:
            sense_error = "Cactus Mirage is unavailable or failed; no visual detections."

        b = self.viewport.bounds
        return SceneIndex(
            hwnd=self.hwnd,
            window_title=window_title,
            source="mirage",
            viewport={
                "x": b.left,
                "y": b.top,
                "width": b.width,
                "height": b.height,
            },
            elements=elements_map,
            sense_error=sense_error,
        )
