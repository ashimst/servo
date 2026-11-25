"""Copilot brain and app-launch helpers.

The floating UI lives in servo.daemon.copilot_app; routing lives in
servo.cognition.router. This module keeps the conversational shortcuts, the legacy
and Windows app resolution/launching.
"""

from __future__ import annotations

import getpass
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Tuple


from servo.perception import app_identity, windows

logger = logging.getLogger("servo.copilot")

# Common application executable mappings on Windows
APP_EXECUTABLE_MAP = {
    "notepad": "notepad.exe",
    "calculator": "calc.exe",
    "calc": "calc.exe",
    "chrome": "chrome.exe",
    "google chrome": "chrome.exe",
    "google-chrome": "chrome.exe",
    "edge": "msedge.exe",
    "microsoft edge": "msedge.exe",
    "msedge": "msedge.exe",
    "firefox": "firefox.exe",
    "mozilla firefox": "firefox.exe",
    "brave": "brave.exe",
    "terminal": "wt.exe",
    "cmd": "cmd.exe",
    "command prompt": "cmd.exe",
    "powershell": "powershell.exe",
    "paint": "mspaint.exe",
    "mspaint": "mspaint.exe",
    "explorer": "explorer.exe",
    "file explorer": "explorer.exe",
    "task manager": "taskmgr.exe",
    "taskmgr": "taskmgr.exe",
    "code": "Code.exe",
    "vs code": "Code.exe",
    "vscode": "Code.exe",
}


def resolve_app_executable(app_name: str) -> str:
    """Resolve full executable path for Windows apps using Registry App Paths and known locations."""
    app_key = app_name.strip().lower()

    # 0. Check Windows Context Catalog (Start Menu & App Paths)
    try:
        from servo.cognition.windows_context import windows_context
        installed, app_info = windows_context.is_installed(app_name)
        if installed and app_info and app_info.executable and os.path.exists(app_info.executable):
            return app_info.executable
    except Exception:
        pass

    exe_name = APP_EXECUTABLE_MAP.get(app_key, f"{app_key}.exe")

    # 1. Check if already absolute or in PATH
    if os.path.isabs(exe_name) and os.path.exists(exe_name):
        return exe_name
    which_p = shutil.which(exe_name)
    if which_p and os.path.exists(which_p):
        return which_p

    # 2. Check Windows App Paths in Registry (HKCU and HKLM)
    if sys.platform == "win32":
        import winreg
        for target in (exe_name, f"{app_key}.exe"):
            for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
                try:
                    key_path = f"SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\App Paths\\{target}"
                    with winreg.OpenKey(root, key_path) as k:
                        val, _ = winreg.QueryValueEx(k, "")
                        val = val.strip('\"\'')
                        if os.path.exists(val):
                            return val
                except OSError:
                    pass

    # 3. Known fallback locations for common desktop applications
    candidates = [
        rf"C:\Program Files\Google\Chrome\Application\{exe_name}",
        rf"C:\Program Files (x86)\Google\Chrome\Application\{exe_name}",
        rf"C:\Program Files\Microsoft\Edge\Application\{exe_name}",
        rf"C:\Program Files (x86)\Microsoft\Edge\Application\{exe_name}",
        os.path.expandvars(rf"%LOCALAPPDATA%\Google\Chrome\Application\{exe_name}"),
        os.path.expandvars(rf"%LOCALAPPDATA%\Microsoft\Edge\Application\{exe_name}"),
        os.path.expandvars(rf"%LOCALAPPDATA%\Programs\{app_key}\{exe_name}"),
        os.path.expandvars(rf"%PROGRAMFILES%\{exe_name}"),
        os.path.expandvars(rf"%PROGRAMFILES(X86)%\{exe_name}"),
    ]
    for c in candidates:
        if os.path.exists(c):
            return c

    return exe_name


# Apps whose window belongs to a different process than the executable we start.
_PROCESS_ALIASES: Dict[str, Tuple[str, ...]] = {
    "terminal": ("windowsterminal.exe",),
    "windows terminal": ("windowsterminal.exe",),
    "calculator": ("calculatorapp.exe", "calculator.exe", "applicationframehost.exe"),
    "calc": ("calculatorapp.exe", "calculator.exe", "applicationframehost.exe"),
    "settings": ("systemsettings.exe", "applicationframehost.exe"),
    "paint": ("mspaint.exe",),
    "photos": ("photos.exe", "applicationframehost.exe"),
    "file explorer": ("explorer.exe",),
    "explorer": ("explorer.exe",),
}


def _app_record(app_name: str):
    """Catalog entry for the app (with its AppUserModelIDs), or None."""
    try:
        from servo.cognition.windows_context import windows_context
        installed, info = windows_context.is_installed(app_name)
        return info if installed else None
    except Exception:
        return None


