"""Windows Desktop Context Engine and Knowledge Base (Desktop RAG).

Discovers, indexes, and provides semantic and lexical retrieval over:
1. Installed applications (Start Menu shortcuts, Registry App Paths, UWP/MSIX Store apps, system utilities).
2. Running processes and active desktop windows.
3. Windows system capabilities and settings URIs (ms-settings:*).
4. Web fallbacks for popular apps when not installed locally.

Supplies grounded desktop context to Tier 1 (Router/Needle) and Tier 2 (System 2 Planner)
so the system never flies blind or attempts blind execution of uninstalled software.
"""

from __future__ import annotations

import difflib
import glob
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Set, Tuple

from pydantic import BaseModel, Field

from servo.perception import windows

logger = logging.getLogger("servo.cognition.windows_context")


# ===================================================================== Models

class AppInfo(BaseModel):
    name: str
    aliases: List[str] = Field(default_factory=list)
    executable: Optional[str] = None
    app_id: Optional[str] = None
    is_installed: bool = True
    is_running: bool = False
    hwnd: Optional[int] = None
    source: str = "system"  # "system" | "start_menu" | "registry" | "uwp" | "web"
    web_fallback: Optional[str] = None


class WindowsCapability(BaseModel):
    name: str
    keywords: List[str]
    uri_or_command: str
    description: str
    category: str = "settings"  # "settings" | "utility" | "shortcut"


class WindowsContextResult(BaseModel):
    query: str
    active_windows: List[Dict[str, Any]] = Field(default_factory=list)
    matched_installed_apps: List[AppInfo] = Field(default_factory=list)
    uninstalled_apps: List[AppInfo] = Field(default_factory=list)
    matched_capabilities: List[WindowsCapability] = Field(default_factory=list)
    device_state: Optional[Any] = None

    def to_prompt_text(self) -> str:
        """Render a concise, high-density context block for System 2 planning."""
        lines = []
        if self.device_state is not None and hasattr(self.device_state, "to_prompt_text"):
            lines.append(self.device_state.to_prompt_text())

        lines.append("[Windows Desktop Context]")

        # 1. Active Windows
        if self.active_windows:
            lines.append("Running Windows:")
            for w in self.active_windows[:5]:
                focus = " [Focused]" if w.get("is_focused") else ""
                lines.append(f"  * \"{w.get('title')}\" (process: {w.get('process')}, HWND: {w.get('hwnd')}){focus}")
        else:
            lines.append("Running Windows: None detected.")

        # 2. Matched Installed Apps
        if self.matched_installed_apps:
            lines.append("Installed Apps matching request:")
            for a in self.matched_installed_apps[:4]:
                state = " (Currently Open)" if a.is_running else ""
                loc = f" at {a.executable}" if a.executable else (f" (AppID: {a.app_id})" if a.app_id else "")
                lines.append(f"  * \"{a.name}\"{loc}{state}")

        # 3. Uninstalled Apps with Web Fallbacks
        if self.uninstalled_apps:
            lines.append("Not Installed locally (Web alternative available):")
            for u in self.uninstalled_apps[:3]:
                web = f" -> Web alternative: {u.web_fallback}" if u.web_fallback else ""
                lines.append(f"  * \"{u.name}\" is NOT installed{web}")

        # 4. Matched Capabilities / Settings
        if self.matched_capabilities:
            lines.append("Windows System Capabilities & Settings:")
            for c in self.matched_capabilities[:4]:
                lines.append(f"  * {c.name}: `{c.uri_or_command}` ({c.description})")

        return "\n".join(lines)


# ======================================================= Web Fallback Database

