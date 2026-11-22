"""Keep Servo's own windows out of the way of the app it operates.

The copilot is a topmost panel docked to the right edge, so it sits on top of whatever
the user is working in: Facebook's Messenger and notification buttons, a browser's menu,
a dialog's OK button. Anything under it could not be clicked (the hit-test saw the copilot
on top and refused, and many web controls have no programmatic "invoke" to fall back on),
scrolled or dragged, and the vision fallback photographed the copilot instead of the app.

``stand_aside`` moves any of Servo's windows that cover the points (or area) about to be
clicked or captured off-screen for the duration of the action, then puts them back exactly
where they were, without changing focus or z-order. It is a no-op when nothing overlaps.
"""

from __future__ import annotations

import contextlib
import logging
import sys
import threading
import time
from typing import Callable, Iterable, Iterator, List, Optional, Sequence, Set, Tuple

from servo.perception import windows

logger = logging.getLogger("servo.overlay")

OWN_WINDOW_TITLES = ("Servo Copilot",)

_providers: List[Callable[[], Set[int]]] = []
_lock = threading.Lock()

Rect = Tuple[int, int, int, int]
Point = Tuple[int, int]

_OFFSCREEN = -32000
# SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_NOOWNERZORDER
_SWP_MOVE_ONLY = 0x0001 | 0x0004 | 0x0010 | 0x0200


def register_own_windows(provider: Callable[[], Set[int]]) -> None:
    """Tell the overlay which windows are Servo's (the copilot registers itself)."""
    with _lock:
        if provider not in _providers:
            _providers.append(provider)


def own_windows() -> Set[int]:
    """Servo's visible windows: registered ones plus any titled like the copilot
    (so a run from another process, e.g. the task suite, also avoids it)."""
    out: Set[int] = set()
    with _lock:
        providers = list(_providers)
    for p in providers:
        try:
            out |= {int(h) for h in p() if h}
        except Exception:  # noqa: BLE001
            pass
    try:
        for w in windows.list_windows():
            title = str(w.get("title") or "")
            if any(t in title for t in OWN_WINDOW_TITLES):
                out.add(int(w["hwnd"]))
    except Exception:  # noqa: BLE001
        pass
    return {h for h in out if windows.is_window(h) and windows.is_visible(h)}


def window_rect(hwnd: int) -> Optional[Rect]:
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes
    r = wintypes.RECT()
    if not ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(r)):
        return None
    return r.left, r.top, r.right, r.bottom


def _contains(rect: Rect, pt: Point) -> bool:
    return rect[0] <= pt[0] < rect[2] and rect[1] <= pt[1] < rect[3]


def _intersects(a: Rect, b: Rect) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def covering(points: Sequence[Point] = (), area: Optional[Rect] = None,
             exclude: Iterable[int] = ()) -> List[Tuple[int, Rect]]:
    """Servo windows that overlap any of the points or the area."""
    skip = {int(h) for h in exclude if h}
    hits: List[Tuple[int, Rect]] = []
    for h in own_windows():
        if h in skip:
            continue
        r = window_rect(h)
        if not r or r[0] <= _OFFSCREEN // 2:
            continue
        if any(_contains(r, p) for p in points) or (area is not None and _intersects(r, area)):
            hits.append((h, r))
    return hits


def _move(hwnd: int, x: int, y: int) -> None:
    import ctypes
    ctypes.windll.user32.SetWindowPos(hwnd, 0, x, y, 0, 0, _SWP_MOVE_ONLY)


@contextlib.contextmanager
def stand_aside(points: Sequence[Point] = (), area: Optional[Rect] = None,
                exclude: Iterable[int] = (), settle: float = 0.05) -> Iterator[List[int]]:
    """Move Servo windows covering ``points``/``area`` off-screen during the block.

    Yields the HWNDs that were moved ([] when nothing was in the way).
    """
    if sys.platform != "win32":
        yield []
        return
    moved: List[Tuple[int, Rect]] = []
    try:
        for h, r in covering(points, area, exclude):
            try:
                _move(h, _OFFSCREEN, _OFFSCREEN)
                moved.append((h, r))
            except Exception as e:  # noqa: BLE001
                logger.debug("Could not move HWND %s aside: %s", h, e)
        if moved:
            logger.info("Moved %d Servo window(s) aside for the action", len(moved))
            time.sleep(settle)  # let the compositor repaint what was underneath
        yield [h for h, _ in moved]
    finally:
        for h, r in moved:
            try:
                if windows.is_window(h):
                    _move(h, r[0], r[1])
            except Exception as e:  # noqa: BLE001
                logger.warning("Could not put HWND %s back: %s", h, e)