def _app_ids(info: Any) -> Tuple[str, ...]:
    if info is None:
        return ()
    ids = list(getattr(info, "app_ids", None) or [])
    if getattr(info, "app_id", None) and info.app_id not in ids:
        ids.append(info.app_id)
    return tuple(ids)


def _matches_app(w: Dict[str, Any], app_key: str, exe_filename: str, app_ids: Tuple[str, ...] = ()) -> bool:
    """Match a window to an app by its APP IDENTITY or PROCESS, not its title.

    Title matching made "open notepad" focus an Edge tab whose title contained the
    word "notepad". Titles are used only when the process name is unknown.

    Installed web apps (Facebook, YouTube…) run inside msedge.exe/chrome.exe. Such a
    window matches only its own app, through its AppUserModelID; it is never "Edge"
    (before, "open edge" could pick the Facebook app and type URLs into it).
    """
    proc = str(w.get("process") or w.get("process_name") or "").lower()
    title = str(w.get("title") or "").lower()
    if "servo copilot" in title:
        return False
    hwnd = int(w.get("hwnd") or 0)
    aid = None
    if hwnd and (app_ids or proc in app_identity.BROWSER_PROCESSES):
        aid = w.get("app_id")
        if aid is None:
            aid = app_identity.window_app_id(hwnd, proc)
            w["app_id"] = aid
        if app_ids and aid and aid in app_ids:
            return True
        if app_identity.is_web_app_window(hwnd, proc, aid):
            return False
    if proc:
        if proc == "applicationframehost.exe":
            return app_key in title
        if proc in _PROCESS_ALIASES.get(app_key, ()):
            return True
        stem = proc[:-4] if proc.endswith(".exe") else proc
        return proc == exe_filename or stem == app_key.replace(" ", "") or (len(app_key) >= 4 and app_key.replace(" ", "") in stem)
    return app_key in title


# App names come from the user, Needle or an LLM (which may have read a hostile web
# page). Shell metacharacters have no place in an app name, so refuse them outright.
_UNSAFE_APP_NAME = re.compile(r'["&|^<>%;`$\r\n]')


def _app_windows() -> List[Dict[str, Any]]:
    """Top-level windows, with Store apps' inner CoreWindow after their frame.

    A Store app such as Settings briefly has two top-level windows: the frame
    (ApplicationFrameWindow) and its content (Windows.UI.Core.CoreWindow). Picking the inner
    one gave a scene with a single element and no controls.
    """
    ws = windows.list_windows()
    try:
        import win32gui
        return sorted(ws, key=lambda w: win32gui.GetClassName(int(w["hwnd"])) == "Windows.UI.Core.CoreWindow")
    except Exception:
        return ws


def find_app_window(app_name: str) -> Optional[int]:
    """HWND of an already-open window for the app, or None. Never launches anything."""
    if _UNSAFE_APP_NAME.search(app_name):
        return None
    app_key = app_name.strip().lower()
    try:
        exe_filename = os.path.basename(resolve_app_executable(app_name)).lower()
    except Exception:
        exe_filename = f"{app_key}.exe"
    ids = _app_ids(_app_record(app_name))
    for w in _app_windows():
        if _matches_app(w, app_key, exe_filename, ids):
            return int(w["hwnd"])
    return None


_SETTINGS_URI = re.compile(r"^ms-settings:[a-z0-9\-]*$", re.I)


def _settings_page(app_name: str) -> Optional[str]:
    """'ms-settings:colors' (a Windows Settings page link), else None."""
    name = app_name.strip()
    return name if _SETTINGS_URI.match(name) else None


def _open_settings_page(uri: str) -> Tuple[Optional[int], str]:
    """Open a Settings page directly. Harmless and read-only to open, so no command (and no
    approval prompt) is needed; before, this went through "run command Start-Process …"."""
    from servo.actuation.sandboxing import activate_window
    was_open = find_app_window("settings")
    before = _page_fingerprint(was_open) if was_open else None
    try:
        os.startfile(uri)  # type: ignore[attr-defined]
    except Exception as e:
        return None, f"Couldn't open {uri}: {e}"
    hwnd = None
    for _ in range(40):
        time.sleep(0.2)
        hwnd = find_app_window("settings")
        if hwnd:
            break
    if not hwnd:
        return None, f"Settings did not open for {uri}"
    activate_window(hwnd)
    # Settings navigates asynchronously: wait for the page to change and settle, or the
    # planner is shown the PREVIOUS page (it saw Accessibility after opening the taskbar page).
    last, stable, changed = None, 0, before is None
    for _ in range(12):
        time.sleep(0.35)
        fp = _page_fingerprint(hwnd)
        changed = changed or (fp != before)
        stable = stable + 1 if fp == last else 0
        last = fp
        if changed and stable >= 1:
            break
    title = windows.get_window_title(hwnd) or "Settings"
    if not changed:
        return hwnd, f"{title} (the page did not change: '{uri}' may not be a real Settings page; " \
                     f"use the 'Find a setting' search box)"
    return hwnd, title


