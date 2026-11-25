"""General Windows skills, held out from tuning: toggles, checkboxes, dropdowns,
collapsed sections, menus, Save-As dialogs, file renames and reading the screen.

These tasks are not what Servo was built around and must not be special-cased: no
recipes, grammar rules or prompts may name them. They exist to measure whether
general improvements generalise. Each one records the setting it touches before the
run and restores it afterwards, and is checked independently (registry, power
settings, the file system, Calculator's display), never through Servo's own report.

    python -m servo.evals.task_suite --suite general
"""

from __future__ import annotations

import re
import socket
import subprocess
import time
from pathlib import Path
from typing import Any, List, Optional, Tuple

ADV = r"Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced"
PERSONALIZE = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"


# ------------------------------------------------------------------ registry / power

def _reg_get(path: str, name: str) -> Optional[int]:
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as k:
            return int(winreg.QueryValueEx(k, name)[0])
    except OSError:
        return None


def _reg_set(path: str, name: str, value: Optional[int]) -> None:
    import winreg
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, path) as k:
        if value is None:
            try:
                winreg.DeleteValue(k, name)
            except OSError:
                pass
        else:
            winreg.SetValueEx(k, name, 0, winreg.REG_DWORD, int(value))
    _broadcast_setting_change()


def _broadcast_setting_change() -> None:
    """Tell Explorer and apps that settings changed, so a restore shows up at once."""
    import ctypes
    for area in ("ImmersiveColorSet", "TraySettings", "Policy", None):
        ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x001A, 0, area, 0x0002, 1000, None)


def _screen_timeout_ac() -> Optional[int]:
    out = subprocess.run(["powercfg", "/q", "SCHEME_CURRENT", "SUB_VIDEO", "VIDEOIDLE"],
                         capture_output=True, text=True, timeout=15).stdout
    m = re.search(r"Current AC Power Setting Index:\s*0x([0-9a-fA-F]+)", out)
    return int(m.group(1), 16) if m else None


def _set_screen_timeout_ac(seconds: int) -> None:
    subprocess.run(["powercfg", "/setacvalueindex", "SCHEME_CURRENT", "SUB_VIDEO", "VIDEOIDLE", str(seconds)],
                   capture_output=True, timeout=15)
    subprocess.run(["powercfg", "/setactive", "SCHEME_CURRENT"], capture_output=True, timeout=15)


# ------------------------------------------------------------------ helpers

def _registry_task(task_cls: Any, tid: str, goal: str, path: str, name: str, start: int, want: int) -> Any:
    """A setting that must go from ``start`` to ``want``; restored to the user's value after."""
    def setup(ctx: Any) -> None:
        ctx.before[name] = _reg_get(path, name)
        _reg_set(path, name, start)

    def check(ctx: Any) -> Tuple[bool, str]:
        got = _reg_get(path, name)
        return got == want, f"{name} = {got} (wanted {want})"

    def teardown(ctx: Any) -> None:
        _reg_set(path, name, ctx.before.get(name))

    return task_cls(tid, goal, check, needs_llm=True, setup=setup, teardown=teardown, group="general")


def _calculator_mode() -> str:
    from pywinauto import Desktop
    from servo.daemon.copilot import find_app_window
    h = find_app_window("calculator")
    if not h:
        return ""
    for el in Desktop(backend="uia").window(handle=h).descendants():
        try:
            if el.element_info.automation_id == "Header":
                return el.window_text()
        except Exception:
            continue
    return ""


def tasks(task_cls: Any, scratch: Path) -> List[Any]:
    stamp = time.strftime("%H%M%S")
    box = scratch / "general"
    box.mkdir(parents=True, exist_ok=True)

    def setup_rename(ctx: Any) -> None:
        (box / "draft.txt").write_text("rename me", encoding="utf-8")
        (box / "final.txt").unlink(missing_ok=True)

    def check_rename(ctx: Any) -> Tuple[bool, str]:
        ok = (box / "final.txt").exists() and not (box / "draft.txt").exists()
        return ok, f"files now: {sorted(p.name for p in box.iterdir())}"

    save_name = f"ledger{stamp}.txt"

    def check_saved(ctx: Any) -> Tuple[bool, str]:
        f = box / save_name
        if not f.exists():
            return False, f"{f.name} not found; folder has {sorted(p.name for p in box.iterdir())}"
        text = f.read_text(encoding="utf-8", errors="replace")
        return f"ledger {stamp}" in text, f"{f.name} contains {text[:60]!r}"

    def setup_timeout(ctx: Any) -> None:
        ctx.before["timeout"] = _screen_timeout_ac()
        _set_screen_timeout_ac(300)

    def check_timeout(ctx: Any) -> Tuple[bool, str]:
        got = _screen_timeout_ac()
        return got == 600, f"screen off after {got}s on AC (wanted 600)"

    def teardown_timeout(ctx: Any) -> None:
        if ctx.before.get("timeout") is not None:
            _set_screen_timeout_ac(int(ctx.before["timeout"]))

    def check_calc_mode(ctx: Any) -> Tuple[bool, str]:
        mode = _calculator_mode()
        return "scientific" in mode.lower(), f"Calculator header: {mode!r}"

    def check_device_name(ctx: Any) -> Tuple[bool, str]:
        name = socket.gethostname()
        reply = str(getattr(ctx.outcome, "reply", "") or "")
        return name.lower() in reply.lower(), f"device name {name!r}; reply {reply[:120]!r}"

    T = task_cls
    return [
        _registry_task(T, "g_file_extensions", "show file name extensions in File Explorer",
                       ADV, "HideFileExt", start=1, want=0),
        _registry_task(T, "g_transparency", "turn off transparency effects",
                       PERSONALIZE, "EnableTransparency", start=1, want=0),
        _registry_task(T, "g_clock_seconds", "show seconds in the taskbar clock",
                       ADV, "ShowSecondsInSystemClock", start=0, want=1),
        T("g_screen_timeout", "make the screen turn off after 10 minutes when plugged in", check_timeout,
          needs_llm=True, setup=setup_timeout, teardown=teardown_timeout, group="general"),
        T("g_calc_scientific", "switch the calculator to scientific mode", check_calc_mode,
          needs_llm=True, group="general"),
        T("g_notepad_save_as", f"open notepad, write 'ledger {stamp}' and save it as {save_name} in {box}",
          check_saved, needs_llm=True, group="general"),
        T("g_rename_file", f"rename {box / 'draft.txt'} to final.txt", check_rename,
          needs_llm=True, setup=setup_rename, group="general"),
        T("g_device_name", "what is this PC's device name?", check_device_name, needs_llm=True, group="general"),
    ]
