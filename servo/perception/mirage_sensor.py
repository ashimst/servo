"""Cactus Mirage visual perception.

Mirage (``cactus.Mirage().describe(image, sections=[...], fields=[...])``) returns JSON
from the engine. Its detection schema is not documented by Cactus, so
``parse_detections`` accepts the plausible shapes (lists under ``objects`` / ``text`` /
``detections`` / ``elements``; boxes as ``[x1,y1,x2,y2]``, ``[x,y,w,h]`` or dicts; pixel
or 0-1 normalized coordinates) and drops anything it cannot place.

Mirage is the *sideways* escalation in the cascade: it is used when the accessibility
tree (UIA) or the DOM cannot find a target, not when the request needs reasoning.
"""

from __future__ import annotations

import difflib
import logging
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

from PIL import Image, ImageGrab
from pydantic import BaseModel

from servo.config import settings
from servo.perception.scene_index import SceneElement, SceneIndex
from servo.perception.viewport import ViewportTransform

logger = logging.getLogger(__name__)

_LIST_KEYS = ("objects", "text", "lines", "words", "detections", "elements", "items", "regions")
_LABEL_KEYS = ("label", "text", "name", "class", "category", "content")


class Detection(BaseModel):
    """One Mirage detection, bbox in IMAGE pixel coordinates (x1, y1, x2, y2)."""
    label: str
    bbox: Tuple[int, int, int, int]
    score: Optional[float] = None
    kind: str = "object"

    @property
    def center(self) -> Tuple[int, int]:
        return ((self.bbox[0] + self.bbox[2]) // 2, (self.bbox[1] + self.bbox[3]) // 2)


def _num(v: Any) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _box(item: Dict[str, Any]) -> Optional[Tuple[float, float, float, float]]:
    raw = None
    for k in ("bbox", "box", "bounds", "rect", "bounding_box"):
        if k in item:
            raw = item[k]
            break
    if raw is None and all(k in item for k in ("x1", "y1", "x2", "y2")):
        raw = {k: item[k] for k in ("x1", "y1", "x2", "y2")}
    if raw is None and all(k in item for k in ("x", "y", "w", "h")):
        raw = {k: item[k] for k in ("x", "y", "w", "h")}
    if isinstance(raw, dict):
        if all(k in raw for k in ("x1", "y1", "x2", "y2")):
            vals = [_num(raw[k]) for k in ("x1", "y1", "x2", "y2")]
        elif all(k in raw for k in ("left", "top", "right", "bottom")):
            vals = [_num(raw[k]) for k in ("left", "top", "right", "bottom")]
        elif all(k in raw for k in ("x", "y")) and any(k in raw for k in ("w", "width")):
            x, y = _num(raw["x"]), _num(raw["y"])
            w, h = _num(raw.get("w", raw.get("width"))), _num(raw.get("h", raw.get("height")))
            if None in (x, y, w, h):
                return None
            vals = [x, y, x + w, y + h]  # type: ignore[operator]
        else:
            return None
    elif isinstance(raw, (list, tuple)) and len(raw) == 4:
        vals = [_num(v) for v in raw]
        if None not in vals and (vals[2] <= vals[0] or vals[3] <= vals[1]):  # type: ignore[operator]
            vals = [vals[0], vals[1], vals[0] + vals[2], vals[1] + vals[3]]  # type: ignore[operator]
    else:
        return None
    if None in vals:
        return None
    return tuple(vals)  # type: ignore[return-value]


def _walk_lists(raw: Any) -> Iterable[Tuple[str, Dict[str, Any]]]:
    if isinstance(raw, dict):
        for key, val in raw.items():
            if key in _LIST_KEYS and isinstance(val, list):
                kind = "text" if key in ("text", "lines", "words") else "object"
                for item in val:
                    if isinstance(item, dict):
                        yield kind, item
            elif isinstance(val, (dict, list)):
                yield from _walk_lists(val)
    elif isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict) and _box(item) is not None:
                yield "object", item
            else:
                yield from _walk_lists(item)


def parse_detections(raw: Any, width: int, height: int) -> List[Detection]:
    """Normalize Mirage output into pixel-space detections."""
    out: List[Detection] = []
    for kind, item in _walk_lists(raw):
        label = next((str(item[k]).strip() for k in _LABEL_KEYS if isinstance(item.get(k), str) and item[k].strip()), "")
        if not label:
            continue
        box = _box(item)
        if box is None:
            continue
        x1, y1, x2, y2 = box
        if max(x1, y1, x2, y2) <= 1.0:  # normalized 0-1
            x1, x2, y1, y2 = x1 * width, x2 * width, y1 * height, y2 * height
        x1, x2 = max(0, min(x1, width)), max(0, min(x2, width))
        y1, y2 = max(0, min(y1, height)), max(0, min(y2, height))
        if x2 - x1 < 2 or y2 - y1 < 2:
            continue
        score = _num(item.get("score", item.get("confidence", item.get("conf"))))
        out.append(Detection(label=label, bbox=(int(x1), int(y1), int(x2), int(y2)), score=score, kind=kind))
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


