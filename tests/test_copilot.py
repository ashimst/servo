"""Unit tests for the Conversational Floating On-Screen Desktop Copilot companion with System 1/2 handoff."""

import pytest
from unittest.mock import patch, MagicMock

from servo.daemon.copilot import CopilotBrain, launch_or_focus_app
from servo.actuation.alias_mapping import CanonicalActionType


def test_copilot_brain_greetings():
    brain = CopilotBrain(user_name="Ashim")
    reply, plan = brain.process_message("hello")
    assert "Ashim" in reply
    assert plan is None


def test_copilot_brain_identity_query():
    brain = CopilotBrain(user_name="Ashim")
    reply, plan = brain.process_message("what is my name?")
    assert "Ashim" in reply
    assert plan is None


def test_copilot_brain_name_learning():
    brain = CopilotBrain(user_name="Ashim")
    reply, plan = brain.process_message("my name is Alex")
    assert "Alex" in reply
    assert brain.user_name == "Alex"
    assert plan is None


def test_copilot_brain_general_chat():
    brain = CopilotBrain(user_name="Ashim")
    reply, plan = brain.process_message("who are you?")
    assert "Servo" in reply
    assert plan is None


def test_copilot_brain_plan_open_and_write():
    brain = CopilotBrain(user_name="Ashim")
    reply, plan = brain.process_message("open notepad and write hello world")
    assert plan is not None
    assert len(plan.steps) == 2
    assert plan.steps[0].canonical_action.action_type == CanonicalActionType.LAUNCH
    assert plan.steps[0].canonical_action.app_name == "notepad"
    assert plan.steps[1].canonical_action.action_type == CanonicalActionType.TYPE
    assert plan.steps[1].canonical_action.text == "hello world"


def test_copilot_brain_plan_open_and_click():
    brain = CopilotBrain(user_name="Ashim")
    reply, plan = brain.process_message("open calculator and click 7")
    assert plan is not None
    assert len(plan.steps) == 2
    assert plan.steps[0].canonical_action.action_type == CanonicalActionType.LAUNCH
    assert plan.steps[0].canonical_action.app_name == "calculator"
    assert plan.steps[1].canonical_action.action_type == CanonicalActionType.CLICK
    assert plan.steps[1].canonical_action.target == "7"


def test_copilot_brain_plan_write_my_name():
    brain = CopilotBrain(user_name="Ashim")
    reply, plan = brain.process_message("write my name")
    assert plan is not None
    assert len(plan.steps) == 1
    assert plan.steps[0].canonical_action.action_type == CanonicalActionType.TYPE
    assert plan.steps[0].canonical_action.text == "Ashim"


def test_copilot_brain_plan_close():
    brain = CopilotBrain(user_name="Ashim")
    reply, plan = brain.process_message("close notepad")
    assert plan is not None
    assert len(plan.steps) == 1
    assert plan.steps[0].canonical_action.action_type == CanonicalActionType.CLOSE


@patch("servo.perception.windows.list_windows")
def test_copilot_brain_list_windows(mock_list):
    mock_list.return_value = [
        {"hwnd": 12345, "title": "Untitled - Notepad", "process_name": "notepad.exe"}
    ]
    brain = CopilotBrain(user_name="Ashim")
    reply, plan = brain.process_message("what windows are open?")
    assert plan is None
    assert "Notepad" in reply


@patch("servo.perception.windows.list_windows")
def test_launch_or_focus_app_existing_window(mock_list):
    mock_list.return_value = [
        {"hwnd": 12345, "title": "Untitled - Notepad", "process_name": "notepad.exe"}
    ]
    hwnd, title = launch_or_focus_app("notepad")
    assert hwnd == 12345
    assert "Notepad" in title


def test_resolve_app_executable():
    from servo.daemon.copilot import resolve_app_executable
    chrome_path = resolve_app_executable("chrome")
    assert "chrome" in chrome_path.lower()
    notepad_path = resolve_app_executable("notepad")
    assert "notepad" in notepad_path.lower()


