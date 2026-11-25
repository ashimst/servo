"""Tests for System 1 and System 2 Handoff Architecture."""

import threading
import pytest
from unittest.mock import ANY, MagicMock, patch

from servo.control.handoff import (
    PlanExecutionSummary,
    StepExecutionReport,
    SystemHandoffCoordinator,
)
from servo.cognition.plan import MacroPlan, PlanStep
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


def test_handoff_launch_and_wait(laya):  # even a wait is pre-checked by Laya
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

    def mock_launcher(app_name):
        return 9999, "Mock App"

    with patch("servo.perception.windows.is_window", return_value=True), \
         patch("servo.perception.windows.get_window_title", return_value="Mock App"):
        coord = SystemHandoffCoordinator(fast_mode=True)
        summary = coord.execute_plan(plan, app_launcher=mock_launcher)

    assert summary.success is True
    assert summary.completed_steps == 2
    assert summary.active_hwnd == 9999
    assert [(r.step_index, r.success) for r in summary.step_reports] == [(1, True), (2, True)]


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


def test_handoff_multi_action_parameter_forwarding(mock_scene):
    plan = MacroPlan(
        goal="execute multi-action plan",
        steps=[
            PlanStep(step_index=1, tool_name="click_element", arguments={"target": "Save", "button": "left"}),
            PlanStep(step_index=2, tool_name="double_click", arguments={"target": "Text editor"}),
            PlanStep(step_index=3, tool_name="right_click", arguments={"target": "Save"}),
            PlanStep(step_index=4, tool_name="type_text", arguments={"target": "Text editor", "text": "sample", "clear_first": True, "press_enter": True}),
            PlanStep(step_index=5, tool_name="hotkey", arguments={"keys": ["ctrl", "s"]}),
            PlanStep(step_index=6, tool_name="invoke", arguments={"target": "Save"}),
            PlanStep(step_index=7, tool_name="scroll", arguments={"target": "Text editor", "direction": "down", "amount": 4}),
            PlanStep(step_index=8, tool_name="wait", arguments={"duration_seconds": 0.05}),
        ],
    )

    mock_loop = MagicMock()
    mock_loop.execute_subgoal.return_value = MagicMock(
        success=True, attempts_used=1, step_log=MagicMock(latency_ms=10, error=None)
    )

    with patch("servo.perception.windows.is_window", return_value=True), \
         patch("servo.perception.windows.get_window_title", return_value="Mock App"), \
         patch("servo.control.handoff.ServoControlLoop", return_value=mock_loop):
        coord = SystemHandoffCoordinator(target_hwnd=9999, fast_mode=True)
        summary = coord.execute_plan(plan)

    assert summary.success is True
    assert summary.completed_steps == 8
    assert mock_loop.execute_subgoal.call_count == 8

    calls = mock_loop.execute_subgoal.call_args_list

    # Step 1: click
    assert calls[0].kwargs["action_type"] == "click"
    assert calls[0].kwargs["button"] == "left"

    # Step 2: double click
    assert calls[1].kwargs["action_type"] in ("double_click", "click")
    assert calls[1].kwargs["button"] == "double"

    # Step 3: right click
    assert calls[2].kwargs["action_type"] in ("right_click", "click")
    assert calls[2].kwargs["button"] == "right"

    # Step 4: type_text with flags
    assert calls[3].kwargs["action_type"] == "type"
    assert calls[3].kwargs["text_to_type"] == "sample"
    assert calls[3].kwargs["clear_first"] is True
    assert calls[3].kwargs["press_enter"] is True

    # Step 5: hotkey
    assert calls[4].kwargs["action_type"] == "hotkey"
    assert calls[4].kwargs["keys"] == ["ctrl", "s"]

    # Step 6: invoke
    assert calls[5].kwargs["action_type"] == "invoke"

    # Step 7: scroll
    assert calls[6].kwargs["action_type"] == "scroll"
    assert calls[6].kwargs["direction"] == "down"
    assert calls[6].kwargs["amount"] == 4

    # Step 8: wait
    assert calls[7].kwargs["action_type"] == "wait"
    assert calls[7].kwargs["duration_seconds"] == 0.05


