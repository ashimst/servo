"""Visual locator: Qwen3.5-4B (vision) finds a named target on a screenshot.

Used when neither the accessibility tree nor the text on screen (Windows OCR) has the
target: icons, canvas and custom-drawn controls. It is the planner model itself with its
vision projector loaded, so it adds no second model:

    llama-server -m Qwen3.5-4B-Q4_K_M.gguf --mmproj Qwen3.5-4B-mmproj-F16.gguf -c 8192 ...

Measured on 12 WAA app screens (106 lookups): the point lands inside the named control 65%
of the time (UGround-V1-2B: 48%), 39 of 68 small controls, ~5.5 s per lookup on a 4 GB GPU
shared with planning. Answers ``{"point_2d": [x, y]}`` on a 0-1000 scale.
"""

from __future__ import annotations

import base64
import io
import re
import time
from typing import Optional, Tuple

import httpx
from PIL import Image

from servo.config import settings

PROMPT = ("Locate {d} in this screenshot. Reply with only JSON: "
          '{{"point_2d": [x, y]}} where x and y are the centre of that element, '
          "on a 0-1000 scale relative to the image width and height.")
# Innermost brackets: the coordinates, not the "2d" in "point_2d".
_BRACKETS = re.compile(r"\[([^\[\]]+)\]")
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")


class LocatorUnavailable(RuntimeError):
    """The vision model is not running, failed, or answered without a point."""


def parse_point(answer: str) -> Optional[Tuple[float, float]]:
    """(x, y) on the 0-1000 scale from the model's answer; a box is reduced to its centre."""
    m = _BRACKETS.search(answer or "")
    nums = [float(v) for v in _NUMBER.findall(m.group(1) if m else (answer or ""))]
    if len(nums) >= 4 and "bbox" in (answer or ""):
        return (nums[0] + nums[2]) / 2, (nums[1] + nums[3]) / 2
    if len(nums) >= 2 and all(0 <= v <= 1000 for v in nums[:2]):
        return nums[0], nums[1]
    return None


def locate(img: Image.Image, description: str, timeout: Optional[float] = None) -> Tuple[int, int, int]:
    """(x, y) in image pixels for ``description``, and the latency in ms. Raises LocatorUnavailable."""
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG")
    body = {"model": settings.local_planner_model,
            "messages": [{"role": "user", "content": [
                {"type": "image_url",
                 "image_url": {"url": "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()}},
                {"type": "text", "text": PROMPT.format(d=description)}]}],
            "temperature": 0, "max_tokens": 48}
    url = settings.locator_url.rstrip("/")
    t0 = time.perf_counter()
    try:
        with httpx.Client(timeout=timeout or settings.locator_timeout_seconds) as c:
            r = c.post(f"{url}/chat/completions", json=body)
        r.raise_for_status()
        answer = r.json()["choices"][0]["message"]["content"]
    except Exception as e:  # noqa: BLE001
        raise LocatorUnavailable(f"Vision locator at {url} failed: {e.__class__.__name__}: {e}") from e
    point = parse_point(answer)
    if point is None:
        raise LocatorUnavailable(f"Vision locator gave no point: {answer!r}"[:300])
    x = point[0] * img.width / 1000
    y = point[1] * img.height / 1000
    return int(round(x)), int(round(y)), int((time.perf_counter() - t0) * 1000)
