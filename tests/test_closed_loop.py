"""Unit tests for the Closed Loop State Machine and Recovery Ladder."""

from servo.control.loop import ServoControlLoop, ExecutionResult
from servo.perception.scene_index import SceneIndex, SceneElement


def test_control_loop_attempt_1_success(monkeypatch):
    loop = ServoControlLoop(hwnd=1001, fast_mode=True)

    mock_scene = SceneIndex(
        hwnd=1001,
        window_title="Test App",
        source="uia",
        elements={
            0: SceneElement(
                element_id=0,
                label="Submit",
                role="Button",
                bbox_screen=(100, 100, 200, 140),
                bbox_norm=(100, 100, 200, 140),
                center_screen=(150, 120),
            ),
            1: SceneElement(
                element_id=1,
                label="Cancel",
                role="Button",
                bbox_screen=(210, 100, 310, 140),
                bbox_norm=(210, 100, 310, 140),
                center_screen=(260, 120),
            ),
            2: SceneElement(
                element_id=2,
                label="Help",
                role="Button",
                bbox_screen=(320, 100, 420, 140),
                bbox_norm=(320, 100, 420, 140),
                center_screen=(370, 120),
            ),
        },
    )

    # Mock UIA sensor
    monkeypatch.setattr(loop.uia_sensor, "sense", lambda: mock_scene)

    # Mock tool click to not perform actual OS input
    monkeypatch.setattr(loop.tools, "click", lambda scene, el_id, button="left": "Clicked")

    # Mock settling to return a changed scene
    changed_scene = mock_scene.model_copy(deep=True)
    changed_scene.window_title = "Test App - Submitted"
    monkeypatch.setattr(
        loop.settling,
        "settle_and_diff",
        lambda before, fn: (changed_scene, SceneIndex.compute_diff if False else None),
    )
    from servo.perception.scene_index import SceneDelta
    monkeypatch.setattr(
        loop.settling,
        "settle_and_diff",
        lambda before, fn: (changed_scene, SceneDelta(has_changed=True, title_changed=True)),
    )

    result = loop.execute_subgoal(
        step_index=1,
        sub_goal="Click Submit Button",
        target_label_hint="Submit",
        action_type="click",
    )

    assert result.success is True
    assert result.attempts_used == 1
    assert result.step_log.jev_post_ok is True
