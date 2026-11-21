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

    # Jev / Kev Decision Model endpoint (TypeSafe System One contract).
    # Empty by default: the old default (localhost:8000) was Servo's own daemon port,
    # so disabling the heuristic fallback made the gate POST to Servo itself.
    jev_url: str = ""
    jev_timeout_seconds: float = 2.0
    # True forces heuristics even when a Kev URL is set (hermetic/offline runs).
    use_heuristic_jev_fallback: bool = False
    # Kev server (python -m kev.serve ... --port 8009). Base URL; /v1/systemone is appended.
    kev_url: str = ""
    kev_api_key: str = ""
    kev_model: str = "kev-latest"
    # P(high) at or above this counts as high risk even if another level is likelier.
    kev_high_risk_floor: float = 0.25

    # ---- Cognitive cascade (see docs/COGNITIVE_CASCADE.md) ----
    # Cactus Needle 3: minimum calibrated confidence to act on a proposed tool call.
    needle_confidence_threshold: float = 0.7
    # Path to needle3.cact; empty = cactus downloads Cactus-Compute/needle3.
    needle_weights: str = ""
    mirage_weights: str = ""
    # Minimum fuzzy-match score for a Mirage detection to count as the target.
    mirage_match_threshold: float = 0.72
    # How many times System 2 may re-plan after a failure within one goal.
    max_replans: int = 2
    # Act-observe-decide rounds with System 2 before asking you whether to continue.
    max_rounds: int = 8
    # Playwright browser tier: 'msedge', 'chrome', or 'chromium' (bundled).
    browser_channel: str = "msedge"
    browser_profile_dir: Path = Field(default_factory=lambda: Path.home() / ".servo" / "browser-profile")
    browser_headless: bool = False
    search_url: str = "https://www.bing.com/search?q={q}"
    # System 2 (LLM) reasoning depth for Anthropic models: low | medium | high | xhigh | max
    system2_effort: str = "medium"
    # cactus-engine bundle for a fully local System 2 (SERVO_PLANNER_PROVIDER=cactus)
    cactus_engine_model: str = "google/gemma-4-E2B-it"
    # Copilot UI server
    copilot_port: int = 8765

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
    planner_provider: Literal["openai", "anthropic", "gemini", "cactus", "passthrough", "none"] = "openai"
    openai_api_key: str = ""
    openai_base_url: str = "http://localhost:11434/v1"
    # Default model chosen specifically to fit 4GB VRAM local GPU budgets
    openai_model: str = "llama3.2:1b"
    default_local_model: str = "llama3.2:1b"
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-opus-5-5"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.0-flash"

    # Local REST API bearer token. If empty, a random token is generated on first
    # start and stored in ~/.servo/api_token (see servo.daemon.auth).
    api_token: str = ""

    # Assistant-mode confirmation for medium/high-risk actions:
    #   'dialog' - native Yes/No dialog on the automated desktop (default No, times out to No)
    #   'deny'   - refuse all such actions (unattended / headless deployments)
    confirmation_channel: Literal["dialog", "deny"] = "dialog"
    confirmation_timeout_seconds: float = 60.0

    # Upper bound for a whole /task run (planning + all sub-goals) on the worker thread.
    # Generous because a human may be answering confirmation dialogs during the run.
    task_timeout_seconds: float = 300.0


# Auto-initialize DPI awareness on import
_dpi_initialized = init_windows_dpi_awareness()
settings = ServoConfig()
try:  # values chosen in the copilot's setup screen (env vars still win)
    from servo.user_settings import apply as _apply_user_settings
    _apply_user_settings(settings)
except Exception:
    pass
if not settings.kev_url and settings.jev_url:
    settings.kev_url = settings.jev_url  # SERVO_JEV_URL kept as an alias
