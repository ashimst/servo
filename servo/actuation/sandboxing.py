"""Input Sandboxing and Safety Guardrails.


The sandbox *rejects* input that would land outside the target window instead of
clamping it: clamping silently turns "click element X" into "click whatever is at
the edge of the window", which is a different action than the one that was gated.
"""

from __future__ import annotations

import ctypes
import re
import sys
import time
from typing import Iterable, List, Tuple

from servo.perception import windows
from servo.perception.viewport import ViewportTransform


class SandboxViolationException(Exception):
    """Raised when an action attempts an out-of-bounds click or prohibited input."""


class FailsafeException(Exception):
    """Raised when the operator triggers an emergency interrupt (e.g. corner-slam or Esc)."""


# Canonical key names. Anything not listed maps to itself (lowercased).
KEY_ALIASES = {
    # Punctuation by name: "press ctrl+comma" survives every tool-calling model, "ctrl+," doesn't.
    "comma": ",", "period": ".", "dot": ".", "slash": "/", "backslash": "\\", "semicolon": ";",
    "minus": "-", "dash": "-", "equals": "=", "plus": "+", "backtick": "`", "quote": "'",
    "control": "ctrl",
    "ctl": "ctrl",
    "lctrl": "ctrl",
    "rctrl": "ctrl",
    "menu": "alt",
    "lalt": "alt",
    "ralt": "alt",
    "altgr": "alt",
    "option": "alt",
    "lshift": "shift",
    "rshift": "shift",
    "windows": "win",
    "lwin": "win",
    "rwin": "win",
    "meta": "win",
    "super": "win",
    "cmd": "win",
    "command": "win",
    "delete": "del",
    "escape": "esc",
    "return": "enter",
}

# A combo is blocked if it CONTAINS any of these sets (so adding shift to alt+f4
# does not get around the rule). Any combo involving the Windows key is blocked
# because every Win shortcut acts on the OS shell, never on the target window
# (Win+R alone is an arbitrary-command launcher).
HOTKEY_BLACKLIST: Tuple[frozenset, ...] = (
    frozenset({"win"}),
    frozenset({"alt", "f4"}),
    frozenset({"alt", "tab"}),
    frozenset({"alt", "esc"}),
    frozenset({"ctrl", "esc"}),
    frozenset({"ctrl", "alt", "del"}),
    frozenset({"ctrl", "shift", "esc"}),
)


def normalize_keys(keys: Iterable[str]) -> List[str]:
    out = []
    for k in keys:
        lk = k.strip().lower()
        out.append(KEY_ALIASES.get(lk, lk))
    return out


def split_hotkeys(keys: str) -> List[str]:
    """Split a hotkey string like 'ctrl+shift+s', 'ctrl+,', 'ctrl++', 'ctrl+-' into individual key names.

    Preserves trailing/standalone punctuation keys like ',', '+', '-', '[', ']' without
    treating them as delimiter characters.
    """
    s = keys.strip()
    if not s:
        return []
    if len(s) == 1:
        return [s]
    last_char = s[-1]
    if not last_char.isalnum():
        prefix = s[:-1].rstrip("+ -,\t")
        if prefix:
            prefix_tokens = [k.strip() for k in re.split(r"[\+\s,\-]+", prefix) if k.strip()]
            return prefix_tokens + [last_char]
        return [last_char]
    return [k.strip() for k in re.split(r"[\+\s,\-]+", s) if k.strip()]


def is_hotkey_blacklisted(keys: List[str]) -> bool:
    """Check if key combination contains any prohibited combination."""
    combo = set(normalize_keys(keys))
    return any(blocked <= combo for blocked in HOTKEY_BLACKLIST)


def check_target_allowed(hwnd: int, mode: str) -> None:
    """Enforce harness-mode allowlists (spec: 'strictly sandboxed to a whitelisted HWND
    or process name'). Fails closed: harness mode with no allowlist configured refuses
    to act, because 'no allowlist' would otherwise mean 'every window on the desktop'.
    """
    if mode != "harness":
        return
    from servo.config import settings

    hwnds = set(settings.whitelisted_hwnds)
    procs = {p.lower() for p in settings.whitelisted_process_names}
    if not hwnds and not procs:
        raise SandboxViolationException(
            "Harness mode requires SERVO_WHITELISTED_HWNDS or SERVO_WHITELISTED_PROCESS_NAMES to be set."
        )
    if hwnd in hwnds:
        return
    proc = windows.get_process_name(hwnd)
    if proc and proc in procs:
        return
    raise SandboxViolationException(
        f"HWND {hwnd} (process '{proc or 'unknown'}') is not on the harness allowlist."
    )


def _pid_of(hwnd: int) -> int:
    from ctypes import wintypes
    pid = wintypes.DWORD()
    ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def app_pids(hwnd: int) -> set:
    """Processes that make up the app in ``hwnd``: the window's own, plus those of its child
    windows. Packaged apps (Settings, Clock, Calculator, Photos) run in an
    ApplicationFrameHost.exe frame while their content, and so their menus, dropdowns and
    flyouts, belong to the app's own process (SystemSettings.exe, Time.exe, ...)."""
    pids = {_pid_of(hwnd)}
    if sys.platform != "win32" or not hwnd:
        return pids
    from ctypes import wintypes

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _child(h, _l):  # type: ignore[no-untyped-def]
        pids.add(_pid_of(int(h or 0)))
        return True

    try:
        ctypes.windll.user32.EnumChildWindows(hwnd, _child, 0)
    except Exception:
        pass
    pids.discard(0)
    return pids


