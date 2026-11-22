"""Unit tests for Mind2Web evaluation metrics, runner, and task definitions."""

from unittest.mock import MagicMock
import pytest

from servo.integrations.mind2web.evaluator import Mind2WebEvaluator
from servo.integrations.mind2web.runner import Mind2WebRunner, format_markdown_report
from servo.integrations.mind2web.tasks import Mind2WebStepRef, Mind2WebTask, get_mind2web_benchmark_suite


def test_mind2web_evaluator_metrics():
    # Element accuracy
    acc = Mind2WebEvaluator.evaluate_element_acc(
        pred_target="search box",
        target_candidates=["Search input", "search-box", "twotabsearchtextbox"],
    )
    assert acc == 1.0

    miss = Mind2WebEvaluator.evaluate_element_acc(
        pred_target="shopping cart",
        target_candidates=["Sign In", "Account & Lists"],
    )
    assert miss == 0.0

    # Action F1
    act_click = Mind2WebEvaluator.evaluate_action_f1(
        pred_action="click", pred_value="", true_action="click", true_value=""
    )
    assert act_click == 1.0

    act_type = Mind2WebEvaluator.evaluate_action_f1(
        pred_action="type", pred_value="wireless headphones", true_action="type", true_value="wireless headphones"
    )
    assert act_type == 1.0

    step_eval = Mind2WebEvaluator.evaluate_step(
        pred_target="Search box",
        target_candidates=["search input", "Search box"],
        pred_action="type",
        pred_value="headphones",
        true_action="type",
        true_value="headphones",
    )
    assert step_eval["element_acc"] == 1.0
    assert step_eval["action_f1"] == 1.0
    assert step_eval["step_sr"] == 1.0


def test_mind2web_task_suite_loading():
    suite = get_mind2web_benchmark_suite()
    assert len(suite) >= 4
    domains = {t.domain for t in suite}
    assert "ecommerce" in domains
    assert "information" in domains
    assert "entertainment" in domains
    assert "developer" in domains


def test_mind2web_runner_mock_execution():
    mock_agent = MagicMock()
    mock_agent.execute_goal.return_value = {
        "passed": True,
        "route": "system1",
        "steps": ["type_text(target='search', text='headphones')"],
    }

    runner = Mind2WebRunner(agent=mock_agent)
    task = Mind2WebTask(
        id="test_m2w_search",
        domain="ecommerce",
        website="amazon.com",
        instruction="search amazon for headphones",
        reference_steps=[
            Mind2WebStepRef(target_candidates=["search", "search input"], action_type="type", value="headphones")
        ],
        check=lambda: (True, "Found amazon"),
    )

    res = runner.run_single_task(task)
    assert res["passed"] is True
    assert res["verdict"] == "PASS"
    assert res["element_acc"] == 100.0
    assert res["task_sr"] == 1.0


def test_mind2web_report_formatting():
    summary = {
        "benchmark": "Mind2Web",
        "timestamp": "2026-11-22 20:00:00",
        "total_tasks": 1,
        "passed_tasks": 1,
        "task_success_rate_percent": 100.0,
        "element_acc_percent": 100.0,
        "action_f1_percent": 100.0,
        "avg_latency_s": 2.5,
        "leaderboard_comparison": {
            "SeeAct (GPT-4V)": {"ele_acc": 64.5, "task_sr": 42.0},
            "Servo Web Tier": {"ele_acc": 100.0, "task_sr": 100.0},
        },
        "results": [
            {
                "id": "m2w_1",
                "domain": "ecommerce",
                "website": "amazon.com",
                "route": "system1",
                "element_acc": 100.0,
                "seconds": 2.5,
                "passed": True,
                "evidence": "OK",
            }
        ],
    }
    report = format_markdown_report(summary)
    assert "Mind2Web Benchmark Evaluation Report" in report
    assert "SeeAct (GPT-4V)" in report
    assert "100.0%" in report
