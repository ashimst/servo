"""Unit tests for the Closed Loop State Machine and Recovery Ladder."""

import pytest

from servo.control.loop import ServoControlLoop
from servo.perception.scene_index import SceneDelta, SceneElement, SceneIndex


def _el(i, label, role="Button", value=None):
    x = 100 + i * 110
    return SceneElement(
        element_id=i, label=label, role=role,
        bbox_screen=(x, 100, x + 100, 140), bbox_norm=(x, 100, x + 100, 140),
        center_screen=(x + 50, 120), value=value,
    )


@pytest.fixture
def scene():
    return SceneIndex(
        hwnd=1001, window_title="Test App", source="uia",
        elements={0: _el(0, "Submit"), 1: _el(1, "Cancel"), 2: _el(2, "Help")},
    )


@pytest.fixture
def loop(monkeypatch, scene):
    lp = ServoControlLoop(hwnd=1001, fast_mode=True, mode="assistant")
    lp.clicks = []
    monkeypatch.setattr(lp.uia_sensor, "sense", lambda: scene)
    monkeypatch.setattr(lp.tools, "click", lambda s, el_id, button="left": lp.clicks.append(el_id) or "Clicked")
    # Mirage unavailable: empty, error-flagged scene (the real behaviour without cactus).
    monkeypatch.setattr(
        lp.mirage_sensor, "sense",
        lambda window_title="": SceneIndex(hwnd=1001, window_title=window_title, source="mirage", sense_error="no mirage"),
    )
    return lp


def _settle_changed(lp, monkeypatch, scene):
    changed = scene.model_copy(deep=True)
    changed.window_title = "Test App - Done"
    monkeypatch.setattr(
        lp.settling, "settle_and_diff",
        lambda before, fn: (changed, SceneDelta(has_changed=True, title_changed=True, new_title="Test App - Done")),
    )


def _settle_unchanged(lp, monkeypatch, scene):
    monkeypatch.setattr(lp.settling, "settle_and_diff", lambda before, fn: (scene, SceneDelta(has_changed=False)))


def test_low_risk_click_succeeds_on_first_attempt(loop, monkeypatch, scene):
    _settle_changed(loop, monkeypatch, scene)
    result = loop.execute_subgoal(1, "Open the help page", "Help", action_type="click")
    assert result.success is True
    assert result.attempts_used == 1
    assert loop.clicks == [2]


def test_medium_risk_without_confirmation_channel_is_refused(loop, monkeypatch, scene):
    _settle_changed(loop, monkeypatch, scene)
    result = loop.execute_subgoal(1, "Click Submit Button", "Submit", action_type="click")
    assert result.success is False
    assert loop.clicks == []
    assert "requires confirmation" in (result.step_log.error or "")


def test_medium_risk_with_approval_proceeds(loop, monkeypatch, scene):
    _settle_changed(loop, monkeypatch, scene)
    loop.confirmation_callback = lambda req: True
    result = loop.execute_subgoal(1, "Click Submit Button", "Submit", action_type="click")
    assert result.success is True
    assert loop.clicks == [0]


def test_medium_risk_rejected_by_user(loop, monkeypatch, scene):
    _settle_changed(loop, monkeypatch, scene)
    loop.confirmation_callback = lambda req: False
    result = loop.execute_subgoal(1, "Click Submit Button", "Submit", action_type="click")
    assert result.success is False
    assert loop.clicks == []
    assert result.escalation_required is False


def test_unmatched_hint_never_clicks_an_arbitrary_element(loop, monkeypatch, scene):
    _settle_changed(loop, monkeypatch, scene)
    result = loop.execute_subgoal(1, "click the save button", "click the save button")
    assert result.success is False
    assert result.escalation_required is True
    assert loop.clicks == []


def test_failed_verification_does_not_actuate_twice(loop, monkeypatch, scene):
    _settle_unchanged(loop, monkeypatch, scene)
    result = loop.execute_subgoal(1, "Open the help page", "Help")
    assert result.success is False
    assert result.actuated is True
    assert result.escalation_required is True
    assert loop.clicks == [2]  # exactly one click


