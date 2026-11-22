"""Win32 window helpers: enumeration, owning process lookup, and hit-testing.

These are used by the sandbox (allowlists, occlusion checks) and by the daemon's
``list_windows`` endpoint so callers can pick an explicit HWND instead of relying
on whatever window happens to be in the foreground.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from pathlib import Path
from typing import Dict, List, Optional

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
GA_ROOT = 2

if sys.platform == "win32":
    _user32 = ctypes.windll.user32
    _kernel32 = ctypes.windll.kernel32

    _user32.WindowFromPoint.argtypes = [wintypes.POINT]
    _user32.WindowFromPoint.restype = wintypes.HWND
    _user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    _user32.GetAncestor.restype = wintypes.HWND
    _user32.GetForegroundWindow.restype = wintypes.HWND
    _user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    _user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    _user32.IsWindow.argtypes = [wintypes.HWND]
    _user32.IsWindowVisible.argtypes = [wintypes.HWND]
    _user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    _user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)
    ]
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]


def _as_int(hwnd: Optional[int]) -> int:
    return int(hwnd or 0)


def get_foreground_window() -> int:
    if sys.platform != "win32":
        return 0
    return _as_int(_user32.GetForegroundWindow())


def is_window(hwnd: int) -> bool:
    if sys.platform != "win32":
        return False
    return bool(_user32.IsWindow(hwnd))


def is_visible(hwnd: int) -> bool:
    if sys.platform != "win32":
        return False
    return bool(_user32.IsWindowVisible(hwnd))


def get_root_window(hwnd: int) -> int:
    """Return the top-level ancestor of ``hwnd`` (itself if already top-level)."""
    if sys.platform != "win32" or not hwnd:
        return _as_int(hwnd)
    return _as_int(_user32.GetAncestor(hwnd, GA_ROOT)) or _as_int(hwnd)


def window_at_point(x: int, y: int) -> int:
    """Return the top-level window that would receive a physical click at (x, y)."""
    if sys.platform != "win32":
        return 0
    child = _user32.WindowFromPoint(wintypes.POINT(x, y))
    return get_root_window(_as_int(child))


def get_window_title(hwnd: int) -> str:
    if sys.platform != "win32":
        return ""
    length = _user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(length + 1)
    _user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def get_process_name(hwnd: int) -> str:
    """Return the lowercase executable name (e.g. ``notepad.exe``) owning ``hwnd``, or ''."""
    if sys.platform != "win32" or not hwnd:
        return ""
    pid = wintypes.DWORD()
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return ""
    handle = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if _kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return Path(buf.value).name.lower()
        return ""
    finally:
        _kernel32.CloseHandle(handle)


def list_windows() -> List[Dict[str, object]]:
    """Enumerate visible, titled top-level windows."""
    if sys.platform != "win32":
        return []
    results: List[Dict[str, object]] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _callback(hwnd, _lparam):  # type: ignore[no-untyped-def]
        h = _as_int(hwnd)
        if _user32.IsWindowVisible(h):
            title = get_window_title(h)
            if title:
                results.append({"hwnd": h, "title": title, "process": get_process_name(h)})
        return True

    _user32.EnumWindows(_callback, 0)
    return results