def test_copilot_brain_dictionary_subscripting_compatibility():
    """Verify legacy dictionary subscripting syntax works on MacroPlan without TypeError."""
    brain = CopilotBrain(user_name="Ashim")
    _, plan = brain.process_message("open notepad and write hello world")
    assert plan is not None
    # Subscripting on MacroPlan
    assert plan["type"] == "launch"
    assert plan["app"] == "notepad"
    assert len(plan["steps"]) == 2
    assert len(plan["actions"]) == 2
    # Subscripting on PlanStep
    assert plan.steps[0]["type"] == "launch"
    assert plan.steps[0]["app"] == "notepad"
    assert plan.steps[1]["type"] == "type"
    assert plan.steps[1]["text"] == "hello world"
    assert plan.steps[1]["target"] == "editor"


def test_copilot_brain_dictionary_subscripting_write_and_close():
    brain = CopilotBrain(user_name="Ashim")
    _, plan_write = brain.process_message("write my name")
    assert plan_write["type"] == "type"
    assert plan_write["text"] == "Ashim"

    _, plan_close = brain.process_message("close notepad")
    assert plan_close["type"] == "close"
    assert plan_close["app"] == "notepad"


# =============================================================================
# Feature 15: Centralized Planning via get_planner
# =============================================================================

def test_copilot_brain_delegates_to_configured_planner(monkeypatch):
    from servo.config import settings
    from servo.planner.tool_calling import MacroPlan, PlanStep
    from servo.actuation.alias_mapping import AliasMapper

    monkeypatch.setattr(settings, "planner_provider", "openai")

    expected_plan = MacroPlan(
        goal="open notepad",
        steps=[
            PlanStep(
                step_index=1,
                tool_name="launch_app",
                arguments={"app_name": "notepad"},
                canonical_action=AliasMapper.canonicalize_action("launch_app", app_name="notepad"),
            )
        ],
    )

    mock_planner = MagicMock()
    mock_planner.decompose_macro.return_value = expected_plan

    with patch("servo.planner.get_planner", return_value=mock_planner) as mock_gp:
        brain = CopilotBrain()
        plan = brain.query_llm_for_plan("open notepad")

        mock_gp.assert_called_with(None)
        mock_planner.decompose_macro.assert_called_once()
        assert plan == expected_plan


def test_copilot_brain_provider_synonym_routing():
    from servo.planner.tool_calling import MacroPlan, PlanStep

    brain = CopilotBrain(planner_provider="ollama")
    mock_planner = MagicMock()
    mock_planner.decompose_macro.return_value = MacroPlan(goal="test", steps=[PlanStep(step_index=1, tool_name="wait")])
    with patch("servo.planner.get_planner", return_value=mock_planner) as mock_gp:
        brain.query_llm_for_plan("wait a second")
        mock_gp.assert_called_with("ollama")


def test_copilot_brain_fallback_on_planner_error():
    from servo.planner.base import PlannerError

    mock_planner = MagicMock()
    mock_planner.decompose_macro.side_effect = PlannerError("Rate limit / API error")

    with patch("servo.planner.get_planner", return_value=mock_planner):
        brain = CopilotBrain(user_name="Ashim")
        reply, plan = brain.process_message("open notepad and write hello world")

        assert plan is not None
        assert len(plan.steps) == 2
        assert plan.steps[0].canonical_action.action_type == CanonicalActionType.LAUNCH
        assert plan.steps[1].canonical_action.text == "hello world"


def test_copilot_brain_forwards_scene_context():
    from servo.perception.scene_index import SceneIndex

    mock_scene = SceneIndex(hwnd=12345, window_title="Test App", source="uia", elements={})
    mock_planner = MagicMock()
    mock_planner.decompose_macro.return_value = None

    with patch("servo.planner.get_planner", return_value=mock_planner):
        brain = CopilotBrain()
        brain.query_llm_for_plan("click Save", scene=mock_scene)

        assert mock_planner.decompose_macro.call_args[1]["initial_scene"] == mock_scene


