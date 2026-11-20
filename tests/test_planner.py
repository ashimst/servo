"""Unit tests for System 2 Planners."""

import pytest

from servo.perception.scene_index import SceneElement, SceneIndex
from servo.planner import PlannerError, get_planner, parse_subgoals
from servo.planner.openai_compat import OpenAICompatPlanner
from servo.planner.passthrough import PassthroughPlanner


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
