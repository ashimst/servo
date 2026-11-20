"""Unit tests for JevGate pre-action risk and post-action state transition evaluation."""

from servo.control.jev_gate import JevGate
from servo.perception.scene_index import SceneDelta


def test_jev_pre_risk_heuristic():
    gate = JevGate(use_fallback=True)

    res_low = gate.evaluate_pre_risk("Click search box", "click(0)", "Search")
    assert res_low.risk_level == "low"
    assert res_low.scope_valid is True

    assert gate.evaluate_pre_risk("Save document to disk", "click(1)", "Save").risk_level == "medium"
    assert gate.evaluate_pre_risk("Delete user account and wipe database", "click(2)", "Delete").risk_level == "high"


def test_jev_keywords_match_words_not_substrings():
    gate = JevGate(use_fallback=True)
    # Previously all "high" due to substring matches (format/order/send).
    assert gate.evaluate_pre_risk("Click More information", "click(0)", "More information").risk_level == "low"
    assert gate.evaluate_pre_risk("Toggle border", "click(0)", "Border").risk_level == "low"
    assert gate.evaluate_pre_risk("Open Resend tab", "click(0)", "Resend").risk_level == "low"
    # Inflected forms still match.
    assert gate.evaluate_pre_risk("Start formatting drive", "click(0)", "Go").risk_level == "high"
    assert gate.evaluate_pre_risk("Sending message", "click(0)", "Go").risk_level == "high"


def test_jev_post_transition_heuristic():
    gate = JevGate(use_fallback=True)

    delta_success = SceneDelta(has_changed=True, added_labels=["Dialog Opened"])
    res = gate.evaluate_post_transition("Open dialog", delta_success, expected_outcome="Dialog opened")
    assert res.transition_ok is True
    assert res.confidence > 0.8

    unmatched = gate.evaluate_post_transition("Open dialog", delta_success, expected_outcome="Settings visible")
    assert unmatched.transition_ok is True
    assert unmatched.confidence < 0.6

    assert gate.evaluate_post_transition("Open dialog", SceneDelta(has_changed=False)).transition_ok is False


def test_jev_post_transition_checks_typed_text():
    gate = JevGate(use_fallback=True)
    ok = SceneDelta(has_changed=True, value_changes={"Search": {"old_value": "", "new_value": "bluetooth"}})
    assert gate.evaluate_post_transition("type", ok, typed_text="bluetooth").transition_ok is True
    wrong = SceneDelta(has_changed=True, value_changes={"Search": {"old_value": "", "new_value": "blue"}})
    assert gate.evaluate_post_transition("type", wrong, typed_text="bluetooth").transition_ok is False
