"""Unit tests for System 2 Planners."""

import json
from unittest.mock import MagicMock, patch
import httpx
import pytest

from servo.actuation.alias_mapping import CanonicalActionType
from servo.perception.scene_index import SceneElement, SceneIndex
from servo.planner import PlannerError, get_planner, parse_subgoals
from servo.planner.base import Plan, SubGoal
from servo.planner.gemini_adapter import GeminiPlanner
from servo.planner.openai_compat import OpenAICompatPlanner
from servo.planner.passthrough import PassthroughPlanner
from servo.planner.tool_calling import MacroPlan, PlanStep


def _scene(*labels_roles):
    return SceneIndex(
        hwnd=10, window_title="App", source="uia",
        elements={
            i: SceneElement(
                element_id=i, label=label, role=role,
                bbox_screen=(0, 0, 10, 10), bbox_norm=(0, 0, 10, 10), center_screen=(5, 5),
            )
            for i, (label, role) in enumerate(labels_roles)
        },
    )


def test_passthrough_planner_click():
    subgoals = PassthroughPlanner().decompose("Click Save to disk", _scene(("Save", "Button")))
    assert len(subgoals) == 1
    assert subgoals[0].target_element_hint == "Save"
    assert subgoals[0].action_type == "click"


def test_passthrough_prefers_longest_label():
    scene = _scene(("Save", "Button"), ("Save As", "Button"))
    assert PassthroughPlanner().decompose('click "Save As"', scene)[0].target_element_hint == "Save As"


def test_passthrough_type_command_extracts_text():
    sg = PassthroughPlanner().decompose('type "bluetooth" into "Search"', _scene(("Search", "Edit")))[0]
    assert sg.action_type == "type"
    assert sg.text_to_type == "bluetooth"
    assert sg.target_element_hint == "Search"


def test_passthrough_unmatched_goal_raises_instead_of_guessing():
    with pytest.raises(PlannerError):
        PassthroughPlanner().decompose("Find Settings", _scene(("Search", "Edit")))


def test_openai_compat_failure_raises_instead_of_inventing_a_plan():
    planner = OpenAICompatPlanner(base_url="http://invalid-non-existent-domain.invalid:9999/v1", timeout_seconds=2)
    with pytest.raises(PlannerError):
        planner.decompose("Find Settings", _scene(("Search", "Edit")))


def test_parse_subgoals_handles_fences_and_wrappers():
    raw = '```json\n{"sub_goals": [{"step_index": 1, "instruction": "x", "target_element_hint": "OK"}]}\n```'
    assert parse_subgoals(raw)[0].target_element_hint == "OK"
    with pytest.raises(PlannerError):
        parse_subgoals("not json")
    with pytest.raises(PlannerError):
        parse_subgoals("[]")


def test_get_planner_factory():
    assert isinstance(get_planner("passthrough"), PassthroughPlanner)
    with pytest.raises(PlannerError):
        get_planner("nope")


def test_subgoal_from_plan_step_and_plan_from_macro_plan():
    step1 = PlanStep(
        step_index=1,
        tool_name="launch_app",
        arguments={"app_name": "notepad"},
        rationale="Open notepad",
    )
    step2 = PlanStep(
        step_index=2,
        tool_name="type_text",
        arguments={"text": "hello", "target": "editor"},
        rationale="Type text",
    )
    macro = MacroPlan(goal="open notepad and write", steps=[step1, step2])

    plan = Plan.from_macro_plan(macro)
    assert plan.goal == "open notepad and write"
    assert len(plan.sub_goals) == 2
    assert plan.sub_goals[0].action_type == "launch"
    assert plan.sub_goals[0].target_element_hint == "notepad"
    assert plan.sub_goals[1].action_type == "type"
    assert plan.sub_goals[1].text_to_type == "hello"


def test_gemini_planner_structured_tool_calling_success():
    planner = GeminiPlanner(api_key="fake-test-key")
    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {
                            "text": json.dumps([
                                {"step_index": 1, "tool_name": "launch_app", "arguments": {"app_name": "notepad"}},
                                {"step_index": 2, "tool_name": "type_text", "arguments": {"text": "hi", "target": "editor"}},
                            ])
                        }
                    ]
                }
            }
        ]
    }
    mock_resp.raise_for_status.return_value = None

    with patch("httpx.Client.post", return_value=mock_resp):
        macro_plan = planner.decompose_macro("open notepad and write hi", _scene())
        assert len(macro_plan.steps) == 2
        assert macro_plan.steps[0].canonical_action.action_type == CanonicalActionType.LAUNCH
        assert macro_plan.steps[1].canonical_action.action_type == CanonicalActionType.TYPE

        subgoals = planner.decompose("open notepad and write hi", _scene())
        assert len(subgoals) == 2
        assert subgoals[0].action_type == "launch"
        assert subgoals[1].action_type == "type"


