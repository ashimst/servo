"""Servo configuration, runtime flags, DPI v2 awareness boot, and hardware probe."""

from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path
from typing import Literal
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def init_windows_dpi_awareness() -> bool:
    """Initialize Windows Per-Monitor DPI v2 awareness for pixel-perfect coordinate mapping.
    
    See ADR-06 in SERVO_ARCHITECTURE_V1.md.
    """
    if sys.platform != "win32":
        return False
    try:
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
        user32 = ctypes.windll.user32
        result = user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        if result:
            return True
    except Exception:
        pass

    try:
        # Fallback to shcore: PROCESS_PER_MONITOR_DPI_AWARE = 2
        shcore = ctypes.windll.shcore
        result = shcore.SetProcessDpiAwareness(2)
        if result == 0:
            return True
    except Exception:
        pass

    try:
        # Legacy fallback
        ctypes.windll.user32.SetProcessDPIAware()
        return True
    except Exception:
        return False


def is_running_as_admin() -> bool:
    """Probe if current process has elevated Administrator privileges (UIPI / UAC check)."""
    if sys.platform != "win32":
        return False
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False


def get_hardware_profile() -> dict[str, int | str | bool]:
    """Inspect local CPU core count and RAM for System 1 threading configuration."""
    profile: dict[str, int | str | bool] = {
        "cpu_count": os.cpu_count() or 1,
        "is_admin": is_running_as_admin(),
        "platform": sys.platform,
    }
    if sys.platform == "win32":
        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]
        stat = MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
            profile["total_ram_mb"] = stat.ullTotalPhys // (1024 * 1024)
            profile["avail_ram_mb"] = stat.ullAvailPhys // (1024 * 1024)
    return profile


class ServoConfig(BaseSettings):
    """Central configuration for Servo execution modes and policies."""
    model_config = SettingsConfigDict(env_prefix="SERVO_", extra="ignore")

    # Execution Mode: 'assistant' requires confirmation on medium/high risk; 'harness' runs sandboxed
    mode: Literal["assistant", "harness"] = "assistant"

    # Compute device: defaults to CPU to strictly respect < 70MB budget
    device: Literal["auto", "cpu", "gpu"] = "cpu"

    # Kinematics: fast_mode skips trajectory interpolation for CI throughput
    fast_mode: bool = False

    # Jev / Kev Decision Model endpoint (TypeSafe System One contract)
    jev_url: str = "http://localhost:8000/v1/systemone"
    jev_timeout_seconds: float = 2.0
    use_heuristic_jev_fallback: bool = True

    # Adaptive Settling Time (ADR-08)
    settling_base_ms: int = 100
    settling_max_ms: int = 750
    settling_poll_interval_ms: int = 50

    # Sandboxing allowlists (Harness mode)
    whitelisted_hwnds: list[int] = Field(default_factory=list)
    whitelisted_process_names: list[str] = Field(default_factory=list)

    # Telemetry storage
    trace_dir: Path = Field(default_factory=lambda: Path.home() / ".servo" / "traces")

    # System 2 Planner config
    planner_provider: Literal["openai", "anthropic", "gemini", "passthrough"] = "openai"
    openai_api_key: str = ""
    openai_base_url: str = "http://localhost:11434/v1"
    openai_model: str = "qwen2.5-coder:7b"
    anthropic_api_key: str = ""
    gemini_api_key: str = ""


# Auto-initialize DPI awareness on import
_dpi_initialized = init_windows_dpi_awareness()
settings = ServoConfig()