# The Windows shell's own windows. They belong to explorer.exe, like every File Explorer
# window, and the taskbar is a borderless popup, so without this File Explorer "owned" the
# whole taskbar and its buttons led the planner's screen description.
_SHELL_CLASSES = {"Shell_TrayWnd", "Shell_SecondaryTrayWnd", "Progman", "WorkerW", "NotifyIconOverflowWindow",
                  "TopLevelWindowForOverflowXamlIsland", "Windows.UI.Core.CoreWindow"}


def _class_name(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    ctypes.windll.user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def is_popup_of(hit: int, target: int) -> bool:
    """True when ``hit`` is a menu, dropdown list or flyout that belongs to ``target``.

    Menus and dropdowns are separate top-level windows. Treating them as "another window
    covering the target" refused every click inside Explorer's View menu, a combo box's
    list or a context menu. Same app (see ``app_pids``) AND (owned by the target, or a
    borderless popup) qualifies; a second full window of the same app does not.
    """
    if sys.platform != "win32" or not hit or not target:
        return False
    try:
        if _pid_of(hit) not in app_pids(target) or _class_name(hit) in _SHELL_CLASSES:
            return False
        user32 = ctypes.windll.user32
        owner = hit
        for _ in range(8):                       # GW_OWNER chain
            owner = user32.GetWindow(owner, 4)
            if not owner:
                break
            if windows.get_root_window(owner) == target or owner == target:
                return True
        style = user32.GetWindowLongW(hit, -16) & 0xFFFFFFFF   # GWL_STYLE
        WS_POPUP, WS_CAPTION = 0x80000000, 0x00C00000
        return bool(style & WS_POPUP) and (style & WS_CAPTION) != WS_CAPTION
    except Exception:
        return False


def activate_window(hwnd: int) -> bool:
    """Bring a window to the foreground; True only if it verifiably got there.

    Windows frequently refuses SetForegroundWindow (foreground lock). Synthetic
    keystrokes always go to the foreground window, so if activation failed we
    must not type: the keys would land in some other application.
    """
    if sys.platform != "win32":
        return True
    target = windows.get_root_window(hwnd)
    if windows.get_foreground_window() == target:
        return True
    user32 = ctypes.windll.user32

    def became_foreground(tries: int = 15) -> bool:
        for _ in range(tries):
            if windows.get_foreground_window() == target:
                return True
            time.sleep(0.02)
        return False

    if user32.IsIconic(target):
        user32.ShowWindow(target, 9)  # SW_RESTORE
    user32.SetForegroundWindow(target)
    if became_foreground():
        return True
    # Foreground lock: a process that didn't receive the last input may not take
    # focus. A synthetic Alt tap lifts the lock (documented SetForegroundWindow rule).
    user32.keybd_event(0x12, 0, 0, 0)        # VK_MENU down
    user32.keybd_event(0x12, 0, 2, 0)        # VK_MENU up
    user32.SetForegroundWindow(target)
    user32.BringWindowToTop(target)
    if became_foreground():
        return True
    # Last resort: share the foreground window's input state for one call.
    fg = windows.get_foreground_window()
    fg_thread = user32.GetWindowThreadProcessId(fg, None)
    me = ctypes.windll.kernel32.GetCurrentThreadId()
    if fg_thread and fg_thread != me and user32.AttachThreadInput(me, fg_thread, True):
        try:
            user32.SetForegroundWindow(target)
            user32.BringWindowToTop(target)
        finally:
            user32.AttachThreadInput(me, fg_thread, False)
    return became_foreground(25)


class InputSandbox:
    """Enforces target window boundary checks and hotkey restrictions."""

    def __init__(self, hwnd: int) -> None:
        self.hwnd = hwnd
        self.viewport = ViewportTransform(hwnd)

    def validate_target(self, screen_x: int, screen_y: int) -> Tuple[int, int]:
        """Validate that the target point lies inside the client area; raise otherwise."""
        self.viewport.refresh()
        b = self.viewport.bounds

        if b.client_right - b.client_left <= 0 or b.client_bottom - b.client_top <= 0:
            raise SandboxViolationException(f"Target window HWND {self.hwnd} has invalid zero client dimensions.")

        if not self.viewport.is_point_inside_client(screen_x, screen_y) and not (
                sys.platform == "win32"
                and is_popup_of(windows.window_at_point(screen_x, screen_y), windows.get_root_window(self.hwnd))):
            # (A point in one of the app's own menus/dropdown lists is fine: they float
            # outside the window's client area.)
            raise SandboxViolationException(
                f"Target point ({screen_x}, {screen_y}) is outside the client area of HWND {self.hwnd}; "
                "element is likely scrolled out of view or the window moved."
            )
        return screen_x, screen_y

    def point_hits_target(self, screen_x: int, screen_y: int) -> bool:
        """True if a physical click at this point would be delivered to the target window
        (i.e. no other window is covering it)."""
        if sys.platform != "win32":
            return True
        hit = windows.window_at_point(screen_x, screen_y)
        target = windows.get_root_window(self.hwnd)
        return hit == target or is_popup_of(hit, target)

    def validate_hotkey(self, keys: List[str]) -> None:
        """Validate that proposed hotkey does not violate the safety blacklist."""
        if is_hotkey_blacklisted(keys):
            raise SandboxViolationException(f"Hotkey combination {keys} is blacklisted by Servo safety policy.")

    def ensure_target_in_foreground(self) -> None:
        """Bring the target to the foreground and VERIFY it got there.

        Synthetic keystrokes always go to the foreground window, so if activation failed
        we must not type: the keys would land in some other application.
        """
        if activate_window(self.hwnd):
            return
        raise SandboxViolationException(
            f"Could not bring HWND {self.hwnd} to the foreground; refusing to send keystrokes to another window."
        )
