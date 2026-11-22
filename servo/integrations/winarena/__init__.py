"""Windows Agent Arena (WAA) integration for Servo."""

from servo.integrations.winarena.action_converter import (
    parse_call_args,
    steps_to_pyautogui_script,
    tool_call_to_pyautogui,
)
from servo.integrations.winarena.agent import ServoAgent
from servo.integrations.winarena.tasks import WAATask, get_waa_benchmark_suite

__all__ = [
    "ServoAgent",
    "WindowsAgentArenaRunner",
    "WAATask",
    "get_waa_benchmark_suite",
    "tool_call_to_pyautogui",
    "steps_to_pyautogui_script",
    "parse_call_args",
    "format_markdown_report",
]


def __getattr__(name: str):
    if name in ("WindowsAgentArenaRunner", "format_markdown_report"):
        from servo.integrations.winarena import runner
        return getattr(runner, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
