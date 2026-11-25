"""Cactus Mirage visual perception.

``cactus.Mirage().describe(image, ...)`` (cactus-compute 4.x) returns::

    {"objects": [{"id": 1, "name": "button", "bbox": [x1, y1, x2, y2], "score": 0.38}, ...],
     "text":    [{"string": "Save", "bbox": [x1, y1, x2, y2], "score": 0.45}, ...]}

with boxes on a 0-1000 scale relative to the image. There is no other visual path: when
Mirage is not installed or fails, the scene carries ``sense_error`` and the caller fails.
"""

from __future__ import annotations

import difflib
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image, ImageGrab
from pydantic import BaseModel

from servo.perception.scene_index import SceneElement, SceneIndex
from servo.perception.viewport import ViewportTransform

logger = logging.getLogger(__name__)

# Mirage boxes are on a 0-1000 scale relative to the image.
MIRAGE_SCALE = 1000.0
# Measured on 12 WAA app screens (384 labelled controls): these settings put a box on 69%
# of controls (23% by chance) vs 48% for the defaults, at ~1.7 s per screen on CPU.
DESCRIBE_KWARGS: Dict[str, Any] = dict(sections=["objects", "text"], fields=["bbox", "score"], max_objects=100,
                                       max_text_lines=200, conf=0.05, text_conf=0.05)


