"""Unit tests for OSWorld integration harness and tasks."""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from servo.integrations.osworld.runner import OSWorldRunner, format_markdown_report
from servo.integrations.osworld.tasks import OSWorldTask, get_osworld_benchmark_suite


def test_osworld_benchmark_suite_loading():
    scratch = Path(tempfile.mkdtemp())
    suite = get_osworld_benchmark_suite(scratch)
    assert len(suite) >= 6
    ids = [t.id for t in suite]
    assert "osworld_notepad_create" in ids
    assert "osworld_calc_sum" in ids
    assert "osworld_volume_raise" in ids
    assert "osworld_browser_wiki" in ids


def test_osworld_runner_mock_execution():
    mock_agent = MagicMock()
    mock_agent.execute_goal.return_value = {
        "passed": True,
        "route": "system1",
        "steps": ["hotkey(keys=['volumeup'])"],
    }

    scratch = Path(tempfile.mkdtemp())
    runner = OSWorldRunner(agent=mock_agent, scratch_dir=scratch)

    task = OSWorldTask(
        id="test_osworld_vol",
        domain="os_system",
        instruction="increase volume",
        check=lambda: (True, "Volume ok"),
        baseline_success={"GPT-4o": True},
    )

    res = runner.run_single_task(task)
    assert res["passed"] is True
    assert res["verdict"] == "PASS"
    assert res["route"] == "system1"


def test_osworld_markdown_report_formatting():
    summary = {
        "benchmark": "OSWorld",
        "timestamp": "2026-11-22 20:00:00",
        "total_tasks": 2,
        "passed_tasks": 2,
        "success_rate_percent": 100.0,
        "lie_rate_percent": 0.0,
        "reflex_s1_avg_latency_s": 0.5,
        "planning_s2_avg_latency_s": 0.0,
        "leaderboard_comparison": {"Claude 3.5 Sonnet": 40.1, "Servo": 100.0},
        "results": [
            {
                "id": "osworld_task_1",
                "domain": "desktop_apps",
                "route": "system1",
                "seconds": 0.5,
                "passed": True,
                "verdict": "PASS",
                "evidence": "OK",
            }
        ],
    }
    report = format_markdown_report(summary)
    assert "OSWorld Benchmark Evaluation Report" in report
    assert "Claude 3.5 Sonnet" in report
    assert "100.0%" in report
