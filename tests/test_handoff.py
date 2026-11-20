"""Tests for System 1 and System 2 Handoff Architecture."""

import pytest
from unittest.mock import MagicMock, patch

from servo.control.handoff import (
    PlanExecutionSummary,
    StepExecutionReport,
    SystemHandoffCoordinator,
)
from servo.planner.tool_calling import MacroPlan, PlanStep
from servo.actuation.alias_mapping import AliasMapper
from servo.perception.scene_index import SceneElement, SceneIndex


@pytest.fixture
def mock_scene():
    elements = {
        0: SceneElement(
            element_id=0,
            label="Text editor",
            role="Document",
            bbox_screen=(50, 50, 400, 300),
            bbox_norm=(50, 50, 400, 300),
            center_screen=(200, 150),
            is_keyboard_focusable=True,
            value="",
        ),
        1: SceneElement(
            element_id=1,
            label="Save",
            role="Button",
            bbox_screen=(10, 10, 40, 40),
            bbox_norm=(10, 10, 40, 40),
            center_screen=(25, 25),
        ),
    }
    return SceneIndex(hwnd=9999, window_title="Mock App", source="uia", elements=elements)


def test_handoff_launch_and_wait():
    plan = MacroPlan(
        goal="launch and wait",
        steps=[
            PlanStep(
                step_index=1,
                tool_name="launch_app",
                arguments={"app_name": "notepad"},
                canonical_action=AliasMapper.canonicalize_action("launch_app", app_name="notepad"),
            ),
            PlanStep(
                step_index=2,
                tool_name="wait",
                arguments={"duration_seconds": 0.05},
                canonical_action=AliasMapper.canonicalize_action("wait", duration_seconds=0.05),
            ),
        ],
    )

    started_steps = []
    progress_steps = []

    def on_step_start(step):
        started_steps.append(step.step_index)

    def on_step_progress(step, report):
        progress_steps.append((step.step_index, report.success))

    def mock_launcher(app_name):
        return 9999, "Mock App"

    with patch("servo.perception.windows.is_window", return_value=True), \
         patch("servo.perception.windows.get_window_title", return_value="Mock App"):
        coord = SystemHandoffCoordinator(
            fast_mode=True,
            on_step_start=on_step_start,
            on_step_progress=on_step_progress,
        )
        summary = coord.execute_plan(plan, app_launcher=mock_launcher)

    assert summary.success is True
    assert summary.completed_steps == 2
    assert summary.active_hwnd == 9999
    assert started_steps == [1, 2]
    assert progress_steps == [(1, True), (2, True)]


def test_handoff_action_dispatch(mock_scene):
    plan = MacroPlan(
        goal="type and click",
        steps=[
            PlanStep(
                step_index=1,
                tool_name="type_text",
                arguments={"text": "hello", "target": "editor"},
                canonical_action=AliasMapper.canonicalize_action("type_text", text="hello", target="editor"),
            )
        ],
    )

    mock_loop_instance = MagicMock()
    mock_loop_instance.execute_subgoal.return_value = MagicMock(
        success=True,
        attempts_used=1,
        step_log=MagicMock(latency_ms=45, error=None),
    )

    with patch("servo.perception.windows.is_window", return_value=True), \
         patch("servo.perception.windows.get_window_title", return_value="Mock App"), \
         patch("servo.control.handoff.ServoControlLoop", return_value=mock_loop_instance):
        coord = SystemHandoffCoordinator(target_hwnd=9999, fast_mode=True)
        summary = coord.execute_plan(plan)

    assert summary.success is True
    assert summary.completed_steps == 1
    assert len(summary.step_reports) == 1
    assert summary.step_reports[0].latency_ms == 45
    mock_loop_instance.execute_subgoal.assert_called_once()
