"""Web tasks in the user's own browser (Dia, Arc, Chrome, Edge, ...).

Why not Playwright: it drives a separate browser with its own profile, so pages open
where the user isn't looking, without their logins. And Chromium (136+) refuses remote
debugging on a normal profile, so attaching to the user's real session isn't possible.

So this module does what a person does: focus the browser, open a new tab (Ctrl+T, so
the current page is left alone), type the address, press Enter, then read the address
bar to verify. Command-line URLs are not used: some browsers (Dia) ignore them.
After navigation the page is an ordinary window, driven through UI Automation like any
desktop app.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import sys
import time
from typing import Optional, Tuple
from urllib.parse import quote_plus, urlparse

from servo.config import settings

logger = logging.getLogger(__name__)

# name the user might say -> process name
KNOWN_BROWSERS = {
    "dia": "dia.exe", "arc": "arc.exe", "chrome": "chrome.exe", "google chrome": "chrome.exe",
    "edge": "msedge.exe", "microsoft edge": "msedge.exe", "msedge": "msedge.exe",
    "firefox": "firefox.exe", "brave": "brave.exe", "opera": "opera.exe", "vivaldi": "vivaldi.exe",
}
BROWSER_PROCESSES = frozenset(KNOWN_BROWSERS.values())
_BROWSER_NAME_RE = "|".join(sorted((re.escape(k) for k in KNOWN_BROWSERS), key=len, reverse=True))

# Sites people name without a domain, plus how to search them by URL (far more reliable
# than finding and clicking a site's search box).
SITES = {
    "youtube": ("https://www.youtube.com", "https://www.youtube.com/results?search_query={q}"),
    "spotify": ("https://open.spotify.com", "https://open.spotify.com/search/{q}"),
    "gmail": ("https://mail.google.com", "https://mail.google.com/mail/u/0/#search/{q}"),
    "google": ("https://www.google.com", "https://www.google.com/search?q={q}"),
    "bing": ("https://www.bing.com", "https://www.bing.com/search?q={q}"),
    "wikipedia": ("https://en.wikipedia.org", "https://en.wikipedia.org/w/index.php?search={q}"),
    "github": ("https://github.com", "https://github.com/search?q={q}"),
    "amazon": ("https://www.amazon.com", "https://www.amazon.com/s?k={q}"),
    "maps": ("https://www.google.com/maps", "https://www.google.com/maps/search/{q}"),
    "google maps": ("https://www.google.com/maps", "https://www.google.com/maps/search/{q}"),
    "facebook": ("https://www.facebook.com", "https://www.facebook.com/search/top?q={q}"),
    "gemini": ("https://gemini.google.com", ""),
    "chatgpt": ("https://chatgpt.com", ""),
    "linkedin": ("https://www.linkedin.com", "https://www.linkedin.com/search/results/all/?keywords={q}"),
    "reddit": ("https://www.reddit.com", "https://www.reddit.com/search/?q={q}"),
    "netflix": ("https://www.netflix.com", "https://www.netflix.com/search?q={q}"),
    "outlook": ("https://outlook.live.com", ""),
    "whatsapp": ("https://web.whatsapp.com", ""),
}


def site_url(name: str) -> Optional[str]:
    hit = SITES.get(name.strip().lower())
    return hit[0] if hit else None


def site_search_url(site: str, query: str) -> Optional[str]:
    hit = SITES.get(site.strip().lower())
    if not hit or not hit[1]:
        return None
    return hit[1].format(q=quote_plus(query.strip()))


def browser_process(name: str) -> Optional[str]:
    return KNOWN_BROWSERS.get(name.strip().lower())


def is_browser_name(name: str) -> bool:
    return browser_process(name) is not None


_PREF_PATTERNS = [
    rf"^(?:the\s+)?({_BROWSER_NAME_RE})\s+is\s+(?:my|the)\s+(?:default|preferred|main|usual)\s+browser\.?$",
    rf"^(?:i\s+)?(?:use|prefer)\s+({_BROWSER_NAME_RE})(?:\s+(?:as\s+my\s+browser|for\s+(?:the\s+)?web|by\s+default))?\.?$",
    rf"^(?:set|make)\s+({_BROWSER_NAME_RE})\s+(?:as\s+)?(?:my|the)\s+(?:default\s+|preferred\s+)?browser\.?$",
    rf"^always\s+use\s+({_BROWSER_NAME_RE})(?:\s+for\s+(?:the\s+)?web)?\.?$",
    rf"^my\s+(?:default\s+|preferred\s+)?browser\s+is\s+({_BROWSER_NAME_RE})\.?$",
]


def parse_browser_preference(text: str) -> Optional[str]:
    """'dia is my default browser' -> 'dia'. A stated preference, not a task."""
    t = text.strip()
    for pat in _PREF_PATTERNS:
        m = re.match(pat, t, re.I)
        if m:
            return m.group(1).lower()
    return None


# ------------------------------------------------------------- which browser

def windows_default_browser() -> Optional[str]:
    """Process name of the Windows default https handler, e.g. 'arc.exe'."""
    if sys.platform != "win32":
        return None
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice") as k:
            prog = winreg.QueryValueEx(k, "ProgId")[0]
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, prog + r"\shell\open\command") as k:
            cmd = winreg.QueryValueEx(k, "")[0]
        m = re.search(r"([^\\\"/]+\.exe)", cmd, re.I)
        return m.group(1).lower() if m else None
    except OSError:
        return None


def choose_browser(explicit: str = "", recent: str = "") -> Tuple[str, str]:
    """(app name to launch, why). Order: named in the request > the browser this run
    just opened > the user's saved preference > the Windows default > Edge."""
    if explicit and is_browser_name(explicit):
        return explicit.lower(), f"you named {explicit}"
    if recent and is_browser_name(recent):
        return recent.lower(), f"{recent} is the browser this request opened"
    if settings.preferred_browser and is_browser_name(settings.preferred_browser):
        return settings.preferred_browser.lower(), f"{settings.preferred_browser} is your preferred browser"
    proc = windows_default_browser()
    if proc:
        name = next((n for n, p in KNOWN_BROWSERS.items() if p == proc), proc[:-4])
        return name, f"{name} is the Windows default browser"
    return "edge", "no preference or default found"