class Detection(BaseModel):
    """One Mirage detection, bbox in IMAGE pixel coordinates (x1, y1, x2, y2)."""
    label: str
    bbox: Tuple[int, int, int, int]
    score: Optional[float] = None
    kind: str = "object"

    @property
    def center(self) -> Tuple[int, int]:
        return ((self.bbox[0] + self.bbox[2]) // 2, (self.bbox[1] + self.bbox[3]) // 2)


def parse_detections(raw: Any, width: int, height: int) -> List[Detection]:
    """Mirage's ``objects`` (label in ``name``) and ``text`` (label in ``string``), boxes
    converted from the 0-1000 scale to image pixels."""
    out: List[Detection] = []
    if not isinstance(raw, dict):
        return out
    for section, key, kind in (("objects", "name", "object"), ("text", "string", "text")):
        for item in raw.get(section) or []:
            if not isinstance(item, dict):
                continue
            label = str(item.get(key) or "").strip()
            box = item.get("bbox")
            if not label or not isinstance(box, (list, tuple)) or len(box) != 4:
                continue
            try:
                x1, y1, x2, y2 = (float(v) for v in box)
            except (TypeError, ValueError):
                continue
            x1, x2 = x1 * width / MIRAGE_SCALE, x2 * width / MIRAGE_SCALE
            y1, y2 = y1 * height / MIRAGE_SCALE, y2 * height / MIRAGE_SCALE
            x1, x2 = max(0.0, min(x1, width)), max(0.0, min(x2, width))
            y1, y2 = max(0.0, min(y1, height)), max(0.0, min(y2, height))
            if x2 - x1 < 2 or y2 - y1 < 2:
                continue
            score = item.get("score")
            out.append(Detection(label=label, bbox=(int(x1), int(y1), int(x2), int(y2)),
                                 score=float(score) if isinstance(score, (int, float)) else None, kind=kind))
    return out


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", s.lower()).strip()


def match_score(target: str, label: str) -> float:
    t, lbl = _norm(target), _norm(label)
    if not t or not lbl:
        return 0.0
    if t == lbl:
        return 1.0
    if re.search(r"\b" + re.escape(t) + r"\b", lbl):
        return 0.9 if len(t) >= 3 else 0.75
    return difflib.SequenceMatcher(None, t, lbl).ratio()


def match_target(dets: List[Detection], target: str) -> Tuple[Optional[Detection], float, List[Tuple[str, float]]]:
    """Best detection for a target label, its score, and the runner-up list for the trace."""
    scored = sorted(((match_score(target, d.label), d) for d in dets), key=lambda x: -x[0])
    ranked = [(d.label, round(s, 3)) for s, d in scored[:6]]
    if not scored:
        return None, 0.0, ranked
    best_score, best = scored[0]
    # Ambiguity: two near-identical candidates is not a confident match.
    if len(scored) > 1 and scored[1][0] >= best_score - 0.02 and scored[1][1].label != best.label:
        best_score *= 0.8
    return best, best_score, ranked


class MirageUnavailable(RuntimeError):
    """Mirage is not installed, failed to load, or failed on this image."""


def describe_image(img: Image.Image) -> Dict[str, Any]:
    """Run Mirage on a PIL image and return its JSON. Raises MirageUnavailable."""
    from servo.cognition.cactus_runtime import get_mirage, mirage_error

    model = get_mirage()
    if model is None:
        raise MirageUnavailable(f"Cactus Mirage is unavailable: {mirage_error() or 'not installed'}")
    try:
        raw = model.describe(img.convert("RGB"), **DESCRIBE_KWARGS)
    except Exception as e:  # noqa: BLE001 - surfaced to the caller as the failure it is
        raise MirageUnavailable(f"Mirage describe failed: {e.__class__.__name__}: {e}") from e
    if not isinstance(raw, dict):
        raise MirageUnavailable(f"Mirage returned {type(raw).__name__}, expected a JSON object")
    return raw


def summarize(dets: List[Detection], limit: int = 60) -> str:
    """Compact text description of what Mirage saw (fed to System 2 on escalation)."""
    lines = [f"- {d.kind} '{d.label}' at {d.bbox}" for d in dets[:limit]]
    return "\n".join(lines) if lines else "(no detections)"


class MirageSensor:
    """Visual sensor: screenshot of the target window -> Mirage -> SceneIndex (screen coords)."""

    def __init__(self, hwnd: int) -> None:
        self.hwnd = hwnd
        self.viewport = ViewportTransform(hwnd)
        self.last_detections: List[Detection] = []

    def capture_window_image(self) -> Optional[Image.Image]:
        self.viewport.refresh()
        b = self.viewport.bounds
        if b.width <= 1 or b.height <= 1:
            return None
        try:
            # The copilot panel sits on top of the app; without moving it aside, the
            # vision model was shown the copilot instead of the controls beneath it.
            from servo.actuation.overlay import stand_aside
            box = (b.left, b.top, b.right, b.bottom)
            with stand_aside(area=box, exclude=[self.hwnd], settle=0.12):
                return ImageGrab.grab(bbox=box, all_screens=True)
        except Exception:
            return None

    def sense(self, window_title: str = "Target Window") -> SceneIndex:
        """Visual pass. Returns an empty scene with ``sense_error`` when Mirage cannot run."""
        img = self.capture_window_image()
        b = self.viewport.bounds
        elements: Dict[int, SceneElement] = {}
        sense_error: Optional[str] = None
        self.last_detections = []

        if img is None:
            sense_error = "Could not capture the target window for Mirage."
        else:
            try:
                raw = describe_image(img)
            except MirageUnavailable as e:
                sense_error = str(e)
            else:
                self.last_detections = parse_detections(raw, img.width, img.height)
                for idx, d in enumerate(self.last_detections):
                    x1, y1, x2, y2 = d.bbox
                    screen = (b.left + x1, b.top + y1, b.left + x2, b.top + y2)
                    elements[idx] = SceneElement(
                        element_id=idx,
                        label=d.label,
                        role="VisualText" if d.kind == "text" else "VisualControl",
                        bbox_screen=screen,
                        bbox_norm=self.viewport.bbox_screen_to_norm(screen),
                        center_screen=((screen[0] + screen[2]) // 2, (screen[1] + screen[3]) // 2),
                    )
                if not elements:
                    sense_error = "Mirage returned no placeable detections."

        return SceneIndex(
            hwnd=self.hwnd,
            window_title=window_title,
            source="mirage",
            viewport={"x": b.left, "y": b.top, "width": b.width, "height": b.height},
            elements=elements,
            sense_error=sense_error,
        )