def test_gemini_planner_rate_limit_429_deterministic_fallback():
    planner = GeminiPlanner(api_key="fake-test-key")
    mock_req = MagicMock()
    mock_resp = MagicMock(status_code=429)
    err = httpx.HTTPStatusError("Rate limited", request=mock_req, response=mock_resp)

    with patch("httpx.Client.post", side_effect=err):
        subgoals = planner.decompose("open notepad and write hello", _scene())
        assert len(subgoals) == 2
        assert subgoals[0].action_type == "launch"
        assert subgoals[0].target_element_hint == "notepad"
        assert subgoals[1].action_type == "type"
        assert subgoals[1].text_to_type == "hello"


def test_gemini_planner_connection_error_deterministic_fallback():
    planner = GeminiPlanner(api_key="fake-test-key")
    err = httpx.ConnectError("Connection refused")

    with patch("httpx.Client.post", side_effect=err):
        subgoals = planner.decompose("open calculator and click 7", _scene())
        assert len(subgoals) == 2
        assert subgoals[0].action_type == "launch"
        assert subgoals[1].action_type == "click"
        assert subgoals[1].target_element_hint == "7"


def test_gemini_planner_unparseable_goal_raises():
    planner = GeminiPlanner(api_key="fake-test-key")
    err = httpx.ConnectError("Connection refused")

    with patch("httpx.Client.post", side_effect=err):
        with pytest.raises(PlannerError):
            planner.decompose("Completely unknown arbitrary objective", _scene())


def test_openai_compat_structured_tool_calling_success():
    planner = OpenAICompatPlanner(api_key="fake-test-key")
    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "choices": [
            {
                "message": {
                    "content": json.dumps([
                        {"step_index": 1, "tool_name": "launch_app", "arguments": {"app_name": "notepad"}},
                    ])
                }
            }
        ]
    }
    mock_resp.raise_for_status.return_value = None

    with patch("httpx.Client.post", return_value=mock_resp):
        macro_plan = planner.decompose_macro("open notepad", _scene())
        assert len(macro_plan.steps) == 1
        assert macro_plan.steps[0].canonical_action.action_type == CanonicalActionType.LAUNCH

        subgoals = planner.decompose("open notepad", _scene())
        assert len(subgoals) == 1
        assert subgoals[0].action_type == "launch"


def test_openai_compat_native_tool_calls_success():
    planner = OpenAICompatPlanner(api_key="fake-test-key")
    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "launch_app",
                                "arguments": json.dumps({"app_name": "notepad"}),
                            },
                        },
                        {
                            "id": "call_2",
                            "type": "function",
                            "function": {
                                "name": "type_text",
                                "arguments": json.dumps({"text": "hello", "target": "editor"}),
                            },
                        },
                    ],
                }
            }
        ]
    }
    mock_resp.raise_for_status.return_value = None

    with patch("httpx.Client.post", return_value=mock_resp):
        macro_plan = planner.decompose_macro("open notepad and write hello", _scene())
        assert len(macro_plan.steps) == 2
        assert macro_plan.steps[0].tool_name == "launch_app"
        assert macro_plan.steps[1].tool_name == "type_text"

        subgoals = planner.decompose("open notepad and write hello", _scene())
        assert len(subgoals) == 2
        assert subgoals[0].action_type == "launch"
        assert subgoals[1].action_type == "type"


def test_openai_compat_rate_limit_429_deterministic_fallback():
    planner = OpenAICompatPlanner(api_key="fake-test-key")
    mock_req = MagicMock()
    mock_resp = MagicMock(status_code=429)
    err = httpx.HTTPStatusError("Rate limited", request=mock_req, response=mock_resp)

    with patch("httpx.Client.post", side_effect=err):
        subgoals = planner.decompose("close notepad", _scene())
        assert len(subgoals) == 1
        assert subgoals[0].action_type == "close"


def test_openai_compat_connection_error_deterministic_fallback():
    planner = OpenAICompatPlanner(base_url="http://invalid.domain:1234/v1")
    err = httpx.ConnectError("Connection failed")

    with patch("httpx.Client.post", side_effect=err):
        subgoals = planner.decompose("open notepad", _scene())
        assert len(subgoals) == 1
        assert subgoals[0].action_type == "launch"


def test_gemini_planner_hotkey_deterministic_fallback_on_429():
    planner = GeminiPlanner(api_key="fake-test-key")
    mock_req = MagicMock()
    mock_resp = MagicMock(status_code=429)
    err = httpx.HTTPStatusError("Rate limited", request=mock_req, response=mock_resp)

    with patch("httpx.Client.post", side_effect=err):
        subgoals = planner.decompose("press ctrl+s", _scene())
        assert len(subgoals) == 1
        assert subgoals[0].action_type == "hotkey"
        assert subgoals[0].tool_name == "send_hotkey"
        assert subgoals[0].canonical_action.keys == ["ctrl", "s"]


def test_openai_compat_hotkey_deterministic_fallback_on_connection_error():
    planner = OpenAICompatPlanner(base_url="http://invalid.domain:1234/v1")
    err = httpx.ConnectError("Connection failed")

    with patch("httpx.Client.post", side_effect=err):
        subgoals = planner.decompose("press alt+f4", _scene())
        assert len(subgoals) == 1
        assert subgoals[0].action_type == "hotkey"
        assert subgoals[0].tool_name == "send_hotkey"
        assert subgoals[0].canonical_action.keys == ["alt", "f4"]


