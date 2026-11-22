"""Step results must be honest: confirmed, unconfirmed, or failed, never a false "done"."""

import pytest

from servo.actuation.alias_mapping import AliasMapper, CanonicalActionType, _label_synonyms
from servo.perception.scene_index import SceneElement, SceneIndex


def _scene(*labels):
    els = {}
    for i, lab in enumerate(labels):
        els[i] = SceneElement(element_id=i, role="Button", label=lab, bbox_screen=(0, 0, 10, 10),
                              bbox_norm=(0, 0, 10, 10), center_screen=(5, 5))
    return SceneIndex(hwnd=1, window_title="T", source="uia", elements=els)


# ----------------------------------------------------------- resolution

def test_digit_matches_spoken_label():
    scene = _scene("Seven", "Seventeen", "Plus", "Equals")
    assert AliasMapper.resolve_element(scene, "7")[0].label == "Seven"
    assert AliasMapper.resolve_element(scene, "+")[0].label == "Plus"
    assert AliasMapper.resolve_element(scene, "=")[0].label == "Equals"
    assert _label_synonyms("seven") == ("7",)


def test_control_type_word_is_dropped_only_as_fallback():
    scene = _scene("Save", "Submit Button")
    assert AliasMapper.resolve_element(scene, "Save button")[0].label == "Save"
    assert AliasMapper.resolve_element(scene, "Submit Button")[0].label == "Submit Button"


def test_click_on_grammar():
    from servo.cognition.needle import parse_grammar
    assert parse_grammar("click on person 1").steps[0].arguments == {"target": "person 1"}
    assert parse_grammar("click the Save").steps[0].arguments == {"target": "Save"}


# --------------------------------------------------------- needle guard

def test_needle_cannot_close_on_pause(monkeypatch):
    from servo.cognition import cactus_runtime, needle
    monkeypatch.setattr(cactus_runtime, "get_needle", lambda tools: object())
    monkeypatch.setattr(cactus_runtime, "needle_complete", lambda agent, text: {
        "function_calls": [{"name": "close_window", "arguments": {"app_name": "YouTube"}}], "confidence": 0.93})
    t1 = needle.Tier1Intent()
    assert t1.propose("Pause the YouTube video") is None          # rejected -> escalates
    assert t1.propose("close youtube") is not None                 # literally asked for


# ----------------------------------------------------------- handoff

class _Tracer:
    def emit(self, *a, **k): pass


def _coordinator(monkeypatch, own=frozenset()):
    from servo.control import handoff
    monkeypatch.setattr(handoff.windows, "is_window", lambda h: bool(h))
    monkeypatch.setattr(handoff.windows, "get_window_title", lambda h: {7: "Servo Copilot", 9: "Notes - Notepad"}.get(h, ""))
    return handoff.SystemHandoffCoordinator(target_hwnd=None, exclude_hwnds=lambda: set(own))


def _plan(tool, **args):
    from servo.cognition.needle import _step
    from servo.planner.tool_calling import MacroPlan
    return MacroPlan(goal="g", steps=[_step(1, tool, args, tool)])


def test_launch_never_succeeds_by_focusing_the_copilot(monkeypatch):
    coord = _coordinator(monkeypatch, own={7})
    summary = coord.execute_plan(_plan("launch_app", app_name="any browser"),
                                 app_launcher=lambda name: (7, "Servo Copilot"))
    rep = summary.step_reports[0]
    assert not rep.success and "Servo Copilot" in rep.description


def test_close_does_not_launch_a_missing_app(monkeypatch):
    from servo.daemon import copilot
    launched = []
    monkeypatch.setattr(copilot, "launch_or_focus_app", lambda n: launched.append(n) or (9, "x"))
    monkeypatch.setattr(copilot, "find_app_window", lambda n: None)
    summary = _coordinator(monkeypatch).execute_plan(_plan("close_window", app_name="spotify"))
    rep = summary.step_reports[0]
    assert launched == []
    assert rep.verification == "unconfirmed" and "Nothing to close" in rep.description


def test_close_refuses_the_copilot(monkeypatch):
    from servo.daemon import copilot
    monkeypatch.setattr(copilot, "find_app_window", lambda n: 7)
    rep = _coordinator(monkeypatch, own={7}).execute_plan(_plan("close_window", app_name="edge")).step_reports[0]
    assert not rep.success and "copilot" in (rep.error or "").lower()


# ------------------------------------------------------------- replies

def test_reply_names_unconfirmed_steps():
    from servo.cognition.router import StepRecord, _summary_reply
    recs = [StepRecord(command="open notepad", tool="launch_app", tier="uia", success=True),
            StepRecord(command="click Play", tool="click_element", tier="uia", success=True,
                       verification="unconfirmed", note="no visible change")]
    reply = _summary_reply(recs)
    assert "couldn't confirm" in reply and "click Play" in reply and "no visible change" in reply
    assert _summary_reply(recs[:1]) == "Done — 1 step completed."
