"""Windows OCR: the text on a window, with a box per line.

Windows.Media.Ocr ships with Windows, so it adds no model to load. Measured on 12 WAA app
screens (384 labelled controls): it finds 80 controls by their visible label (Mirage 78,
RapidOCR/PP-OCRv5 87) in ~0.12 s per screen (Mirage 3.5 s, RapidOCR 1.9 s), with no cap on
the number of lines. Controls without visible text (icons) are the visual locator's job.

In the perception order it is the step after the accessibility tree: a target the tree
does not expose is matched against the text on screen before the vision model is asked.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import io
import sys
import time
from typing import List, Tuple

from PIL import Image

from servo.perception.mirage_sensor import Detection


class OcrUnavailable(RuntimeError):
    """Windows OCR is not available on this machine (no engine for the user's languages)."""


def read_lines(img: Image.Image) -> Tuple[List[Detection], int]:
    """OCR lines (text + pixel box) for ``img`` and the latency in ms. Raises OcrUnavailable."""
    if sys.platform != "win32":
        raise OcrUnavailable("Windows OCR needs Windows")
    try:
        import winsdk.windows.graphics.imaging as imaging
        import winsdk.windows.media.ocr as ocr
        import winsdk.windows.storage.streams as streams
    except Exception as e:  # noqa: BLE001
        raise OcrUnavailable(f"winsdk is not installed: {e}") from e

    async def recognize() -> List[Detection]:
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="PNG")
        writer = streams.DataWriter()
        writer.write_bytes(buf.getvalue())
        ras = streams.InMemoryRandomAccessStream()
        await ras.write_async(writer.detach_buffer())
        ras.seek(0)
        bitmap = await (await imaging.BitmapDecoder.create_async(ras)).get_software_bitmap_async()
        engine = ocr.OcrEngine.try_create_from_user_profile_languages()
        if engine is None:
            raise OcrUnavailable("Windows has no OCR engine for the user's languages")
        result = await engine.recognize_async(bitmap)
        out: List[Detection] = []
        for line in result.lines:
            words = list(line.words)
            text = (line.text or "").strip()
            if not words or not text:
                continue
            rects = [w.bounding_rect for w in words]
            box = (int(min(r.x for r in rects)), int(min(r.y for r in rects)),
                   int(max(r.x + r.width for r in rects)), int(max(r.y + r.height for r in rects)))
            out.append(Detection(label=text, bbox=box, kind="text", score=1.0))
        return out

    t0 = time.perf_counter()
    try:
        try:
            asyncio.get_running_loop()
            in_loop = True
        except RuntimeError:
            in_loop = False
        if in_loop:  # called from async code: run on a worker thread with its own loop
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                lines = ex.submit(lambda: asyncio.run(recognize())).result(timeout=10)
        else:
            lines = asyncio.run(recognize())
    except OcrUnavailable:
        raise
    except Exception as e:  # noqa: BLE001
        raise OcrUnavailable(f"Windows OCR failed: {e.__class__.__name__}: {e}") from e
    return lines, int((time.perf_counter() - t0) * 1000)