POPULAR_APP_WEB_FALLBACKS: Dict[str, Dict[str, Any]] = {
    "spotify": {"name": "Spotify", "url": "https://open.spotify.com", "keywords": ["spotify", "music", "songs"]},
    "slack": {"name": "Slack", "url": "https://app.slack.com", "keywords": ["slack", "team chat"]},
    "whatsapp": {"name": "WhatsApp", "url": "https://web.whatsapp.com", "keywords": ["whatsapp", "chat", "messages"]},
    "discord": {"name": "Discord", "url": "https://discord.com/app", "keywords": ["discord", "voice", "community"]},
    "telegram": {"name": "Telegram", "url": "https://web.telegram.org", "keywords": ["telegram", "chat"]},
    "notion": {"name": "Notion", "url": "https://www.notion.so", "keywords": ["notion", "notes", "workspace"]},
    "figma": {"name": "Figma", "url": "https://www.figma.com", "keywords": ["figma", "design", "ui"]},
    "trello": {"name": "Trello", "url": "https://trello.com", "keywords": ["trello", "kanban", "boards"]},
    "zoom": {"name": "Zoom", "url": "https://zoom.us/join", "keywords": ["zoom", "meeting", "call"]},
    "youtube": {"name": "YouTube", "url": "https://www.youtube.com", "keywords": ["youtube", "video", "videos"]},
    "maps": {"name": "Google Maps", "url": "https://maps.google.com", "keywords": ["maps", "google maps", "directions"]},
    "github": {"name": "GitHub", "url": "https://github.com", "keywords": ["github", "code", "repos"]},
    "gmail": {"name": "Gmail", "url": "https://mail.google.com", "keywords": ["gmail", "email", "mail"]},
    "outlook": {"name": "Outlook Web", "url": "https://outlook.live.com", "keywords": ["outlook", "email"]},
    "twitter": {"name": "X / Twitter", "url": "https://x.com", "keywords": ["twitter", "x", "social"]},
    "reddit": {"name": "Reddit", "url": "https://www.reddit.com", "keywords": ["reddit", "forum"]},
}


# ================================================ Built-in Windows Capabilities

WINDOWS_CAPABILITIES: List[WindowsCapability] = [
    WindowsCapability(
        name="Sound & Volume Settings",
        keywords=["sound", "volume", "audio", "speaker", "headphones", "mute", "microphone"],
        uri_or_command="ms-settings:sound",
        description="Windows audio output, volume sliders, and input devices",
    ),
    WindowsCapability(
        name="Display & Resolution Settings",
        keywords=["display", "resolution", "monitor", "screen", "brightness", "scaling", "hdr", "night light"],
        uri_or_command="ms-settings:display",
        description="Monitor resolution, scaling, brightness, and multiple displays",
    ),
    WindowsCapability(
        name="Network & Wi-Fi Settings",
        keywords=["wifi", "wi-fi", "network", "internet", "ethernet", "ip", "dns", "vpn", "proxy", "airplane mode"],
        uri_or_command="ms-settings:network",
        description="Wi-Fi connections, network adapters, and internet settings",
    ),
    WindowsCapability(
        name="Bluetooth & Devices",
        keywords=["bluetooth", "pair", "mouse", "keyboard", "controller", "headphones", "printer", "usb"],
        uri_or_command="ms-settings:bluetooth",
        description="Bluetooth pairing, printers, mice, keyboards, and USB devices",
    ),
    WindowsCapability(
        name="Installed Apps & Features",
        keywords=["installed apps", "uninstall", "add remove programs", "apps features", "programs"],
        uri_or_command="ms-settings:appsfeatures",
        description="List of installed software, uninstall apps, default apps",
    ),
    WindowsCapability(
        name="Windows Update",
        keywords=["windows update", "update", "patch", "check for updates"],
        uri_or_command="ms-settings:windowsupdate",
        description="Check for Windows OS updates and drivers",
    ),
    WindowsCapability(
        name="Battery & Power Settings",
        keywords=["battery", "power", "sleep", "energy", "screen timeout", "power plan"],
        uri_or_command="ms-settings:powersleep",
        description="Battery usage, sleep timeout, power saving modes",
    ),
    WindowsCapability(
        name="Notifications & Focus Assist",
        keywords=["notification", "notifications", "do not disturb", "focus assist", "alerts"],
        uri_or_command="ms-settings:notifications",
        description="Notification banners, quiet hours, and alerts",
    ),
    WindowsCapability(
        name="Personalization & Background",
        keywords=["wallpaper", "background", "theme", "dark mode", "light mode", "colors"],
        uri_or_command="ms-settings:personalization",
        description="Desktop background, dark/light theme, accent colors",
    ),
    WindowsCapability(
        name="Task Manager",
        keywords=["task manager", "processes", "kill process", "cpu usage", "ram usage", "performance"],
        uri_or_command="taskmgr.exe",
        description="View CPU/RAM usage, kill frozen apps, view running services",
        category="utility",
    ),
    WindowsCapability(
        name="File Explorer",
        keywords=["file explorer", "files", "folder", "directory", "my computer", "this pc"],
        uri_or_command="explorer.exe",
        description="Browse drives, documents, downloads, and files",
        category="utility",
    ),
    WindowsCapability(
        name="Windows Terminal / Command Prompt",
        keywords=["terminal", "command prompt", "cmd", "shell", "powershell", "console"],
        uri_or_command="wt.exe",
        description="Command-line shell for scripting, developer tools, and commands",
        category="utility",
    ),
    WindowsCapability(
        name="Snipping Tool / Screenshot",
        keywords=["screenshot", "snipping tool", "screen clip", "capture screen"],
        uri_or_command="snippingtool.exe",
        description="Take screen snips, rectangular captures, or full screenshots",
        category="utility",
    ),
    WindowsCapability(
        name="Calculator",
        keywords=["calculator", "calc", "math", "add numbers", "convert units"],
        uri_or_command="calc.exe",
        description="Perform arithmetic, scientific calculations, and unit conversions",
        category="utility",
    ),
    WindowsCapability(
        name="Notepad",
        keywords=["notepad", "text editor", "notes", "write note", "plain text"],
        uri_or_command="notepad.exe",
        description="Lightweight plain text editor",
        category="utility",
    ),
]