def test_handoff_safety_gating_rejection_halts_plan(mock_scene):
    plan = MacroPlan(
        goal="gated plan",
        steps=[
            PlanStep(step_index=1, tool_name="click_element", arguments={"target": "Save"}),
            PlanStep(step_index=2, tool_name="click_element", arguments={"target": "Cancel"}),
        ],
    )

    mock_loop = MagicMock()
    # Step 1 fails due to user rejection
    mock_loop.execute_subgoal.return_value = MagicMock(
        success=False, attempts_used=1, step_log=MagicMock(latency_ms=15, error="Action rejected by user confirmation.")
    )

    confirmation_cb = MagicMock(return_value=False)

    with patch("servo.perception.windows.is_window", return_value=True), \
         patch("servo.perception.windows.get_window_title", return_value="Mock App"), \
         patch("servo.control.handoff.ServoControlLoop", return_value=mock_loop) as mock_loop_cls:
        coord = SystemHandoffCoordinator(
            target_hwnd=9999, fast_mode=True, confirmation_callback=confirmation_cb
        )
        summary = coord.execute_plan(plan)

    assert summary.success is False
    assert summary.completed_steps == 0
    # Step 2 was never dispatched
    assert mock_loop.execute_subgoal.call_count == 1
    # Verify confirmation_callback passed to loop constructor
    mock_loop_cls.assert_called_with(
        hwnd=9999, fast_mode=True, confirmation_callback=confirmation_cb, should_abort=None, mode=None, tracer=ANY
    )


def test_handoff_cancellation_mid_plan(mock_scene):
    plan = MacroPlan(
        goal="cancellable plan",
        steps=[
            PlanStep(step_index=1, tool_name="click_element", arguments={"target": "Save"}),
            PlanStep(step_index=2, tool_name="click_element", arguments={"target": "Cancel"}),
        ],
    )

    abort_token = {"aborted": False}
    mock_loop = MagicMock()

    def side_effect(*args, **kwargs):
        # User clicks cancel while step 1 executes
        abort_token["aborted"] = True
        return MagicMock(success=True, attempts_used=1, step_log=MagicMock(latency_ms=20, error=None))

    mock_loop.execute_subgoal.side_effect = side_effect

    with patch("servo.perception.windows.is_window", return_value=True), \
         patch("servo.perception.windows.get_window_title", return_value="Mock App"), \
         patch("servo.control.handoff.ServoControlLoop", return_value=mock_loop):
        coord = SystemHandoffCoordinator(
            target_hwnd=9999, fast_mode=True, should_abort=lambda: abort_token["aborted"]
        )
        summary = coord.execute_plan(plan)

    assert summary.success is False
    assert summary.completed_steps == 1
    assert summary.aborted is True
    # Step 2 must not have been executed
    assert mock_loop.execute_subgoal.call_count == 1


def test_handoff_cancellation_threading_event(mock_scene):
    plan = MacroPlan(
        goal="cancellable plan with event",
        steps=[
            PlanStep(step_index=1, tool_name="click_element", arguments={"target": "Save"}),
            PlanStep(step_index=2, tool_name="click_element", arguments={"target": "Cancel"}),
        ],
    )

    event = threading.Event()
    event.set()  # Cancel before execution starts

    mock_loop = MagicMock()

    with patch("servo.perception.windows.is_window", return_value=True), \
         patch("servo.perception.windows.get_window_title", return_value="Mock App"), \
         patch("servo.control.handoff.ServoControlLoop", return_value=mock_loop):
        coord = SystemHandoffCoordinator(
            target_hwnd=9999, fast_mode=True, should_abort=event
        )
        summary = coord.execute_plan(plan)

    assert summary.success is False
    assert summary.completed_steps == 0
    assert summary.aborted is True
    assert mock_loop.execute_subgoal.call_count == 0


def test_handoff_wait_fallback_when_no_window():
    plan = MacroPlan(
        goal="headless wait",
        steps=[
            PlanStep(step_index=1, tool_name="wait", arguments={"duration_seconds": 0.05}),
        ],
    )

    with patch("servo.perception.windows.is_window", return_value=False), \
         patch("servo.perception.windows.get_foreground_window", return_value=None):
        coord = SystemHandoffCoordinator(target_hwnd=None, fast_mode=True)
        summary = coord.execute_plan(plan)

    assert summary.success is True
    assert summary.completed_steps == 1
    assert summary.step_reports[0].success is True

