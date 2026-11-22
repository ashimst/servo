"""Windows app identity: which *app* a window belongs to, not just which process.

Many apps on Windows are not a plain ``something.exe``:

* Installed web apps (PWAs) such as Facebook, Messenger or YouTube run inside
  ``msedge.exe`` / ``chrome.exe`` but are presented as separate apps, with their own
  Start-menu entry, taskbar icon and window.
* Store apps (WhatsApp, Teams, the new Outlook, Instagram) have no Start-menu shortcut
  and their executables under ``WindowsApps`` cannot be started directly.

Windows identifies all of these by an AppUserModelID (AUMID), e.g.
``5319275A.WhatsAppDesktop_cv1g1gvanyjgm!App`` or ``FACEBOOK.FACEBOOK_8xx8rvfyw5nnt!App``.
The Start menu lists every app with its AUMID, windows carry it (as a window property or
through their process' package identity), and ``shell:AppsFolder\\<AUMID>`` launches it.
Matching on AUMID instead of the process name is what lets Servo tell the Facebook app
apart from an ordinary Edge window.
"""

from __future__ import annotations

import ctypes
import json
import logging
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

logger = logging.getLogger("servo.app_identity")

# Processes that host installed web apps. A window of one of these that carries a
# foreign app id is an app window, not a browser window.
BROWSER_PROCESSES = frozenset({
    "msedge.exe", "chrome.exe", "brave.exe", "vivaldi.exe", "opera.exe", "arc.exe", "dia.exe",
    "chromium.exe", "firefox.exe",
})
# Names under which the browsers themselves appear in the Start menu.
_BROWSER_NAMES = ("microsoft edge", "google chrome", "chrome", "brave", "vivaldi", "opera", "arc",
                  "dia", "firefox", "mozilla firefox", "chromium")

# Characters an AUMID may contain. Anything else is refused before it reaches explorer.
_SAFE_APP_ID = re.compile(r"^[\w.\-!{}\\~ ]{2,260}$")

_CACHE_TTL = 600.0


@dataclass(frozen=True)
class StartApp:
    name: str
    app_id: str

    @property
    def kind(self) -> str:
        return classify(self.app_id)


def classify(app_id: str) -> str:
    """'web_app' | 'store' | 'desktop' for a Start-menu AUMID."""
    aid = app_id or ""
    if "_crx_" in aid.lower():
        return "web_app"                     # Chrome/Edge unpackaged PWA
    if "!" in aid:
        family = aid.split("!", 1)[0]
        # Edge's packaged PWAs are named after their site: "www.facebook.com-1C2D851A_…!App".
        if re.match(r"^(?:www\.)?[a-z0-9-]+(?:\.[a-z0-9-]+)+-", family, re.I):
            return "web_app"
        return "store"
    return "desktop"


def safe_app_id(app_id: str) -> bool:
    return bool(app_id) and bool(_SAFE_APP_ID.match(app_id)) and ".." not in app_id


def launch_target(app_id: str) -> str:
    return f"shell:AppsFolder\\{app_id}"


def launch(app_id: str) -> None:
    """Start an app by AUMID the same way the Start menu does."""
    if not safe_app_id(app_id):
        raise ValueError(f"Refusing unusual app id {app_id!r}")
    subprocess.Popen(["explorer.exe", launch_target(app_id)])


# ------------------------------------------------------------------ Start menu catalog

_lock = threading.Lock()
_cache: List[StartApp] = []
_cache_at = 0.0


def start_apps(refresh: bool = False) -> List[StartApp]:
    """Every app the Start menu lists, with its AUMID. Cached; [] off Windows or on error."""
    global _cache, _cache_at
    if sys.platform != "win32":
        return []
    with _lock:
        if _cache and not refresh and time.time() - _cache_at < _CACHE_TTL:
            return list(_cache)
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                 "Get-StartApps | Select-Object Name, AppID | ConvertTo-Json -Compress"],
                capture_output=True, text=True, timeout=15, encoding="utf-8", errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            ).stdout.strip()
            rows = json.loads(out) if out else []
            if isinstance(rows, dict):
                rows = [rows]
            apps = [StartApp(str(r.get("Name") or "").strip(), str(r.get("AppID") or "").strip())
                    for r in rows if r.get("Name") and r.get("AppID")]
        except Exception as e:  # noqa: BLE001 - catalog is best effort
            logger.debug("Get-StartApps failed: %s", e)
            apps = list(_cache)
        _cache, _cache_at = apps, time.time()
        return list(apps)


