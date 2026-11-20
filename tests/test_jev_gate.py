"""Unit tests for JevGate pre-action risk and post-action state transition evaluation."""

from servo.control.jev_gate import JevGate
from servo.perception.scene_index import SceneDelta


def test_jev_pre_risk_heuristic():
    gate = JevGate(use_fallback=True)

    # Low risk
    res_low = gate.evaluate_pre_risk("Click search box", "click(0)", "Search")
    assert res_low.risk_level == "low"
    assert res_low.scope_valid is True

    # Medium risk
    res_med = gate.evaluate_pre_risk("Save document to disk", "click(1)", "Save")
    assert res_med.risk_level == "medium"

    # High risk
    res_high = gate.evaluate_pre_risk("Delete user account and wipe database", "click(2)", "Delete")
    assert res_high.risk_level == "high"


def test_jev_post_transition_heuristic():
    gate = JevGate(use_fallback=True)

    # Successful mutation
    delta_success = SceneDelta(
        has_changed=True,
        title_changed=False,
        added_labels=["Dialog Opened"],
    )
    res = gate.evaluate_post_transition("Open dialog", delta_success)
    assert res.transition_ok is True
    assert res.confidence > 0.8

    # No mutation detected
    delta_empty = SceneDelta(has_changed=False)
    res_fail = gate.evaluate_post_transition("Open dialog", delta_empty)
    assert res_fail.transition_ok is False
