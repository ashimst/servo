"""Servo configuration, runtime flags, DPI v2 awareness boot, and hardware probe."""

from __future__ import annotations

import ctypes
import sys
from pathlib import Path
from typing import Literal
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from servo.paths import servo_home


def init_windows_dpi_awareness() -> bool:
    """Initialize Windows Per-Monitor DPI v2 awareness for pixel-perfect coordinate mapping.
    
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


class ServoConfig(BaseSettings):
    """Central configuration for Servo execution modes and policies."""
    model_config = SettingsConfigDict(env_prefix="SERVO_", extra="ignore")

    # Execution Mode: 'assistant' requires confirmation on medium/high risk; 'harness' runs sandboxed
    mode: Literal["assistant", "harness"] = "assistant"

    # Compute device: defaults to CPU to strictly respect < 70MB budget
    device: Literal["auto", "cpu", "gpu"] = "cpu"

    # Kinematics: fast_mode skips trajectory interpolation for CI throughput
    fast_mode: bool = False

    # Laya runs on the CPU (the GPU holds the planner); allow it time per decision.
    jev_timeout_seconds: float = 20.0
    # Laya decision server: decision_model/server.py serve (Laya vega engine + heads fitted on
    # Servo's labelled rows; see decision_model/build_rows.py).
    # Base URL; /v1/systemone is appended. There is no decision path without it.
    laya_url: str = "http://127.0.0.1:8009"
    laya_api_key: str = ""
    laya_model: str = "english"
    # P(high) at or above this counts as high risk even if another level is likelier.
    laya_high_risk_floor: float = 0.25

    # ---- Cognitive cascade (see docs/COGNITIVE_CASCADE.md) ----
    # Cactus Needle 3: minimum calibrated confidence to act on a proposed tool call.
    needle_confidence_threshold: float = 0.7
    # Path to needle3.cact; empty = cactus downloads Cactus-Compute/needle3.
    needle_weights: str = ""
    # Servo's tuned Needle for the request and command agents (needle_model/README.md);
    # empty = the base Needle. Planner steps always use needle_weights.
    needle_intent_weights: str = ""
    # A request or command call acts only at or above this confidence (needle_model/eval.py).
    needle_intent_threshold: float = 0.5
    mirage_weights: str = ""
    # Visual locator: the local planner's Qwen3.5-4B with its vision projector (llama-server
    # --mmproj), so it is the same server as local_planner_url.
    locator_url: str = "http://127.0.0.1:8080/v1"
    locator_timeout_seconds: float = 60.0
    # Minimum fuzzy-match score for text on screen (OCR, Mirage) to count as the target.
    text_match_threshold: float = 0.72
    # How many times System 2 may re-plan after a failure within one goal.
    max_replans: int = 4   # real multi-step tasks need room to recover from a wrong label or two
    # Act-observe-decide rounds with System 2 before asking you whether to continue.
    max_rounds: int = 8
    # Web tasks open pages in YOUR browser (preferred_browser, else the Windows default)
    # and drive it like any desktop app. e.g. "dia", "chrome", "edge", "arc".
    preferred_browser: str = ""
    search_url: str = "https://www.bing.com/search?q={q}"
    # System 2 (LLM) reasoning depth for Anthropic models: low | medium | high | xhigh | max
    system2_effort: str = "medium"
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
    trace_dir: Path = Field(default_factory=lambda: servo_home() / "traces")

    # System 2 planner tiers. The local planner is a Qwen model behind llama.cpp's
    # OpenAI-compatible server; the cloud planner (planner_provider + its key) is the step-up.
    #   local_first: local; steps up to cloud for the rest of a task when local fails
    #   local_only / cloud_first / cloud_only: as named (cloud_first steps down to nothing)
    planner_mode: Literal["local_first", "local_only", "cloud_first", "cloud_only"] = "local_first"
    local_planner_url: str = "http://127.0.0.1:8080/v1"
    local_planner_model: str = "qwen"
    local_planner_timeout_seconds: float = 300.0
    # Cloud planner
    planner_provider: Literal["openai", "anthropic", "gemini", "none"] = "openai"
    openai_api_key: str = ""
    openai_base_url: str = ""
    openai_model: str = ""
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-opus-5-5"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.0-flash"


    # Assistant-mode confirmation for medium/high-risk actions:
    #   'dialog' - native Yes/No dialog on the automated desktop (default No, times out to No)
    #   'deny'   - refuse all such actions (unattended / headless deployments)
    confirmation_channel: Literal["dialog", "deny"] = "dialog"
    confirmation_timeout_seconds: float = 60.0

    # Direct file and shell tools (see servo/actuation/system_tools.py). Always gated:
    # every command asks first in assistant mode; never available in harness mode.
    system_tools: bool = True
    command_timeout_seconds: float = 120.0

    # Self-diagnosis (servo/cognition/diagnose.py): send code snippets to the planner model
    # when asking it to explain a failure. Off by default: only function names/docstrings go.
    diagnose_send_code: bool = False

    # Scheduled and queued goals (see servo/daemon/scheduler.py)
    schedule_file: Path = Field(default_factory=lambda: servo_home() / "schedule.json")

    # Upper bound for a whole /task run (planning + all sub-goals) on the worker thread.
    # Generous because a human may be answering confirmation dialogs during the run.
    task_timeout_seconds: float = 300.0


# Auto-initialize DPI awareness on import
_dpi_initialized = init_windows_dpi_awareness()
settings = ServoConfig()
try:  # values chosen in the copilot's setup screen (env vars still win)
    from servo.user_settings import apply as _apply_user_settings
    _apply_user_settings(settings)
except Exception as _e:  # noqa: BLE001 - the copilot still starts, but says its saved settings were not used
    import logging as _logging
    _logging.getLogger("servo.config").warning("Saved settings (~/.servo/settings.json) not applied: %s", _e)
