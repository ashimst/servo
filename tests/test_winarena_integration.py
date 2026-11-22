"""Unit tests for WindowsAgentArena integration (action converter, agent interface, task suite, runner)."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any, List
from unittest.mock import MagicMock

import pytest

from servo.cognition.router import RunOutcome, StepRecord
from servo.integrations.winarena.action_converter import (
    parse_call_args,
    steps_to_pyautogui_script,
    tool_call_to_pyautogui,
)
from servo.integrations.winarena.agent import ServoAgent
from servo.integrations.winarena.runner import WindowsAgentArenaRunner, format_markdown_report
from servo.integrations.winarena.tasks import WAATask, get_waa_benchmark_suite


def test_parse_call_args():
    args = parse_call_args("click_element(target='7', x=100, y=200)")
    assert args["target"] == "7"
    assert args["x"] == 100
    assert args["y"] == 200

    args2 = parse_call_args("send_hotkey(keys=['ctrl', 'c'])")
    assert args2["keys"] == ["ctrl", "c"]


def test_tool_call_to_pyautogui_conversions():
    # Clicks
    assert "pyautogui.click(100, 200)" in tool_call_to_pyautogui("click_element(x=100, y=200)")
    assert "pyautogui.doubleClick(50, 75)" in tool_call_to_pyautogui("double_click(x=50, y=75)")
    assert "pyautogui.rightClick(10, 20)" in tool_call_to_pyautogui("right_click(x=10, y=20)")

    # Typing
    assert "pyautogui.write('hello world'" in tool_call_to_pyautogui("type_text(text='hello world')")

    # Hotkeys
    assert "pyautogui.hotkey('ctrl', 'shift', 'esc')" in tool_call_to_pyautogui("send_hotkey(keys=['ctrl', 'shift', 'esc'])")

    # Launch app
    assert "subprocess.Popen('notepad'" in tool_call_to_pyautogui("launch_app(app_name='notepad')")

    # Browser URL
    assert "webbrowser.open('https://en.wikipedia.org')" in tool_call_to_pyautogui("open_url(url='https://en.wikipedia.org')")

    # Shell commands
    assert "powershell" in tool_call_to_pyautogui("run_command(command='Get-Process', shell='powershell')")

    # Sleep
    assert "time.sleep(2.5)" in tool_call_to_pyautogui("wait(duration_seconds=2.5)")

    # Scroll
    assert "pyautogui.scroll(-300)" in tool_call_to_pyautogui("scroll(direction='down', amount=3)")


def test_steps_to_pyautogui_script():
    steps = [
        StepRecord(command="open notepad", tool="launch_app", call="launch_app(app_name='notepad')", tier="needle", success=True),
        StepRecord(command="write hello", tool="type_text", call="type_text(text='hello')", tier="needle", success=True),
    ]
    script = steps_to_pyautogui_script(steps)
    assert "import pyautogui" in script
    assert "subprocess.Popen('notepad'" in script
    assert "pyautogui.write('hello'" in script
    assert "# DONE" in script


def test_servo_agent_predict_and_queue():
    fake_router = MagicMock()
    fake_outcome = RunOutcome(
        run_id="test_run_123",
        goal="open calculator and click 7",
        success=True,
        reply="Done",
        route="system1",
        steps=[
            StepRecord(command="launch calculator", tool="launch_app", call="launch_app(app_name='calculator')", tier="needle", success=True),
            StepRecord(command="click 7", tool="click_element", call="click_element(x=200, y=300)", tier="needle", success=True),
        ],
    )
    fake_router.run.return_value = fake_outcome

    agent = ServoAgent(router_factory=lambda: fake_router)
    agent.reset()

    # First predict call triggers execution and returns the first action
    action1 = agent.predict("open calculator and click 7")
    assert "subprocess.Popen('calculator'" in action1

    # Second predict call pops the queued second action
    action2 = agent.predict("open calculator and click 7")
    assert "pyautogui.click(200, 300)" in action2

    # Third predict call signals DONE
    action3 = agent.predict("open calculator and click 7")
    assert action3 == "DONE"


def test_servo_agent_failure_signals_fail():
    fake_router = MagicMock()
    fake_router.run.side_effect = RuntimeError("App window crashed")

    agent = ServoAgent(router_factory=lambda: fake_router)
    agent.reset()

    action = agent.predict("impossible goal")
    assert "FAIL" in action


def test_waa_benchmark_suite_definitions(tmp_path):
    tasks = get_waa_benchmark_suite(tmp_path)
    assert len(tasks) >= 8

    domains = {t.domain for t in tasks}
    assert "productivity" in domains
    assert "system" in domains
    assert "browser" in domains
    assert "files_explorer" in domains

    for t in tasks:
        assert t.id.startswith("waa_")
        assert len(t.instruction) > 0
        assert callable(t.check)


def test_waa_runner_metrics_and_markdown_report(tmp_path):
    fake_agent = MagicMock()
    fake_agent.execute_goal.return_value = {
        "passed": True,
        "route": "system1",
        "reply": "Done",
        "steps": ["launch_app(app_name='notepad')", "type_text(text='test')"],
        "latency_seconds": 1.2,
    }

    runner = WindowsAgentArenaRunner(agent=fake_agent, scratch_dir=tmp_path)

    # Run with a mock task that succeeds
    mock_task = WAATask(
        id="waa_mock_pass",
        domain="productivity",
        instruction="mock task",
        check=lambda: (True, "mock check pass"),
        needs_llm=False,
        baseline_success={"Navi": True, "Claude3.5_Sonnet": True},
    )

    res = runner.run_single_task(mock_task)
    assert res["passed"] is True
    assert res["verdict"] == "PASS"

    # Run with a mock task that lies (agent says True, probe says False)
    mock_lie_task = WAATask(
        id="waa_mock_lie",
        domain="system",
        instruction="mock lie",
        check=lambda: (False, "probe found nothing"),
        needs_llm=False,
    )
    res_lie = runner.run_single_task(mock_lie_task)
    assert res_lie["passed"] is False
    assert res_lie["verdict"] == "LIE"

    # Test report formatting
    summary = {
        "benchmark": "WindowsAgentArena (WAA)",
        "timestamp": "2026-11-22 12:00:00",
        "total_tasks": 2,
        "passed_tasks": 1,
        "success_rate_percent": 50.0,
        "lie_rate_percent": 50.0,
        "reflex_s1_avg_latency_s": 1.2,
        "planning_s2_avg_latency_s": 0.0,
        "leaderboard_comparison": {
            "Navi (Microsoft WAA Baseline)": 19.5,
            "Claude 3.5 Sonnet (Computer Use)": 41.2,
            "Servo (Reflex + System 2 Cascade)": 50.0,
        },
        "results": [res, res_lie],
    }

    report = format_markdown_report(summary)
    assert "# WindowsAgentArena (WAA) Benchmark Evaluation Report" in report
    assert "Claude 3.5 Sonnet" in report
    assert "✅ PASS" in report
    assert "⚠️ LIE" in report