def _run_windows_ocr(img: Image.Image) -> Optional[List[Detection]]:
    """Native Windows OCR (WinRT via winsdk). Sub-100ms on Windows 10/11."""
    import sys
    if sys.platform != "win32":
        return None
    try:
        import winsdk.windows.media.ocr as ocr
        import winsdk.windows.graphics.imaging as imaging
        import winsdk.windows.storage.streams as streams
        import asyncio
        import io

        async def _rec():
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            writer = streams.DataWriter()
            writer.write_bytes(buf.getvalue())
            ras = streams.InMemoryRandomAccessStream()
            await ras.write_async(writer.detach_buffer())
            ras.seek(0)
            dec = await imaging.BitmapDecoder.create_async(ras)
            sb = await dec.get_software_bitmap_async()
            eng = ocr.OcrEngine.try_create_from_user_profile_languages()
            if not eng:
                return []
            res = await eng.recognize_async(sb)
            dets: List[Detection] = []
            for line in res.lines:
                lt = line.text.strip()
                if lt:
                    xs = [w.bounding_rect.x for w in line.words]
                    ys = [w.bounding_rect.y for w in line.words]
                    x2s = [w.bounding_rect.x + w.bounding_rect.width for w in line.words]
                    y2s = [w.bounding_rect.y + w.bounding_rect.height for w in line.words]
                    if xs and ys:
                        bbox = (int(min(xs)), int(min(ys)), int(max(x2s)), int(max(y2s)))
                        dets.append(Detection(label=lt, bbox=bbox, kind="text", score=0.95))
                if len(line.words) > 1:
                    for w in line.words:
                        wt = w.text.strip()
                        if len(wt) >= 2:
                            bx = (
                                int(w.bounding_rect.x),
                                int(w.bounding_rect.y),
                                int(w.bounding_rect.x + w.bounding_rect.width),
                                int(w.bounding_rect.y + w.bounding_rect.height),
                            )
                            dets.append(Detection(label=wt, bbox=bx, kind="text", score=0.90))
            return dets

        try:
            return asyncio.run(_rec())
        except RuntimeError:
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                return ex.submit(lambda: asyncio.run(_rec())).result(timeout=5.0)
    except Exception as e:
        logger.debug("Native Windows OCR unavailable or failed: %s", e)
        return None


def describe_image(img: Image.Image) -> Optional[Dict[str, Any]]:
    """Run Mirage (Cactus or native Windows OCR) on a PIL image. Returns raw JSON or None."""
    from servo.cognition.cactus_runtime import get_mirage

    model = get_mirage()
    if model is not None:
        try:
            rgb = img.convert("RGB")
            raw = model.describe(rgb, sections=["objects", "text"], fields=["bbox"])
            return raw if isinstance(raw, dict) else {"result": raw}
        except Exception as e:
            logger.warning("Mirage describe failed: %s", e)

    # Fallback to Native Windows OCR
    ocr_dets = _run_windows_ocr(img)
    if ocr_dets:
        return {
            "text": [
                {"label": d.label, "bbox": list(d.bbox), "score": d.score}
                for d in ocr_dets
            ]
        }
    return None


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
            return ImageGrab.grab(bbox=(b.left, b.top, b.right, b.bottom), all_screens=True)
        except Exception:
            return None

    def sense(self, window_title: str = "Target Window") -> SceneIndex:
        """Visual pass. Returns an empty scene with ``sense_error`` when Mirage is unavailable.

        No synthetic fallback: inventing regions made the loop click things that were not
        controls. An error-flagged scene makes the loop fail and escalate instead.
        """
        img = self.capture_window_image()
        b = self.viewport.bounds
        elements: Dict[int, SceneElement] = {}
        sense_error: Optional[str] = None

        raw = describe_image(img) if img is not None else None
        if img is None:
            sense_error = "Could not capture the target window for Mirage."
        elif raw is None:
            sense_error = "Cactus Mirage is unavailable or failed; no visual detections."
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

    def locate(self, target: str) -> Tuple[Optional[SceneElement], float, List[Tuple[str, float]], SceneIndex]:
        """Sense, then fuzzy-match ``target``. Returns (element|None, score, ranked, scene)."""
        scene = self.sense()
        if scene.sense_error:
            return None, 0.0, [], scene
        best, score, ranked = match_target(self.last_detections, target)
        if best is None or score < settings.mirage_match_threshold:
            return None, score, ranked, scene
        idx = self.last_detections.index(best)
        return scene.elements.get(idx), score, ranked, scene