# ---------------------------------------------------------------- URL safety

def url_problem(url: str, goal: str = "") -> Optional[str]:
    """Why Servo must not open this URL on its own initiative, or None.

    Only http(s). Never loopback, private-network or link-local hosts (that is where
    Servo's own copilot and other local services live) unless the user's own request
    names that host.
    """
    try:
        u = urlparse(url)
    except ValueError:
        return f"Unparsable address: {url}"
    if u.scheme not in ("http", "https"):
        return f"Only web addresses are allowed, not {u.scheme or 'this'}: links."
    host = (u.hostname or "").lower().rstrip(".")
    if not host:
        return "The address has no host."
    local = host in ("localhost",) or host.endswith(".localhost") or host.endswith(".local")
    try:
        ip = ipaddress.ip_address(host)
        local = local or ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_unspecified
    except ValueError:
        pass
    if local and host not in goal.lower():
        return f"{host} is on this computer or local network; Servo opens it only if you ask for it by name."
    return None


# ------------------------------------------------------------------ keyboard

_SEND_KEYS_SPECIAL = re.compile(r"([{}+^%~()\[\]])")


def escape_send_keys(text: str) -> str:
    """Escape pywinauto send_keys metacharacters so text is typed literally."""
    return _SEND_KEYS_SPECIAL.sub(r"{\1}", text)


def address_bar_text(hwnd: int) -> str:
    """Text of the browser's address bar (the first Edit in the window chrome), or ''."""
    try:
        from servo.perception.uia_sensor import UIASensor
        scene = UIASensor(hwnd).sense()
        edits = [e for e in scene.elements.values() if e.role == "Edit"]
        for e in edits:
            text = f"{e.value or ''} {e.label or ''}".strip()
            if text:
                return text
    except Exception as e:
        logger.debug("address bar read failed: %s", e)
    return ""


def navigate(hwnd: int, url: str, new_tab: bool = True, timeout: float = 12.0) -> Tuple[str, str, str]:
    """Open ``url`` in the browser window ``hwnd``. Returns (verdict, title, note).

    verdict: confirmed (address bar shows the host), unconfirmed (title changed but
    the address bar couldn't be read), failed (nothing changed).
    """
    from servo.actuation.needle_tools import NeedleToolSurface
    from servo.perception import windows

    before = windows.get_window_title(hwnd)
    host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    proc = windows.get_process_name(hwnd)
    tools = NeedleToolSurface(hwnd, fast_mode=True)
    tools.sandbox.ensure_target_in_foreground()
    tools.hotkey(["ctrl", "t"] if new_tab else ["ctrl", "l"])
    time.sleep(0.7)
    from pywinauto.keyboard import send_keys
    tools.sandbox.ensure_target_in_foreground()
    send_keys(escape_send_keys(url), with_spaces=True, pause=0.005)
    # An Enter sent right after typing is swallowed while the suggestion box opens
    # (seen in Dia), so pause first and press once more if the tab is still blank.
    time.sleep(0.5)
    send_keys("{ENTER}")

    def loaded(t: str) -> bool:
        return bool(t) and t != before and not _is_blank_tab_title(t)

    deadline = time.monotonic() + timeout
    retried = False
    title = before
    t0 = time.monotonic()
    while time.monotonic() < deadline:
        time.sleep(0.4)
        fg = windows.get_foreground_window()
        if fg and windows.get_process_name(fg) == proc:
            hwnd = fg
        title = windows.get_window_title(hwnd)
        if loaded(title):
            break
        if not retried and time.monotonic() - t0 > 2.5 and _is_blank_tab_title(title):
            tools.sandbox.ensure_target_in_foreground()
            send_keys("{ENTER}")
            retried = True
    bar = address_bar_text(hwnd).lower()
    if loaded(title) and host and host in bar:
        return "confirmed", title, ""
    if loaded(title):
        return "unconfirmed", title, f"A page loaded (“{title}”), but the address bar didn't show {host}."
    return "failed", title, f"The page didn't load: the tab still shows “{title or 'nothing'}”."


_BLANK_TAB = re.compile(r"(?:^|:\s*)(?:new tab|untitled|about:blank|start page|new page)(?:\s*[-–—].*)?$", re.I)


def _is_blank_tab_title(title: str) -> bool:
    return bool(_BLANK_TAB.search(title.strip()))
