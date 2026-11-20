"""Unit tests for System 2 Planners."""

from servo.planner.base import SubGoal, Plan
from servo.planner.openai_compat import OpenAICompatPlanner
from servo.planner.passthrough import PassthroughPlanner
from servo.perception.scene_index import SceneIndex, SceneElement


def test_passthrough_planner():
    planner = PassthroughPlanner()
    scene = SceneIndex(
        hwnd=10,
        window_title="App",
        source="uia",
        elements={
            0: SceneElement(
                element_id=0,
                label="Save",
                role="Button",
                bbox_screen=(0, 0, 10, 10),
                bbox_norm=(0, 0, 10, 10),
                center_screen=(5, 5),
            )
        },
    )

    subgoals = planner.decompose("Click Save to disk", scene)
    assert len(subgoals) == 1
    assert subgoals[0].target_element_hint == "Save"
    assert subgoals[0].action_type == "click"


def test_openai_compat_heuristic_fallback():
    planner = OpenAICompatPlanner(base_url="http://invalid-non-existent-domain:9999/v1")
    scene = SceneIndex(
        hwnd=10,
        window_title="App",
        source="uia",
        elements={
            0: SceneElement(
                element_id=0,
                label="Search",
                role="Edit",
                bbox_screen=(0, 0, 10, 10),
                bbox_norm=(0, 0, 10, 10),
                center_screen=(5, 5),
            )
        },
    )

    # Should fallback gracefully to single sub-goal without raising unhandled exception
    subgoals = planner.decompose("Find Settings", scene)
    assert len(subgoals) >= 1
    assert subgoals[0].instruction == "Find Settings"
