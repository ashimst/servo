"""Windows Device and Operating System State Inspector.

Extracts real-time hardware, operating system, and system environment state
(< 15ms latency) to ground the RAG engine and System 2 with the exact live
context of the user's computer (RAM, Battery/Power, Display/DPI, Clipboard, OS Build).
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import logging
import os
import platform
import socket
import sys
from typing import Optional

from pydantic import BaseModel

logger = logging.getLogger("servo.perception.device_state")


# Win32 Memory Status Structure
class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", wintypes.DWORD),
        ("dwMemoryLoad", wintypes.DWORD),
        ("ullTotalPhys", ctypes.c_uint64),
        ("ullAvailPhys", ctypes.c_uint64),
        ("ullTotalPageFile", ctypes.c_uint64),
        ("ullAvailPageFile", ctypes.c_uint64),
        ("ullTotalVirtual", ctypes.c_uint64),
        ("ullAvailVirtual", ctypes.c_uint64),
        ("ullAvailExtendedVirtual", ctypes.c_uint64),
    ]


# Win32 System Power Status Structure
class _SYSTEM_POWER_STATUS(ctypes.Structure):
    _fields_ = [
        ("ACLineStatus", wintypes.BYTE),
        ("BatteryFlag", wintypes.BYTE),
        ("BatteryLifePercent", wintypes.BYTE),
        ("SystemStatusFlag", wintypes.BYTE),
        ("BatteryLifeTime", wintypes.DWORD),
        ("BatteryFullLifeTime", wintypes.DWORD),
    ]


class DeviceState(BaseModel):
    os_name: str
    architecture: str
    cpu_cores: int
    ram_total_gb: float
    ram_free_gb: float
    ram_load_percent: int
    battery_percent: Optional[int] = None
    is_charging: Optional[bool] = None
    screen_width: int = 1920
    screen_height: int = 1080
    dpi_scale: float = 1.0
    clipboard_preview: Optional[str] = None
    hostname: str = ""
    gpu_profile: str = "4GB VRAM Target"

    def to_prompt_text(self) -> str:
        """Formatted real-time device state for RAG and System 2 prompts."""
        lines = ["[Device & System State]"]
        lines.append(f"  * OS: {self.os_name} ({self.architecture}, {self.cpu_cores} CPU cores)")
        lines.append(f"  * Memory: {self.ram_free_gb:.1f} GB free of {self.ram_total_gb:.1f} GB ({self.ram_load_percent}% load)")
        if self.battery_percent is not None and self.battery_percent <= 100:
            power_mode = "Plugged in (Charging)" if self.is_charging else "On Battery"
            lines.append(f"  * Power: {self.battery_percent}% ({power_mode})")
        lines.append(f"  * Display: {self.screen_width}x{self.screen_height} (Scale: {int(self.dpi_scale * 100)}%)")
        if self.clipboard_preview:
            lines.append(f"  * Clipboard: \"{self.clipboard_preview}\"")
        return "\n".join(lines)


def get_device_state() -> DeviceState:
    """Query live Windows system state in < 15ms."""
    os_name = f"{platform.system()} {platform.release()} (Build {platform.version()})"
    arch = platform.machine()
    cores = os.cpu_count() or 4
    hostname = socket.gethostname()

    # 1. RAM / Memory
    ram_total = 8.0
    ram_free = 4.0
    ram_load = 50
    if sys.platform == "win32":
        try:
            m = _MEMORYSTATUSEX()
            m.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
                ram_total = m.ullTotalPhys / (1024 ** 3)
                ram_free = m.ullAvailPhys / (1024 ** 3)
                ram_load = m.dwMemoryLoad
        except Exception as e:
            logger.debug("Failed querying memory: %s", e)

    # 2. Battery / Power
    battery_pct: Optional[int] = None
    charging: Optional[bool] = None
    if sys.platform == "win32":
        try:
            sps = _SYSTEM_POWER_STATUS()
            if ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(sps)):
                if sps.BatteryLifePercent != 255:  # 255 = no battery / desktop
                    battery_pct = int(sps.BatteryLifePercent)
                    charging = sps.ACLineStatus == 1
        except Exception as e:
            logger.debug("Failed querying power: %s", e)

    # 3. Display Resolution & DPI
    width, height = 1920, 1080
    dpi_scale = 1.0
    if sys.platform == "win32":
        try:
            u32 = ctypes.windll.user32
            width = u32.GetSystemMetrics(0)
            height = u32.GetSystemMetrics(1)
            # Query DPI if available
            dpi_fn = getattr(u32, "GetDpiForSystem", None)
            if dpi_fn:
                dpi = dpi_fn()
                dpi_scale = dpi / 96.0
        except Exception as e:
            logger.debug("Failed querying display: %s", e)

    # 4. Clipboard Preview
    clip_preview: Optional[str] = None
    try:
        import pyperclip
        text = pyperclip.paste()
        if text and text.strip():
            clean = " ".join(text.split())
            clip_preview = clean[:90] + ("..." if len(clean) > 90 else "")
    except Exception:
        pass

    return DeviceState(
        os_name=os_name,
        architecture=arch,
        cpu_cores=cores,
        ram_total_gb=round(ram_total, 2),
        ram_free_gb=round(ram_free, 2),
        ram_load_percent=int(ram_load),
        battery_percent=battery_pct,
        is_charging=charging,
        screen_width=width,
        screen_height=height,
        dpi_scale=round(dpi_scale, 2),
        clipboard_preview=clip_preview,
        hostname=hostname,
    )