# ==================================================== Core WindowsContextEngine

class WindowsContextEngine:
    """Discovers, caches, and indexes Windows installed applications and capabilities."""

    def __init__(self, cache_ttl_seconds: float = 300.0) -> None:
        self.cache_ttl = cache_ttl_seconds
        self._lock = threading.Lock()
        self._last_scan: float = 0.0
        self._app_catalog: Dict[str, AppInfo] = {}  # key: normalized name

    # ------------------------------------------------------------- Discovery

    def refresh_catalog(self, force: bool = False) -> None:
        now = time.time()
        with self._lock:
            if not force and self._app_catalog and (now - self._last_scan < self.cache_ttl):
                return
            catalog: Dict[str, AppInfo] = {}

            # 1. Standard Built-in Windows Utilities
            standards = [
                ("Notepad", ["notepad", "text editor"], "notepad.exe"),
                ("Calculator", ["calculator", "calc"], "calc.exe"),
                ("File Explorer", ["explorer", "file explorer", "files"], "explorer.exe"),
                ("Task Manager", ["task manager", "taskmgr"], "taskmgr.exe"),
                ("Windows Terminal", ["terminal", "windows terminal", "wt"], "wt.exe"),
                ("Command Prompt", ["cmd", "command prompt"], "cmd.exe"),
                ("PowerShell", ["powershell", "pwsh"], "powershell.exe"),
                ("Paint", ["paint", "mspaint"], "mspaint.exe"),
                ("Registry Editor", ["regedit", "registry editor"], "regedit.exe"),
                ("Snipping Tool", ["snipping tool", "snippingtool"], "snippingtool.exe"),
                ("Microsoft Edge", ["edge", "microsoft edge", "msedge"], "msedge.exe"),
                ("Google Chrome", ["chrome", "google chrome"], "chrome.exe"),
                ("Mozilla Firefox", ["firefox", "mozilla firefox"], "firefox.exe"),
                ("Brave", ["brave", "brave browser"], "brave.exe"),
                ("Visual Studio Code", ["code", "vscode", "vs code"], "Code.exe"),
            ]
            for name, aliases, exe in standards:
                # Check if executable exists or is in PATH
                found_path = shutil.which(exe)
                norm_key = name.lower()
                catalog[norm_key] = AppInfo(
                    name=name,
                    aliases=aliases,
                    executable=found_path or exe,
                    is_installed=bool(found_path or self._resolve_known_path(exe)),
                    source="system",
                )

            # 2. Start Menu Shortcuts (.lnk)
            if sys.platform == "win32":
                start_paths = [
                    os.path.expandvars(r"%ProgramData%\Microsoft\Windows\Start Menu\Programs\**\*.lnk"),
                    os.path.expandvars(r"%APPDATA%\Microsoft\Windows\Start Menu\Programs\**\*.lnk"),
                ]
                for pattern in start_paths:
                    for lnk in glob.glob(pattern, recursive=True):
                        base = os.path.splitext(os.path.basename(lnk))[0]
                        low = base.lower()
                        if low.startswith("uninstall") or "help" in low or "readme" in low:
                            continue
                        if low not in catalog:
                            catalog[low] = AppInfo(
                                name=base,
                                aliases=[low, re.sub(r"[\s\-_]+", "", low)],
                                executable=lnk,
                                is_installed=True,
                                source="start_menu",
                            )

            # 3. Registry App Paths (HKLM and HKCU)
            if sys.platform == "win32":
                import winreg
                for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                    try:
                        key_p = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"
                        with winreg.OpenKey(root, key_p) as k:
                            num = winreg.QueryInfoKey(k)[0]
                            for i in range(num):
                                try:
                                    sub = winreg.EnumKey(k, i)
                                    with winreg.OpenKey(k, sub) as sk:
                                        val, _ = winreg.QueryValueEx(sk, "")
                                        if val:
                                            p = val.strip('\"\'')
                                            stem = os.path.splitext(sub)[0].lower()
                                            if stem not in catalog:
                                                catalog[stem] = AppInfo(
                                                    name=os.path.splitext(sub)[0],
                                                    aliases=[stem],
                                                    executable=p,
                                                    is_installed=os.path.exists(p),
                                                    source="registry",
                                                )
                                except OSError:
                                    pass
                    except OSError:
                        pass

            self._app_catalog = catalog
            self._last_scan = now

    def _resolve_known_path(self, exe: str) -> Optional[str]:
        if sys.platform != "win32":
            return None
        candidates = [
            rf"C:\Program Files\Google\Chrome\Application\{exe}",
            rf"C:\Program Files (x86)\Google\Chrome\Application\{exe}",
            rf"C:\Program Files\Microsoft\Edge\Application\{exe}",
            rf"C:\Program Files (x86)\Microsoft\Edge\Application\{exe}",
            os.path.expandvars(rf"%LOCALAPPDATA%\Programs\Microsoft VS Code\{exe}"),
            os.path.expandvars(rf"%PROGRAMFILES%\{exe}"),
            os.path.expandvars(rf"%PROGRAMFILES(X86)%\{exe}"),
            os.path.expandvars(rf"%WINDIR%\System32\{exe}"),
        ]
        for c in candidates:
            if os.path.exists(c):
                return c
        return None

    # ------------------------------------------------------------- Queries

    def is_installed(self, app_name: str) -> Tuple[bool, Optional[AppInfo]]:
        """Determine whether an application is installed on this system."""
        self.refresh_catalog()
        q = app_name.strip().lower()
        clean_q = re.sub(r"[\s\-_]+", "", q)

        # 1. Exact match in catalog
        if q in self._app_catalog:
            app = self._app_catalog[q]
            return app.is_installed, app

        # 2. Alias match
        for app in self._app_catalog.values():
            if any(a.lower() == q or re.sub(r"[\s\-_]+", "", a.lower()) == clean_q for a in app.aliases):
                return app.is_installed, app

        # 3. Substring match
        for app in self._app_catalog.values():
            if q in app.name.lower() or app.name.lower() in q:
                return app.is_installed, app

        # 4. Check if it's in PATH or executable directly
        which_p = shutil.which(f"{q}.exe") or shutil.which(q)
        if which_p and os.path.exists(which_p):
            info = AppInfo(name=app_name, aliases=[q], executable=which_p, is_installed=True)
            return True, info

        return False, None

    def get_web_fallback(self, app_name: str) -> Optional[str]:
        """Return web fallback URL if the app is a known service with a web app."""
        q = app_name.strip().lower()
        words = set(re.findall(r"\w+", q))
        for key, data in POPULAR_APP_WEB_FALLBACKS.items():
            if key == q or key in words:
                return data["url"]
            for kw in data["keywords"]:
                if kw == q or kw in words or (len(kw) >= 4 and kw in q):
                    return data["url"]
        return None

    # ------------------------------------------------------------- Retrieval (RAG)

    def retrieve_context(self, query: str, limit: int = 5) -> WindowsContextResult:
        """Fast lexical & fuzzy retrieval of installed apps, windows, and capabilities."""
        self.refresh_catalog()
        q = query.strip().lower()
        words = set(re.findall(r"\w+", q))

        # 1. Query Active Windows
        active_windows: List[Dict[str, Any]] = []
        try:
            fg = windows.get_foreground_window()
            for w in windows.list_windows():
                title = str(w.get("title") or "")
                if not title or "servo copilot" in title.lower():
                    continue
                hwnd = int(w.get("hwnd") or 0)
                active_windows.append({
                    "hwnd": hwnd,
                    "title": title,
                    "process": w.get("process") or w.get("process_name") or "",
                    "is_focused": hwnd == fg,
                })
        except Exception as e:
            logger.debug("Failed listing windows for context: %s", e)

        # 2. Match Installed Apps
        matched_installed: List[AppInfo] = []
        uninstalled: List[AppInfo] = []

        # Check explicitly referenced app words
        for app in self._app_catalog.values():
            app_words = set(re.findall(r"\w+", app.name.lower()))
            overlap = words.intersection(app_words)
            if overlap or app.name.lower() in q:
                if app.is_installed:
                    # Check if running
                    running_w = [w for w in active_windows if app.name.lower() in w["title"].lower() or (app.executable and os.path.basename(app.executable).lower() in w["process"].lower())]
                    app.is_running = bool(running_w)
                    if running_w:
                        app.hwnd = running_w[0]["hwnd"]
                    matched_installed.append(app)

        # 3. Check popular web fallbacks for uninstalled queries
        for key, data in POPULAR_APP_WEB_FALLBACKS.items():
            match = (key == q or key in words or any(kw == q or kw in words or (len(kw) >= 4 and kw in q) for kw in data["keywords"]))
            if match:
                installed, info = self.is_installed(key)
                if not installed:
                    uninstalled.append(AppInfo(
                        name=data["name"],
                        aliases=[key],
                        is_installed=False,
                        web_fallback=data["url"],
                        source="web",
                    ))

        # 4. Match Windows Capabilities / Settings
        matched_caps: List[WindowsCapability] = []
        for cap in WINDOWS_CAPABILITIES:
            score = 0
            if cap.name.lower() in q:
                score += 5
            for kw in cap.keywords:
                if kw in q:
                    score += 2
                elif any(w == kw for w in words):
                    score += 1
            if score > 0:
                matched_caps.append(cap)

        matched_caps.sort(key=lambda c: sum(1 for kw in c.keywords if kw in q), reverse=True)

        dev_state = None
        try:
            from servo.perception.device_state import get_device_state
            dev_state = get_device_state()
        except Exception as e:
            logger.debug("Failed querying device state: %s", e)

        return WindowsContextResult(
            query=query,
            active_windows=active_windows[:limit],
            matched_installed_apps=matched_installed[:limit],
            uninstalled_apps=uninstalled[:limit],
            matched_capabilities=matched_caps[:limit],
            device_state=dev_state,
        )


# Global singleton instance
windows_context = WindowsContextEngine()