def test_ambiguous_partial_match_is_refused(loop, monkeypatch, scene):
    scene.elements[3] = _el(3, "Help Center")
    scene.elements[4] = _el(4, "Help Topics")
    _settle_changed(loop, monkeypatch, scene)
    # Exact label wins even though "Help" is also a substring of two other labels.
    assert loop.execute_subgoal(1, "open", "Help").success is True
    assert loop.clicks == [2]
    loop.clicks.clear()
    # A unique partial match is accepted.
    assert loop.execute_subgoal(2, "open", "Help T").success is True
    assert loop.clicks == [4]
    loop.clicks.clear()
    # A partial match hitting several elements is refused.
    del scene.elements[2]
    result = loop.execute_subgoal(3, "open", "Help")
    assert result.success is False
    assert loop.clicks == []


def test_unsupported_action_type_is_rejected(loop, monkeypatch, scene):
    _settle_changed(loop, monkeypatch, scene)
    result = loop.execute_subgoal(1, "scroll down", "Help", action_type="scroll")
    assert result.success is False
    assert loop.clicks == []


def test_type_without_text_is_rejected(loop, monkeypatch, scene):
    result = loop.execute_subgoal(1, "type into help", "Help", action_type="type", text_to_type="")
    assert result.success is False
    assert "non-empty" in (result.step_log.error or "")


def test_harness_mode_without_allowlist_refuses(loop, monkeypatch, scene):
    from servo.config import settings
    monkeypatch.setattr(settings, "whitelisted_hwnds", [])
    monkeypatch.setattr(settings, "whitelisted_process_names", [])
    loop.mode = "harness"
    _settle_changed(loop, monkeypatch, scene)
    result = loop.execute_subgoal(1, "Open the help page", "Help")
    assert result.success is False
    assert loop.clicks == []
    assert "allowlist" in (result.step_log.error or "").lower() or "WHITELISTED" in (result.step_log.error or "")


def test_harness_mode_with_allowlisted_hwnd_runs_without_confirmation(loop, monkeypatch, scene):
    from servo.config import settings
    monkeypatch.setattr(settings, "whitelisted_hwnds", [1001])
    loop.mode = "harness"
    _settle_changed(loop, monkeypatch, scene)
    result = loop.execute_subgoal(1, "Click Submit Button", "Submit")
    assert result.success is True
    assert loop.clicks == [0]


def test_confirmation_request_carries_full_context(loop, monkeypatch, scene):
    _settle_changed(loop, monkeypatch, scene)
    seen = []
    loop.confirmation_callback = lambda req: seen.append(req) or True
    result = loop.execute_subgoal(1, "Click Submit Button", "Submit")
    assert result.step_log.confirmation == "approved"
    req = seen[0]
    assert req.risk_level == "medium"
    assert req.element_label == "Submit"
    assert req.tool_call == "click(element_id=0)"
    assert req.window_title == "Test App"
    assert req.hwnd == 1001


def test_rejection_is_recorded(loop, monkeypatch, scene):
    _settle_changed(loop, monkeypatch, scene)
    loop.confirmation_callback = lambda req: False
    result = loop.execute_subgoal(1, "Click Submit Button", "Submit")
    assert result.step_log.confirmation == "rejected"


def test_callback_exception_counts_as_rejection(loop, monkeypatch, scene):
    _settle_changed(loop, monkeypatch, scene)

    def boom(req):
        raise RuntimeError("dialog crashed")

    loop.confirmation_callback = boom
    assert loop.execute_subgoal(1, "Click Submit Button", "Submit").success is False
    assert loop.clicks == []


def test_late_approval_after_cancellation_does_not_act(loop, monkeypatch, scene):
    _settle_changed(loop, monkeypatch, scene)
    cancelled = {"v": False}

    def slow_human(req):
        cancelled["v"] = True  # caller timed out while the dialog was open
        return True

    loop.confirmation_callback = slow_human
    loop.should_abort = lambda: cancelled["v"]
    result = loop.execute_subgoal(1, "Click Submit Button", "Submit")
    assert result.success is False
    assert loop.clicks == []
    assert "cancelled" in (result.step_log.error or "")