def browser_app_ids() -> frozenset:
    """AUMIDs of the browsers themselves (e.g. 'MSEdge', Arc's package id)."""
    return frozenset(a.app_id for a in start_apps() if a.name.lower() in _BROWSER_NAMES)


# ------------------------------------------------------------------ window -> app id

def _window_property_app_id(hwnd: int) -> str:
    try:
        from win32com.propsys import propsys, pscon  # type: ignore[import-not-found]
        store = propsys.SHGetPropertyStoreForWindow(hwnd, propsys.IID_IPropertyStore)
        value = store.GetValue(pscon.PKEY_AppUserModel_ID).GetValue()
        return str(value or "")
    except Exception:
        return ""


def _pid(hwnd: int) -> int:
    from ctypes import wintypes
    pid = wintypes.DWORD()
    ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def process_app_id(pid: int) -> str:
    """AUMID from a process' package identity (Store apps), or ''."""
    if sys.platform != "win32" or not pid:
        return ""
    from ctypes import wintypes
    k32 = ctypes.windll.kernel32
    handle = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ""
    try:
        length = wintypes.UINT(512)
        buf = ctypes.create_unicode_buffer(length.value)
        fn = getattr(k32, "GetApplicationUserModelId", None)
        if fn is None or fn(handle, ctypes.byref(length), buf) != 0:
            return ""
        return buf.value
    finally:
        k32.CloseHandle(handle)


def _frame_host_child_pid(hwnd: int) -> int:
    """UWP windows belong to ApplicationFrameHost; the app is a child in another process."""
    from ctypes import wintypes
    own = _pid(hwnd)
    found: List[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _cb(child, _l):  # type: ignore[no-untyped-def]
        p = _pid(int(child))
        if p and p != own:
            found.append(p)
            return False
        return True

    ctypes.windll.user32.EnumChildWindows(hwnd, _cb, 0)
    return found[0] if found else 0


def window_app_id(hwnd: int, process: str = "") -> str:
    """The AUMID of the app that owns this window, or '' for a plain desktop program."""
    if sys.platform != "win32" or not hwnd:
        return ""
    aid = _window_property_app_id(hwnd)
    if aid:
        return aid
    try:
        pid = _pid(hwnd)
        if process.lower() == "applicationframehost.exe":
            pid = _frame_host_child_pid(hwnd) or pid
        return process_app_id(pid)
    except Exception:
        return ""


def is_web_app_window(hwnd: int, process: str, app_id: Optional[str] = None) -> bool:
    """True for an installed web app's window (Facebook, YouTube…) hosted by a browser."""
    if process.lower() not in BROWSER_PROCESSES:
        return False
    aid = window_app_id(hwnd, process) if app_id is None else app_id
    if not aid:
        return False
    if "_crx_" in aid.lower():
        return True
    # Only an id that belongs to a listed non-browser app counts: Edge's own windows may
    # carry ids ("MSEdge", per-profile variants) that the Start menu doesn't list.
    return aid not in browser_app_ids() and aid in app_names_by_id()


def app_names_by_id() -> Dict[str, str]:
    return {a.app_id: a.name for a in start_apps()}


def window_label(hwnd: int, process: str, app_id: Optional[str] = None) -> str:
    """How to name a window's app to the planner.

    'Facebook — installed web app (runs in msedge.exe)' instead of just 'msedge.exe',
    so it is not mistaken for a browser window to type URLs into.
    """
    proc = (process or "").lower()
    try:
        aid = window_app_id(hwnd, proc) if app_id is None else app_id
        name = app_names_by_id().get(aid, "") if aid else ""
        if name and is_web_app_window(hwnd, proc, aid):
            return f"{name} — installed web app (runs in {proc})"
        if name and name.lower() not in _BROWSER_NAMES and classify(aid) == "store":
            return f"{name} — Store app ({proc})"
    except Exception:
        pass
    return proc
