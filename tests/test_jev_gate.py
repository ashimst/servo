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

    # No visible change is not proof of failure (a video starting changes nothing in the
    # accessibility tree): the step continues, but is reported as unconfirmed.
    none = gate.evaluate_post_transition("Open dialog", SceneDelta(has_changed=False))
    assert none.transition_ok is True and none.verdict == "unconfirmed"


def test_readback_decides_typing_verdict():
    gate = JevGate(use_fallback=True)
    nothing = SceneDelta(has_changed=False)
    ok = gate.evaluate_post_transition("type", nothing, typed_text="hello world",
                                       observed_text="## Old notes\r\nhello   world")
    assert ok.verdict == "confirmed" and ok.source == "readback"
    bad = gate.evaluate_post_transition("type", SceneDelta(has_changed=True, added_labels=["x"]),
                                        typed_text="hello world", observed_text="hello wor")
    assert bad.verdict == "failed" and "hello wor" in bad.explanation
    # Unreadable control + some change: honest "unconfirmed", not a guess either way.
    unk = gate.evaluate_post_transition("type", SceneDelta(has_changed=True, added_labels=["x"]),
                                        typed_text="hello")
    assert unk.verdict == "unconfirmed"


def test_jev_post_transition_checks_typed_text():
    gate = JevGate(use_fallback=True)
    ok = SceneDelta(has_changed=True, value_changes={"Search": {"old_value": "", "new_value": "bluetooth"}})
    assert gate.evaluate_post_transition("type", ok, typed_text="bluetooth").transition_ok is True
    wrong = SceneDelta(has_changed=True, value_changes={"Search": {"old_value": "", "new_value": "blue"}})
    assert gate.evaluate_post_transition("type", wrong, typed_text="bluetooth").transition_ok is False


def test_readback_treats_carriage_returns_as_line_breaks():
    gate = JevGate(use_fallback=True)
    typed = "Rain falls soft,\nA silver hush.\n\nPitter-patter on the roof"
    notepad = "Rain falls soft,\rA silver hush.\r\rPitter-patter on the roof"   # how Notepad stores it
    assert gate.evaluate_post_transition("type", SceneDelta(), typed_text=typed, observed_text=notepad).verdict == "confirmed"