def test_copilot_brain_conversational_bypasses_planner():
    with patch("servo.planner.get_planner") as mock_gp:
        brain = CopilotBrain(user_name="Ashim")
        reply, plan = brain.process_message("hello, how are you?")

        assert plan is None
        assert "Ashim" in reply
        mock_gp.assert_not_called()


# =============================================================================
# Milestone 4 Iteration 2: Verifier Feedback Fixes & Robustness Tests
# =============================================================================

def test_copilot_brain_legacy_decompose_planner_fallback_without_scene():
    """Verify that CopilotBrain falling back to a legacy planner with decompose()
    and scene=None creates a valid SceneIndex(..., source='uia') and does not
    raise Pydantic ValidationError."""
    from unittest.mock import MagicMock, patch
    from servo.daemon.copilot import CopilotBrain
    from servo.planner.base import SubGoal

    mock_planner = MagicMock(spec=["decompose"])
    mock_planner.decompose.return_value = [
        SubGoal(
            step_index=1,
            instruction="click OK button",
            action_type="click",
            target_element_hint="OK",
        )
    ]
    brain = CopilotBrain()
    with patch("servo.planner.get_planner", return_value=mock_planner):
        plan = brain.query_llm_for_plan("click OK button", scene=None)
        assert plan is not None
        assert len(plan.steps) == 1
        assert plan.steps[0].arguments["target"] == "OK"

        # Verify decompose was called with default SceneIndex having source="uia"
        args, kwargs = mock_planner.decompose.call_args
        assert args[0] == "click OK button"
        scene_arg = args[1]
        assert scene_arg.hwnd == 0
        assert scene_arg.window_title == ""
        assert scene_arg.source == "uia"


def test_copilot_brain_legacy_planner_type_action_resolution():
    """Verify that a legacy planner yielding action_type='type' produces a PlanStep with
    tool_name='type_text' and arguments containing text and target, resolving SubGoal default tool_name."""
    brain = CopilotBrain(planner_provider="passthrough")
    plan = brain.query_llm_for_plan('type "hello" into editor')
    assert plan is not None
    assert len(plan.steps) == 1
    assert plan.steps[0].tool_name == "type_text"
    assert plan.steps[0].arguments["text"] == "hello"
    assert plan.steps[0].arguments["target"] == "editor"
    assert plan.steps[0].canonical_action is not None
    assert plan.steps[0].canonical_action.action_type == CanonicalActionType.TYPE
    assert plan.steps[0].canonical_action.text == "hello"


def test_copilot_brain_legacy_planner_subgoal_default_tool_name_override():
    """Verify that SubGoal instances with default tool_name='click_element' but action_type='type'
    are properly mapped to 'type_text' rather than keeping click_element."""
    from unittest.mock import MagicMock, patch
    from servo.planner.base import SubGoal

    mock_planner = MagicMock(spec=["decompose"])
    mock_planner.decompose.return_value = [
        SubGoal(
            step_index=1,
            instruction="type search query",
            action_type="type",
            target_element_hint="search_bar",
            text_to_type="python tutorials",
            # Note: tool_name defaults to 'click_element' in SubGoal!
        )
    ]
    brain = CopilotBrain()
    with patch("servo.planner.get_planner", return_value=mock_planner):
        plan = brain.query_llm_for_plan("search python tutorials")
        assert plan is not None
        assert len(plan.steps) == 1
        assert plan.steps[0].tool_name == "type_text"
        assert plan.steps[0].arguments["text"] == "python tutorials"
        assert plan.steps[0].arguments["target"] == "search_bar"
        assert plan.steps[0].canonical_action is not None
        assert plan.steps[0].canonical_action.action_type == CanonicalActionType.TYPE
        assert plan.steps[0].canonical_action.text == "python tutorials"