def _page_fingerprint(hwnd: int) -> Tuple[str, ...]:
    """What page a window shows: its visible labels (cheap enough to poll a few times)."""
    try:
        from servo.perception.uia_sensor import UIASensor
        scene = UIASensor(hwnd).sense()
        return tuple(sorted(f"{e.role}:{e.label}" for e in scene.elements.values()))[:200]
    except Exception:
        return ()


def launch_or_focus_app(app_name: str) -> Tuple[Optional[int], str]:
    """Find an existing window for the app or launch it and return (hwnd, title)."""
    if _UNSAFE_APP_NAME.search(app_name):
        return None, f"Refusing to launch '{app_name}': app names cannot contain shell characters."
    page = _settings_page(app_name)
    if page is not None:
        return _open_settings_page(page)
    app_key = app_name.strip().lower()
    full_exe_path = resolve_app_executable(app_name)
    exe_filename = os.path.basename(full_exe_path).lower()
    record = _app_record(app_name)
    ids = _app_ids(record)

    def _focus(w: Dict[str, Any]) -> Tuple[int, str]:
        hwnd = int(w["hwnd"])
        if sys.platform == "win32":
            # Plain SetForegroundWindow is often refused (foreground lock): the app was
            # found but stayed behind, and the next step typed into another window.
            from servo.actuation.sandboxing import activate_window
            if not activate_window(hwnd):
                logger.info("Found %s (HWND %s) but Windows kept it in the background", app_name, hwnd)
        return hwnd, str(w.get("title") or app_name)

    # 1. Already open?
    for w in _app_windows():
        if _matches_app(w, app_key, exe_filename, ids):
            return _focus(w)

    # 1.5. If the app is not installed, fail fast without waiting 8 seconds
    if sys.platform == "win32":
        if not (os.path.isabs(full_exe_path) and os.path.exists(full_exe_path)) and not shutil.which(full_exe_path):
            try:
                from servo.cognition.windows_context import windows_context
                installed, _ = windows_context.is_installed(app_name)
                if not installed:
                    return None, f"Application '{app_name}' is not installed on this system."
            except Exception:
                pass

    # 2. Launch it. Store apps and installed web apps start through their AppUserModelID,
    #    like the Start menu does: they have no exe of their own (web apps) or one under
    #    WindowsApps that Windows refuses to start directly.
    by_id = bool(ids) and (
        getattr(record, "kind", "desktop") in ("store", "web_app")
        or "\\windowsapps\\" in full_exe_path.lower()
        or not (os.path.isabs(full_exe_path) and os.path.exists(full_exe_path))
        or any("!" in i for i in ids)
    )
    try:
        if by_id:
            app_identity.launch(ids[0])
        elif sys.platform == "win32":
            if os.path.isabs(full_exe_path) and os.path.exists(full_exe_path):
                subprocess.Popen([full_exe_path])
            else:
                # ShellExecute resolves App Paths / PATH like `start` did, without a shell.
                os.startfile(full_exe_path)  # type: ignore[attr-defined]
        else:
            subprocess.Popen([full_exe_path])
    except Exception as e:
        return None, f"Failed to launch '{app_name}': {e}"

    # 3. Wait for its window (Store apps like Notepad/Calculator run under a different
    #    process than the stub we launched, so match by name, for up to 8 s).
    for _ in range(40):
        time.sleep(0.2)
        for w in _app_windows():
            if _matches_app(w, app_key, exe_filename, ids):
                return _focus(w)

    return None, f"Window for '{app_name}' did not appear in time."


def resolve_default_user_name() -> str:
    """The signed-in account's name; "User" when Windows doesn't give a personal one."""
    env_user = os.environ.get("USERNAME") or os.environ.get("USER") or getpass.getuser()
    return env_user if env_user and env_user.lower() not in ("user", "default", "administrator") else "User"


class CopilotBrain:
    """Conversational intelligence and System 2 Macro Planner for Servo Copilot."""

    def __init__(
        self,
        user_name: Optional[str] = None,
        planner_provider: Optional[str] = None,
    ) -> None:
        self.user_name = user_name or resolve_default_user_name()
        self.planner_provider = planner_provider
        self.history: List[Dict[str, str]] = []
        self.active_hwnd: Optional[int] = None
        self.active_title: str = ""

