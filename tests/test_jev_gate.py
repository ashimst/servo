"""Laya gate: every decision is Laya's; the safety floor and typed-text readback stay deterministic."""

import pytest

from servo.config import settings
from servo.control.jev_gate import JevGate, LayaUnavailable
from servo.perception.scene_index import SceneDelta


# ------------------------------------------------------------ unavailable Laya

def test_unconfigured_laya_raises():
    gate = JevGate(endpoint_url="")
    with pytest.raises(LayaUnavailable):
        gate.evaluate_pre_risk("Click search box", "click(0)", "Search")
    with pytest.raises(LayaUnavailable):
        gate.assess_goal("open notepad")
    with pytest.raises(LayaUnavailable):
        gate.evaluate_post_transition("Open dialog", SceneDelta(has_changed=True))


def test_unreachable_laya_raises():
    # conftest makes LayaClient.ask raise, as a refused connection does.
    with pytest.raises(LayaUnavailable):
        JevGate().evaluate_pre_risk("Click search box", "click(0)", "Search")


def test_default_laya_url_is_the_local_server():
    from servo.config import ServoConfig
    assert ServoConfig.model_fields["laya_url"].default == "http://127.0.0.1:8009"


@pytest.mark.parametrize("answers", [
    {},
    {"risk_level": {"type": "score", "probabilities": {}}, "scope_valid": {"type": "noul", "noul": 0.9}},
    {"risk_level": {"type": "score", "probabilities": {"0": 0.9}}},
    {"risk_level": {"type": "score", "probabilities": {"0": "x"}}, "scope_valid": {"type": "noul", "noul": 0.9}},
])
def test_malformed_risk_answer_raises(monkeypatch, answers):
    gate = JevGate()
    monkeypatch.setattr(gate, "_ask", lambda state, questions: answers)
    with pytest.raises(LayaUnavailable):
        gate.evaluate_pre_risk("Click", "click(0)", "OK")


def test_malformed_routing_and_verification_answers_raise(monkeypatch):
    gate = JevGate()
    monkeypatch.setattr(gate, "_ask", lambda state, questions: {
        "desktop_task": {"type": "noul", "noul": 0.9},
        "complexity": {"type": "choice", "choice": "lots"},   # not an allowed option
        "outcome": {"type": "choice", "choice": "maybe"}})
    with pytest.raises(LayaUnavailable):
        gate.assess_goal("open notepad")
    with pytest.raises(LayaUnavailable):
        gate.evaluate_post_transition("Open dialog", SceneDelta(has_changed=True))


# ------------------------------------------------------------------- risk

def test_laya_risk_and_scope(laya):
    gate = JevGate()
    res = gate.evaluate_pre_risk("Click search box", "click(0)", "Search")
    assert res.risk_level == "low" and res.scope_valid is True and res.source == "laya"
    laya.risk = "medium"
    assert gate.evaluate_pre_risk("Click search box", "click(0)", "Search").risk_level == "medium"
    laya.scope = 0.2
    assert gate.evaluate_pre_risk("Click search box", "click(0)", "Search").scope_valid is False


def test_high_probability_over_threshold_counts_as_high(laya):
    laya.risk = {"low": 0.5, "medium": 0.1, "high": max(settings.laya_high_risk_floor, 0.4)}
    assert JevGate().evaluate_pre_risk("Click", "click(0)", "OK").risk_level == "high"


def test_floor_raises_laya_low_risk(laya):
    gate = JevGate()
    assert gate.evaluate_pre_risk("Save document to disk", "click(1)", "Save").risk_level == "medium"
    res = gate.evaluate_pre_risk("Delete user account and wipe database", "click(2)", "Delete")
    assert res.risk_level == "high" and "safety floor" in res.reason


def test_floor_keywords_match_words_not_substrings(laya):
    gate = JevGate()
    assert gate.evaluate_pre_risk("Click More information", "click(0)", "More information").risk_level == "low"
    assert gate.evaluate_pre_risk("Toggle border", "click(0)", "Border").risk_level == "low"
    assert gate.evaluate_pre_risk("Open Resend tab", "click(0)", "Resend").risk_level == "low"
    assert gate.evaluate_pre_risk("Start formatting drive", "click(0)", "Go").risk_level == "high"
    assert gate.evaluate_pre_risk("Sending message", "click(0)", "Go").risk_level == "high"


def test_floor_ignores_the_hotkey_executor_name(laya):
    gate = JevGate()
    assert gate.evaluate_pre_risk("Undo", "send_hotkey(keys=['ctrl','z'])", "").risk_level == "low"
    assert gate.evaluate_pre_risk("Save the draft", "send_hotkey(keys=['ctrl','s'])", "").risk_level == "medium"
    assert gate.evaluate_pre_risk("Send the email", "send_hotkey(keys=['ctrl','enter'])", "").risk_level == "high"


def test_floor_never_lowers_laya(laya):
    laya.risk = "high"
    assert JevGate().evaluate_pre_risk("Click search box", "click(0)", "Search").risk_level == "high"


# ----------------------------------------------------------- verification

def test_readback_decides_typing_verdict_before_laya():
    # No Laya at all (conftest): the read-back alone decides.
    gate = JevGate()
    nothing = SceneDelta(has_changed=False)
    ok = gate.evaluate_post_transition("type", nothing, typed_text="hello world",
                                       observed_text="## Old notes\r\nhello   world")
    assert ok.verdict == "confirmed" and ok.source == "readback"
    bad = gate.evaluate_post_transition("type", SceneDelta(has_changed=True, added_labels=["x"]),
                                        typed_text="hello world", observed_text="hello wor")
    assert bad.verdict == "failed" and "hello wor" in bad.explanation and bad.transition_ok is False


def test_unreadable_typing_goes_to_laya(laya):
    laya.outcome = "unconfirmed"
    res = JevGate().evaluate_post_transition("type", SceneDelta(has_changed=True, added_labels=["x"]),
                                             typed_text="hello")
    assert res.verdict == "unconfirmed" and res.source == "laya"
    state, questions = laya.calls[-1]
    assert '"hello"' in state["action"] and questions["outcome"]["type"] == "choice"
    assert state["observed"] == "The window is still ''. Appeared: 'x'."


@pytest.mark.parametrize("choice,ok", [("confirmed", True), ("unconfirmed", True), ("failed", False)])
def test_choice_verdict_mapping(laya, choice, ok):
    laya.outcome = choice
    res = JevGate().evaluate_post_transition("Open dialog", SceneDelta(has_changed=True, added_labels=["Dialog"]))
    assert res.verdict == choice and res.transition_ok is ok and res.source == "laya"
    assert set(laya.calls[-1][1]["outcome"]["criteria"]) == {"confirmed", "unconfirmed", "failed"}


def test_readback_treats_carriage_returns_as_line_breaks():
    gate = JevGate()
    typed = "Rain falls soft,\nA silver hush.\n\nPitter-patter on the roof"
    notepad = "Rain falls soft,\rA silver hush.\r\rPitter-patter on the roof"   # how Notepad stores it
    assert gate.evaluate_post_transition("type", SceneDelta(), typed_text=typed, observed_text=notepad).verdict == "confirmed"


# ---------------------------------------------------------------- routing

def test_assess_goal_from_laya(laya):
    laya.desktop, laya.complexity = 0.8, "multi"
    a = JevGate().assess_goal("rename report.txt and email it")
    assert a.desktop_task is True and a.complexity == "multi" and a.source == "laya"
    laya.desktop = 0.1
    assert JevGate().assess_goal("tell me a joke").desktop_task is False
