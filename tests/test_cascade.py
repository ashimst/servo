"""Cognitive cascade: trace bus, Laya contract, Needle (Tier 1), Mirage parsing, System 2
tiers and router routing. Every model is a fake (see conftest.py): no rule-based stand-ins."""

from __future__ import annotations

import json
import threading
from typing import Any, Dict, List, Optional

import httpx
import pytest

from servo.cognition import cactus_runtime, system2 as system2_mod
from servo.cognition.needle import NEEDLE_TOOLS, NeedleError, Tier1Intent, to_steps
from servo.cognition.planners import AGENTS as PLANNER_AGENTS, DEFAULT_AGENT, agent as planner_agent, plan_system
from servo.cognition.specialists import SPECIALISTS, STEP_FORMS, step_form
from servo.cognition.router import CascadeRouter, StepOutcome, classify_error, describe_scene
from servo.cognition.system2 import System2, System2Result, System2Unavailable, _parse_plan, _split_think
from servo.cognition.trace import TraceBus, Tracer
from servo.config import settings
from servo.control.confirmation import ConfirmationRequest
from servo.control.jev_gate import JevGate, LayaClient, LayaUnavailable
from servo.perception.mirage_sensor import Detection, match_target, parse_detections
from servo.perception.scene_index import SceneDelta, SceneElement, SceneIndex

# The real HTTP client, captured before conftest replaces it for each test.
_REAL_LAYA_ASK = LayaClient.ask
_REAL_GET_NEEDLE = cactus_runtime.get_needle


def tracer() -> Tracer:
    return Tracer("t", TraceBus())


# ------------------------------------------------------------------ trace bus

def test_span_closes_in_place_with_timing():
    bus = TraceBus()
    got: List[Any] = []
    bus.subscribe(got.append)
    tr = Tracer("r1", bus)
    with tr.span("tier", "outer") as sp:
        tr.emit("perception", "inner")
        sp.done("ok", "finished", confidence=0.8)
    assert [e.title for e in got] == ["outer", "inner", "outer"]
    assert got[0].status == "running" and got[2].status == "ok"
    assert got[0].seq == got[2].seq                      # UI replaces the row
    assert got[1].parent == got[0].seq                    # nesting
    assert got[2].duration_ms is not None and got[2].confidence == 0.8
    assert tr.events[0].status == "ok"


def test_broken_subscriber_does_not_break_run():
    bus = TraceBus()
    bus.subscribe(lambda e: (_ for _ in ()).throw(RuntimeError("socket gone")))
    Tracer("r", bus).emit("goal", "still fine")


# ------------------------------------------------------------- Laya contract

class _Transport:
    def __init__(self, answers: Dict[str, Any]) -> None:
        self.answers = answers
        self.requests: List[Dict[str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append({"url": str(request.url), "body": json.loads(request.content), "headers": dict(request.headers)})
        return httpx.Response(200, json={"model": "english", "answers": self.answers})


@pytest.fixture
def laya_server(monkeypatch):
    """The real LayaClient over a mock HTTP transport."""
    monkeypatch.setattr(LayaClient, "ask", _REAL_LAYA_ASK)

    def make(answers: Dict[str, Any]) -> _Transport:
        t = _Transport(answers)
        real = httpx.Client
        monkeypatch.setattr(httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(t.handler), **kw))
        return t
    return make


def test_laya_request_uses_system_one_contract(laya_server):
    t = laya_server({
        "risk_level": {"type": "score", "score": 0.2, "confidence": 0.8, "legend": {"0": "low", "1": "medium", "2": "high"},
                       "probabilities": {"0": 0.85, "1": 0.1, "2": 0.05}},
        "scope_valid": {"type": "noul", "noul": 0.97},
    })
    gate = JevGate(endpoint_url="http://127.0.0.1:8009")
    res = gate.evaluate_pre_risk("Click the 7 button", "click(element_id=3)", "7")
    req = t.requests[0]
    assert req["url"] == "http://127.0.0.1:8009/v1/systemone"
    body = req["body"]
    assert set(body) == {"state", "model", "questions"} and body["model"] == "english"
    assert body["questions"]["risk_level"]["type"] == "score"
    assert body["questions"]["risk_level"]["criteria"] == ["low", "medium", "high"]
    assert body["questions"]["scope_valid"]["type"] == "noul"
    assert res.source == "laya" and res.risk_level == "low" and res.scope_valid


def test_laya_cannot_lower_keyword_risk(laya_server):
    laya_server({
        "risk_level": {"type": "score", "probabilities": {"0": 0.9, "1": 0.08, "2": 0.02}},
        "scope_valid": {"type": "noul", "noul": 0.9},
    })
    res = JevGate(endpoint_url="http://laya").evaluate_pre_risk("Delete the file", "click(1)", "Delete")
    assert res.risk_level == "high" and "safety floor" in res.reason


def test_laya_high_mass_upgrades_to_high(laya_server):
    laya_server({
        "risk_level": {"type": "score", "probabilities": {"0": 0.45, "1": 0.25, "2": 0.30}},
        "scope_valid": {"type": "noul", "noul": 0.9},
    })
    res = JevGate(endpoint_url="http://laya").evaluate_pre_risk("Click OK", "click(1)", "OK")
    assert res.risk_level == "high"


def test_laya_malformed_answer_raises(laya_server):
    laya_server({"risk_level": {"type": "score"}})
    with pytest.raises(LayaUnavailable):
        JevGate(endpoint_url="http://laya").evaluate_pre_risk("Click OK", "click(1)", "OK")


def test_laya_http_error_raises(monkeypatch):
    monkeypatch.setattr(LayaClient, "ask", _REAL_LAYA_ASK)
    real = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: real(
        transport=httpx.MockTransport(lambda r: httpx.Response(500, json={})), **kw))
    with pytest.raises(LayaUnavailable, match="failed"):
        JevGate(endpoint_url="http://laya").assess_goal("open notepad")


def test_laya_routing_question(laya_server):
    t = laya_server({
        "desktop_task": {"type": "noul", "noul": 0.95},
        "complexity": {"type": "choice", "choice": "multi", "confidence": 0.6,
                       "probabilities": {"single": 0.2, "multi": 0.7, "ambiguous": 0.1}},
        "domain": {"type": "choice", "choice": "web", "confidence": 0.8,
                   "probabilities": {"apps": 0.1, "web": 0.8, "settings": 0.1}},
    })
    a = JevGate(endpoint_url="http://laya").assess_goal("find the cheapest flight and book it")
    assert a.source == "laya" and a.desktop_task and a.complexity == "multi"
    assert a.domain == "web" and a.domain_probabilities["web"] == 0.8
    q = t.requests[0]["body"]["questions"]
    assert q["complexity"]["type"] == "choice"
    assert q["domain"]["type"] == "choice" and set(q["domain"]["criteria"]) == {"apps", "settings", "files", "web", "code"}


def test_laya_routing_without_a_domain_is_malformed(laya_server):
    laya_server({
        "desktop_task": {"type": "noul", "noul": 0.95},
        "complexity": {"type": "choice", "choice": "single", "probabilities": {"single": 0.9}},
    })
    with pytest.raises(LayaUnavailable, match="malformed"):
        JevGate(endpoint_url="http://laya").assess_goal("open notepad")


def test_laya_verification_is_a_choice_question(laya_server):
    t = laya_server({"outcome": {"type": "choice", "choice": "failed", "confidence": 0.8,
                                "probabilities": {"confirmed": 0.1, "unconfirmed": 0.1, "failed": 0.8}}})
    res = JevGate(endpoint_url="http://laya").evaluate_post_transition("Open Settings", SceneDelta(has_changed=True))
    assert res.verdict == "failed" and not res.transition_ok and res.confidence == 0.8
    q = t.requests[0]["body"]["questions"]["outcome"]
    assert q["type"] == "choice" and set(q["criteria"]) == {"confirmed", "unconfirmed", "failed"}


# ------------------------------------------------------------ Tier 1: Needle

@pytest.mark.parametrize("name,args,tool,expected", [
    ("open_app", {"name": "notepad"}, "launch_app", {"app_name": "notepad"}),
    ("open_settings", {"page": "display"}, "launch_app", {"app_name": "ms-settings:display"}),
    ("open_settings", {"page": "ms-settings:bluetooth"}, "launch_app", {"app_name": "ms-settings:bluetooth"}),
    ("open_web", {"address": "github.com"}, "open_url", {"url": "https://github.com"}),
    ("open_web", {"address": "https://example.org/a"}, "open_url", {"url": "https://example.org/a"}),
    ("click", {"control": 17}, "click_element", {"target": "17"}),
    ("right_click", {"control": 17}, "right_click", {"target": "17"}),
    ("double_click", {"control": 4}, "double_click", {"target": "4"}),
    ("type_into", {"text": "hi", "control": 4, "press_enter": True}, "type_text",
     {"text": "hi", "target": "4", "press_enter": True, "clear_first": True}),
    ("type_text", {"text": "hi"}, "type_text", {"text": "hi", "target": "editor"}),
    ("type_text", {"text": "hi", "press_enter": True}, "type_text", {"text": "hi", "target": "editor", "press_enter": True}),
    ("press", {"keys": "Ctrl + S"}, "send_hotkey", {"keys": ["ctrl", "s"]}),
    ("clear", {"control": 4}, "set_value", {"target": "4", "value": ""}),
    ("turn_on", {"control": 7}, "set_toggle", {"target": "7", "state": "on"}),
    ("turn_off", {"control": 7}, "set_toggle", {"target": "7", "state": "off"}),
    ("expand", {"control": 8}, "set_expanded", {"target": "8", "state": "expand"}),
    ("collapse", {"control": 8}, "set_expanded", {"target": "8", "state": "collapse"}),
    ("set_value", {"value": "03", "control": 9}, "set_value", {"target": "9", "value": "03"}),
    ("choose", {"option": "Large", "control": 9}, "select_option", {"target": "9", "option": "Large"}),
    ("scroll_down", {"control": 3}, "scroll", {"direction": "down", "amount": 5, "target": "3"}),
    ("scroll_up", {}, "scroll", {"direction": "up", "amount": 5}),
    ("wait", {"seconds": 2}, "wait", {"duration_seconds": 2.0}),
    ("close_window", {"window": "Notepad"}, "close_window", {"target": "Notepad"}),
    ("close_window", {}, "close_window", {"target": ""}),
    ("switch_to", {"window": "Downloads"}, "focus_window", {"title": "Downloads"}),
    ("search_web", {"query": "weather in Kathmandu"}, "web_search", {"query": "weather in Kathmandu"}),
    ("read_file", {"path": "C:\\notes.txt"}, "read_file", {"path": "C:\\notes.txt"}),
    ("list_folder", {"path": "downloads"}, "list_folder", {"path": "downloads"}),
    ("write_file", {"text": "a", "path": "out.txt"}, "write_file",
     {"path": "out.txt", "text": "a", "append": False}),
    ("append_file", {"text": "x", "path": "log.txt"}, "write_file",
     {"path": "log.txt", "text": "x", "append": True}),
    ("run_command", {"command": "Get-Date"}, "run_command", {"command": "Get-Date", "shell": "powershell"}),
    ("describe_screen", {}, "describe_screen", {}),
    ("read_text", {}, "read_text", {}),
])
def test_to_steps_maps_every_specialist_tool(name, args, tool, expected):
    steps = to_steps(name, args)
    assert len(steps) == 1 and steps[0].tool_name == tool and steps[0].arguments == expected


def test_every_needle_tool_has_a_mapping():
    sample = {"name": "x", "page": "display", "address": "x.com", "control": 1, "text": "t", "keys": "enter",
              "value": "v", "option": "o", "seconds": 1, "window": "w", "query": "q", "path": "p", "command": "c"}
    assert len(NEEDLE_TOOLS) == 29
    assert {t["name"] for t in NEEDLE_TOOLS} == {f.tool for f in STEP_FORMS}   # every tool has a step form
    for tool in NEEDLE_TOOLS:
        assert to_steps(tool["name"], sample), tool["name"]


@pytest.mark.parametrize("name,args", [
    ("teleport", {"to": "mars"}),
    ("open", {"name": "notepad"}),           # the old general tools are gone
    ("files", {"operation": "read", "path": "x"}),
    ("toggle", {"control": 7, "state": "on"}),
    ("open_app", {"name": ""}),
    ("press", {"keys": ""}),
    ("click", {}),
    ("type_into", {"text": "hi"}),
])
def test_unusable_needle_call_is_an_error(name, args):
    with pytest.raises(NeedleError):
        to_steps(name, args)


@pytest.mark.parametrize("text,specialist,tool", [
    ("click [3]", "pointer", "click"),
    ("double click [3]", "pointer", "double_click"),
    ("right-click [3]", "pointer", "right_click"),
    ("scroll up", "pointer", "scroll_up"),
    ("scroll up in [3]", "pointer", "scroll_up"),
    ("scroll down", "pointer", "scroll_down"),
    ("scroll down in [3]", "pointer", "scroll_down"),
    ('type "hi" into [4]', "keyboard", "type_into"),
    ('type "hi"', "keyboard", "type_text"),
    ('type "hi" and press enter', "keyboard", "type_text"),
    ("press ctrl+s", "keyboard", "press"),
    ("clear [4]", "keyboard", "clear"),
    ("turn on [7]", "form", "turn_on"),
    ("turn off [7]", "form", "turn_off"),
    ('set "03" in [9]', "form", "set_value"),
    ('choose "Large" in [9]', "form", "choose"),
    ('select "Large" in [9]', "form", "choose"),
    ("expand [5]", "form", "expand"),
    ("collapse [5]", "form", "collapse"),
    ("open settings display", "window", "open_settings"),
    ("open notepad", "window", "open_app"),
    ('close "draft.txt - Notepad"', "window", "close_window"),
    ('switch to "Downloads - File Explorer"', "window", "switch_to"),
    ("wait 2", "window", "wait"),
    ("go to github.com", "web", "open_web"),
    ("search the web for cats", "web", "search_web"),
    ("read file C:/notes.txt", "files", "read_file"),
    ("list folder downloads", "files", "list_folder"),
    ('write "a" to file out.txt', "files", "write_file"),
    ('append "a" to file out.txt', "files", "append_file"),
    ('run command "Get-Date"', "system", "run_command"),
    ("describe screen", "observe", "describe_screen"),
    ("read text", "observe", "read_text"),
])
def test_step_form_routes_verb_to_tool(text, specialist, tool):
    form = step_form(text)
    assert form is not None and (form.specialist, form.tool) == (specialist, tool)
    assert tool in SPECIALISTS[specialist].tools


@pytest.mark.parametrize("text", ["make it bold", "launch Photos", "read the result", ""])
def test_text_outside_the_step_forms_has_no_form(text):
    assert step_form(text) is None


def test_needle_proposal(needle):
    needle.calls["click [17]"] = {"function_calls": [{"name": "click", "arguments": {"control": 17}}],
                                  "confidence": 0.91, "reasoning": "click [17]"}
    prop = Tier1Intent(tracer=tracer()).propose("click [17]")
    assert prop and prop.source == "needle" and prop.confidence == 0.91
    assert prop.steps[0].tool_name == "click_element" and prop.steps[0].arguments == {"target": "17"}


def test_each_tool_has_its_own_needle_agent(needle):
    needle.calls["turn on [7]"] = [("turn_on", {"control": 7})]
    needle.calls["open settings display"] = [("open_settings", {"page": "display"})]
    t1 = Tier1Intent(tracer=tracer())
    assert t1.propose("turn on [7]", planner_step=True).steps[0].arguments == {"target": "7", "state": "on"}
    assert t1.propose("open settings display", planner_step=True).steps[0].arguments == {
        "app_name": "ms-settings:display"}
    assert needle.agents == ["tool:turn_on", "tool:open_settings"]
    assert needle.tools == {"tool:turn_on": ["turn_on"], "tool:open_settings": ["open_settings"]}
    assert needle.systems["tool:turn_on"] == SPECIALISTS["form"].instructions
    assert needle.systems["tool:open_settings"] == SPECIALISTS["window"].instructions


def test_needle_answers_per_tool_agent(needle):
    needle.calls[("tool:click", "click [3]")] = [("click", {"control": 3})]
    t1 = Tier1Intent(tracer=tracer())
    assert t1.propose("click [3]", planner_step=True).steps[0].tool_name == "click_element"
    assert needle.asked == [("tool:click", "click [3]")]


def test_needle_several_calls_keep_the_one_that_matches(needle):
    tr = tracer()
    needle.calls["clear [4]"] = [("clear", {"control": 4}), ("clear", {"control": 44})]     # measured
    prop = Tier1Intent(tracer=tr).propose("clear [4]", planner_step=True)
    assert prop and prop.steps[0].arguments == {"target": "4", "value": ""}
    kept = [e for e in tr.events if e.title == "Needle made 2 calls · kept the one that matches the step"]
    assert kept and "44" in kept[0].detail


def test_needle_identical_duplicate_calls_count_once(needle):
    needle.calls["open notepad"] = [("open_app", {"name": "notepad"}), ("open_app", {"name": "notepad"})]
    prop = Tier1Intent(tracer=tracer()).propose("open notepad", planner_step=True)
    assert prop and [s.tool_name for s in prop.steps] == ["launch_app"]


def test_needle_several_different_matching_calls_are_rejected(needle):
    needle.calls["open notepad"] = [("open_app", {"name": "notepad"}), ("open_app", {"name": "Notepad"})]
    t1 = Tier1Intent(tracer=tracer())
    assert t1.propose("open notepad", planner_step=True) is None
    assert "2 different calls" in t1.last_error


def test_needle_several_calls_none_matching_are_rejected(needle):
    needle.calls["clear [4]"] = [("clear", {"control": 5}), ("clear", {"control": 44})]
    t1 = Tier1Intent(tracer=tracer())
    assert t1.propose("clear [4]", planner_step=True) is None
    assert "doesn't match the step" in t1.last_error and "[4]" in t1.last_error


def test_needle_rejects_text_that_is_not_a_step_form(needle):
    t1 = Tier1Intent(tracer=tracer())
    assert t1.propose("make the heading look nicer") is None
    assert "not one of the step forms" in t1.last_error
    assert needle.seen == []                     # Needle was never asked


def test_needle_rejects_a_specialist_the_planner_may_not_use(needle):
    needle.calls['run command "Get-Date"'] = [("run_command", {"command": "Get-Date"})]
    t1 = Tier1Intent(tracer=tracer())
    assert t1.propose('run command "Get-Date"', planner_step=True,
                      allowed=planner_agent("settings").specialists) is None
    assert "not available" in t1.last_error and needle.seen == []
    prop = t1.propose('run command "Get-Date"', planner_step=True, allowed=planner_agent("code").specialists)
    assert prop and prop.steps[0].tool_name == "run_command"


def test_needle_call_that_does_not_match_the_step_is_rejected(needle):
    needle.calls["wait 2"] = [("close_window", {})]
    t1 = Tier1Intent(tracer=tracer())
    assert t1.propose("wait 2", planner_step=True) is None
    assert "doesn't match the step" in t1.last_error


def test_needle_absent_returns_none_with_error():
    t1 = Tier1Intent(tracer=tracer())
    assert t1.propose("click [3]") is None
    assert "Needle is unavailable" in t1.last_error


def test_needle_returning_nothing(monkeypatch, needle):
    monkeypatch.setattr(cactus_runtime, "needle_complete", lambda agent, text: None)
    t1 = Tier1Intent(tracer=tracer())
    assert t1.propose("click [3]") is None and "nothing usable" in t1.last_error


def test_needle_without_calls(needle):
    t1 = Tier1Intent(tracer=tracer())
    assert t1.propose("click [3]") is None and "no tool" in t1.last_error


def test_needle_unknown_tool(needle):
    needle.calls["click [3]"] = [("teleport", {"to": "mars"})]
    t1 = Tier1Intent(tracer=tracer())
    assert t1.propose("click [3]") is None and "teleport" in t1.last_error


def test_needle_low_confidence_is_returned_with_error(monkeypatch, needle):
    monkeypatch.setattr(settings, "needle_confidence_threshold", 0.7)
    needle.confidence = 0.4
    needle.calls["click [3]"] = [("click", {"control": 3})]
    t1 = Tier1Intent(tracer=tracer())
    prop = t1.propose("click [3]")
    assert prop is not None and prop.confidence == 0.4      # the caller checks the threshold
    assert "not confident" in t1.last_error
    t1.propose("click [3]", planner_step=True)               # a planner step: only traced
    assert t1.last_error is None


def test_get_needle_caches_one_agent_per_key(monkeypatch):
    import types
    made = []

    class Needle:
        def __init__(self, **kw):
            made.append(kw)

    monkeypatch.setattr(cactus_runtime, "get_needle", _REAL_GET_NEEDLE)   # not the autouse fake
    cactus_runtime.reset_for_tests()
    monkeypatch.setattr(cactus_runtime, "_import_cactus", lambda: types.SimpleNamespace(Needle=Needle))
    try:
        a = cactus_runtime.get_needle([{"name": "click"}], key="tool:click", system="P")
        b = cactus_runtime.get_needle([{"name": "click"}], key="tool:click", system="P")
        c = cactus_runtime.get_needle([{"name": "turn_on"}], key="tool:turn_on", system="F")
        assert a is b and a is not c
        assert [(m["tools"][0]["name"], m["system"]) for m in made] == [("click", "P"), ("turn_on", "F")]
    finally:
        cactus_runtime.reset_for_tests()


# ----------------------------------------------------------------- Mirage

def test_parse_detections_reads_mirage_output():
    # cactus-compute 4.x: objects carry "name", text carries "string", boxes are 0-1000.
    raw = {
        "objects": [{"id": 1, "name": "button", "bbox": [10, 40, 110, 120], "score": 0.9}],
        "text": [{"string": "Cancel", "bbox": [200, 40, 280, 120], "score": 0.4},
                 {"string": "", "bbox": [0, 0, 10, 10]}, {"string": "no box"}],
    }
    dets = parse_detections(raw, 2000, 500)
    by = {d.label: d for d in dets}
    assert set(by) == {"button", "Cancel"}
    assert by["button"].bbox == (20, 20, 220, 60) and by["button"].score == 0.9 and by["button"].kind == "object"
    assert by["Cancel"].bbox == (400, 20, 560, 60) and by["Cancel"].kind == "text"


def test_mirage_unavailable_is_a_failure_not_a_substitute(monkeypatch):
    from PIL import Image
    from servo.perception import mirage_sensor

    monkeypatch.setattr(cactus_runtime, "get_mirage", lambda: None)
    monkeypatch.setattr(cactus_runtime, "mirage_error", lambda: "ModuleNotFoundError: cactus")
    with pytest.raises(mirage_sensor.MirageUnavailable, match="cactus"):
        mirage_sensor.describe_image(Image.new("RGB", (40, 40)))


def test_mirage_describe_errors_are_failures(monkeypatch):
    from PIL import Image
    from servo.perception import mirage_sensor

    class Broken:
        def describe(self, img, **kw):
            raise RuntimeError("engine crashed")

    class Wrong:
        def describe(self, img, **kw):
            return "not json"

    monkeypatch.setattr(cactus_runtime, "get_mirage", lambda: Broken())
    with pytest.raises(mirage_sensor.MirageUnavailable, match="engine crashed"):
        mirage_sensor.describe_image(Image.new("RGB", (40, 40)))
    monkeypatch.setattr(cactus_runtime, "get_mirage", lambda: Wrong())
    with pytest.raises(mirage_sensor.MirageUnavailable, match="expected a JSON object"):
        mirage_sensor.describe_image(Image.new("RGB", (40, 40)))


def test_mirage_adapter_passes_its_settings(monkeypatch):
    from PIL import Image
    from servo.perception import mirage_sensor
    seen = {}

    class Model:
        def describe(self, img, **kw):
            seen.update(kw, mode=img.mode)
            return {"objects": [], "text": [{"string": "OK", "bbox": [0, 0, 500, 500]}]}

    monkeypatch.setattr(cactus_runtime, "get_mirage", lambda: Model())
    raw = mirage_sensor.describe_image(Image.new("L", (40, 40)))
    assert seen["mode"] == "RGB" and seen["sections"] == ["objects", "text"]
    assert [d.bbox for d in parse_detections(raw, 40, 40)] == [(0, 0, 20, 20)]


def test_match_target_penalizes_ties():
    dets = [Detection(label="Save", bbox=(0, 0, 10, 10)), Detection(label="Save As", bbox=(0, 20, 10, 30))]
    best, score, _ = match_target(dets, "save")
    assert best.label == "Save" and score == 1.0
    _, tie, _ = match_target([Detection(label="OK", bbox=(0, 0, 9, 9)), Detection(label="ok!", bbox=(0, 9, 9, 18))], "ok")
    assert tie < 1.0


# ---------------------------------------------------------------- System 2

def test_plan_parsing_and_think_split():
    data = _parse_plan('```json\n{"thought":"t","subtasks":["open notepad", ""],"done":false}\n```')
    assert data["subtasks"] == ["open notepad"]
    content, think = _split_think("<think>hmm</think>{\"a\":1}")
    assert think == "hmm" and content == '{"a":1}'


def test_plan_prompt_describes_numbered_controls():
    for name in PLANNER_AGENTS:
        prompt = plan_system(name)
        assert "click [12]" in prompt and "numbered" in prompt, name


def test_plan_prompt_has_only_the_agents_step_forms():
    settings_prompt, code_prompt = plan_system("settings"), plan_system("code")
    assert 'run command "Get-Date"' not in settings_prompt and 'run command "Get-Date"' in code_prompt
    assert "go to github.com" not in settings_prompt and "go to github.com" in plan_system("web")
    assert "list folder downloads" in plan_system("files") and "list folder downloads" not in plan_system("apps")
    assert "turn on [7]" in settings_prompt and "open settings dateandtime" in settings_prompt
    for name, a in PLANNER_AGENTS.items():
        for form in STEP_FORMS:
            assert ((f"  {form.example}" in plan_system(name)) == (form.specialist in a.specialists)), (name, form.tool)


def test_plan_prompt_names_the_other_agents_for_hand_off():
    prompt = plan_system("settings")
    assert "next_agent" in prompt and "web = web pages" in prompt and "settings = " not in prompt


class TierScript:
    """Scripted local/cloud planners behind System2._complete_on_tier."""

    def __init__(self, local, cloud='{"subtasks": ["open notepad"], "thought": "cloud"}'):
        self.local, self.cloud, self.calls = local, cloud, []

    def __call__(self, tier, system, user, json_mode, max_tokens):
        self.calls.append(tier)
        out = self.local if tier == "local" else self.cloud
        if isinstance(out, Exception):
            raise out
        return System2Result(raw=out, provider=tier)


@pytest.fixture
def tiers(monkeypatch):
    monkeypatch.setattr(system2_mod, "build_plan_prompt", lambda *a, **k: "prompt")

    def make(mode, local, cloud_configured=True):
        monkeypatch.setattr(settings, "planner_mode", mode)
        s2 = System2(provider="anthropic", tracer=tracer())
        monkeypatch.setattr(s2, "_cloud_configured", lambda: cloud_configured)
        script = TierScript(local)
        monkeypatch.setattr(s2, "_complete_on_tier", script)
        return s2, script
    return make


def test_local_first_steps_up_to_cloud_when_local_fails(tiers):
    s2, script = tiers("local_first", System2Unavailable("connection refused"))
    assert s2.tier == "local"
    res = s2.plan("open notepad")
    assert res.subtasks == ["open notepad"] and res.meta["tier"] == "cloud"
    assert script.calls == ["local", "cloud"] and s2.tier == "cloud" and len(s2.step_ups) == 1
    s2.plan("again")                       # the rest of the task stays on the cloud tier
    assert script.calls[-1] == "cloud"
    s2.reset_tier()                        # a new task starts local again
    assert s2.tier == "local" and s2.step_ups == []


def test_local_only_never_steps_up(tiers):
    s2, script = tiers("local_only", System2Unavailable("connection refused"))
    with pytest.raises(System2Unavailable):
        s2.plan("open notepad")
    assert script.calls == ["local"] and s2.tier == "local" and s2.step_up("why") is False


def test_local_first_without_cloud_fails(tiers):
    s2, script = tiers("local_first", System2Unavailable("connection refused"), cloud_configured=False)
    with pytest.raises(System2Unavailable):
        s2.plan("open notepad")
    assert script.calls == ["local"] and s2.tier == "local"


def test_unparsable_local_plan_is_retried_once_then_steps_up(tiers):
    s2, script = tiers("local_first", "I think you should open notepad")
    res = s2.plan("open notepad")          # unusable, asked again on the same tier, then the cloud plans
    assert res.subtasks == ["open notepad"] and s2.tier == "cloud"
    assert script.calls == ["local", "local", "cloud"]


def test_unparsable_plan_retry_can_recover_on_the_same_tier(tiers):
    s2, script = tiers("local_first", "not json")
    answers = iter(["not json", '{"subtasks": ["open notepad"], "next_agent": "Web"}'])

    def call(tier, system, user, json_mode, max_tokens):
        script.calls.append(tier)
        return System2Result(raw=next(answers), provider=tier)
    s2._complete_on_tier = call
    res = s2.plan("open notepad")
    assert res.subtasks == ["open notepad"] and res.next_agent == "web"
    assert script.calls == ["local", "local"] and s2.tier == "local"


def test_unparsable_twice_without_step_up_fails(tiers):
    s2, script = tiers("local_only", "I think you should open notepad")
    with pytest.raises(System2Unavailable, match="unparsable"):
        s2.plan("open notepad")
    assert script.calls == ["local", "local"]


def test_plan_uses_the_agents_system_prompt(tiers):
    s2, script = tiers("local_first", '{"subtasks": []}')
    seen = []

    def call(tier, system, user, json_mode, max_tokens):
        seen.append(system)
        return System2Result(raw='{"subtasks": []}', provider=tier)
    s2._complete_on_tier = call
    s2.plan("change the wallpaper", agent="settings")
    s2.plan("open notepad")
    assert seen == [plan_system("settings"), plan_system(DEFAULT_AGENT)]


def test_cloud_modes_start_on_cloud(tiers):
    s2, _ = tiers("cloud_first", "{}")
    assert s2.tier == "cloud" and s2.step_up("x") is False


def test_unavailable_planner_reports_why(monkeypatch):
    monkeypatch.setattr(settings, "planner_mode", "local_first")
    s2 = System2(provider="none")
    assert not s2.available()
    assert "Local planner" in s2.unavailable_reason() and "no cloud planner" in s2.unavailable_reason()


# ------------------------------------------------------------------ router

class FakeS2(System2):
    def __init__(self, plans: List[Any]) -> None:
        super().__init__(provider="none")
        self.plans = list(plans)
        self.calls: List[Dict[str, Any]] = []

    def available(self) -> bool:
        return True

    def plan(self, goal, screen="", completed=None, failure=None, round_no=1, agent=DEFAULT_AGENT):
        self.calls.append({"goal": goal, "completed": list(completed or []), "failure": failure, "round": round_no,
                           "screen": screen, "agent": agent})
        # Once the scripted plans run out, the model looks at the screen and says "done".
        nxt = self.plans.pop(0) if self.plans else System2Result(done=True, reply="All done.")
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    def chat(self, message, history=None):
        return System2Result(reply="chatty answer")


# What the fake Needle reads each instruction as.
CALLS = {
    "open notepad": [("open_app", {"name": "notepad"})],
    "press enter": [("press", {"keys": "enter"})],
    "click [3]": [("click", {"control": 3})],
    "turn on [7]": [("turn_on", {"control": 7})],
    'run command "Get-Date"': [("run_command", {"command": "Get-Date"})],
    'type "hi"': [("type_text", {"text": "hi"})],
    "press ctrl+s": [("press", {"keys": "ctrl+s"})],
    "press ctrl+b": [("press", {"keys": "ctrl+b"})],
    "click [5]": [("click", {"control": 5})],
    "click [1]": [("click", {"control": 1})],
    "click [2]": [("click", {"control": 2})],
    "click [9]": [("click", {"control": 9})],
    "click [8]": [("click", {"control": 8})],
    "click [6]": [("click", {"control": 6})],
    "click [17]": [("click", {"control": 17})],
    "search the web for current weather in Kathmandu": [("search_web", {"query": "current weather in Kathmandu"})],
    'type "Kathmandu: 18°C, light rain"': [("type_text", {"text": "Kathmandu: 18°C, light rain"})],
    'type "a"': [("type_text", {"text": "a"})],
}


@pytest.fixture
def cascade(laya, needle):
    needle.calls.update(CALLS)
    return laya, needle


def make_router(s2: Optional[System2] = None, outcomes: Optional[List[StepOutcome]] = None, **kw) -> CascadeRouter:
    r = CascadeRouter(tracer=tracer(), system2=s2 or System2(provider="none"), laya=JevGate(), **kw)
    r._screen_summary = lambda kind=None: "screen"  # type: ignore[assignment]
    queue = list(outcomes or [])
    r.executed = []  # type: ignore[attr-defined]
    r.steps = []  # type: ignore[attr-defined]

    def fake_exec(step):
        r.executed.append(step.tool_name)  # type: ignore[attr-defined]
        r.steps.append(step)  # type: ignore[attr-defined]
        return queue.pop(0) if queue else StepOutcome(success=True)
    r._execute = fake_exec  # type: ignore[assignment]
    return r


def test_single_confident_command_runs_in_system1(cascade):
    _, needle = cascade
    s2 = FakeS2([])
    out = make_router(s2).run("open notepad")
    assert out.success and out.route == "system1"
    assert s2.calls == []
    assert [s.tool for s in out.steps] == ["launch_app"] and needle.agents == ["tool:open_app"]


def test_request_that_is_not_a_step_form_goes_to_system2(cascade):
    _, needle = cascade
    s2 = FakeS2([System2Result(done=True, reply="Done.")])
    out = make_router(s2).run("make notepad say hello")
    assert out.route == "system2" and len(s2.calls) == 1 and needle.seen == []


def test_unsure_needle_goes_to_system2(monkeypatch, cascade):
    _, needle = cascade
    monkeypatch.setattr(settings, "needle_confidence_threshold", 0.7)
    needle.confidence = 0.3
    s2 = FakeS2([System2Result(done=True, reply="Already open.")])
    out = make_router(s2).run("open notepad")
    assert out.route == "system2" and out.success and out.reply == "Already open."
    assert len(s2.calls) == 1


def test_missing_needle_goes_to_system2_and_its_steps_fail(laya):
    # No Needle at all: System 2 plans, and its steps cannot be read, so the run fails.
    s2 = FakeS2([System2Result(subtasks=["open notepad"])] * 10)
    r = make_router(s2)
    out = r.run("open notepad")
    assert out.route == "system2" and not out.success and r.executed == []
    assert s2.calls[1]["failure"]["kind"] == "reasoning"
    assert "Needle is unavailable" in s2.calls[1]["failure"]["error"]


def test_multi_step_goes_to_system2_and_back_down_to_needle(cascade):
    laya, needle = cascade
    laya.complexity = "multi"
    s2 = FakeS2([System2Result(subtasks=["open notepad", 'type "hi"', "press ctrl+s"], thought="t")])
    r = make_router(s2)
    out = r.run("open notepad, write a short poem and then save it")
    assert out.route == "system2" and out.success
    assert r.executed == ["launch_app", "type_text", "send_hotkey"]
    assert needle.seen == ["open notepad", 'type "hi"', "press ctrl+s"]   # the goal itself never went to Needle
    assert needle.agents == ["tool:open_app", "tool:type_text", "tool:press"]


def test_laya_domain_picks_the_planner_agent(cascade):
    laya, _ = cascade
    laya.complexity, laya.domain = "multi", "settings"
    s2 = FakeS2([System2Result(subtasks=["turn on [7]"])])
    r = make_router(s2)
    out = r.run("turn on bluetooth and then check it")
    assert out.success and r.executed == ["set_toggle"]
    assert [c["agent"] for c in s2.calls] == ["settings", "settings"]


def test_planner_step_outside_the_agents_specialists_fails(cascade):
    laya, needle = cascade
    laya.complexity, laya.domain = "multi", "settings"
    s2 = FakeS2([System2Result(subtasks=['run command "Get-Date"']), System2Result(done=True, reply="ok")])
    r = make_router(s2)
    r.run("show the date and then stop")
    assert r.executed == [] and needle.seen == []
    assert s2.calls[1]["failure"]["kind"] == "reasoning" and "not available" in s2.calls[1]["failure"]["error"]


def test_next_agent_hands_the_task_over(cascade):
    laya, _ = cascade
    laya.complexity, laya.domain = "multi", "settings"
    s2 = FakeS2([
        System2Result(next_agent="code", thought="needs a command"),       # hand-off, no steps
        System2Result(subtasks=['run command "Get-Date"']),
        System2Result(done=True, reply="It is Tuesday."),
    ])
    r = make_router(s2)
    out = r.run("what does Get-Date say, and then tell me")
    assert out.success and r.executed == ["run_command"]
    assert [c["agent"] for c in s2.calls] == ["settings", "code", "code"] and r.agent == "code"
    assert any(e.title == "Planner agent · settings → code" for e in r.tracer.events)


def test_next_agent_mid_task_replans_with_the_new_agent(cascade):
    laya, _ = cascade
    laya.complexity = "multi"
    s2 = FakeS2([
        System2Result(subtasks=["open notepad"]),
        System2Result(next_agent="web"),
        System2Result(done=True, reply="ok"),
    ])
    r = make_router(s2)
    out = r.run("open notepad and then look something up")
    assert out.success and [c["agent"] for c in s2.calls] == ["apps", "apps", "web"]


def test_unknown_or_same_next_agent_is_ignored(cascade):
    laya, _ = cascade
    laya.complexity = "multi"
    s2 = FakeS2([System2Result(subtasks=["open notepad"], next_agent="apps"),
                 System2Result(done=True, reply="ok", next_agent="martians")])
    r = make_router(s2)
    out = r.run("open notepad and then stop")
    assert out.success and r.agent == "apps" and r.executed == ["launch_app"]


def test_repeated_failed_step_is_not_run_again(cascade):
    laya, _ = cascade
    laya.complexity = "multi"
    s2 = FakeS2([
        System2Result(subtasks=["click [3]"]),
        System2Result(subtasks=["click  [3]"]),                    # the same step, unchanged
        System2Result(subtasks=["press enter"]),
    ])
    r = make_router(s2, outcomes=[StepOutcome(success=False, error="No UI element found matching target '[3]'")])
    out = r.run("press the button and then confirm")
    assert out.success and r.executed == ["click_element", "send_hotkey"]     # the repeat never ran
    failure = s2.calls[2]["failure"]
    assert failure["kind"] == "reasoning" and "again unchanged" in failure["error"]
    assert out.replans == 2


def test_laya_unavailable_fails_the_run(cascade):
    laya, _ = cascade
    laya.unavailable = "connection refused"
    s2 = FakeS2([])
    r = make_router(s2)
    out = r.run("open notepad")
    assert not out.success and out.route == "laya-unavailable" and "connection refused" in out.reply
    assert s2.calls == [] and r.executed == []


def test_without_system2_a_planned_request_fails(cascade):
    laya, _ = cascade
    laya.complexity = "multi"
    out = make_router().run("research the best laptop and then email a summary")
    assert not out.success and out.route == "system2-unavailable"
    assert "System 2" in out.reply


def test_system2_failure_fails_the_run(cascade):
    laya, _ = cascade
    laya.complexity = "multi"
    out = make_router(FakeS2([System2Unavailable("local planner timed out")])).run("plan my day and then do it")
    assert not out.success and out.route == "system2" and "timed out" in out.reply


def test_chat_goes_to_system2_chat(cascade):
    laya, needle = cascade
    laya.desktop = 0.1
    out = make_router(FakeS2([])).run("why is the sky blue?")
    assert out.route == "chat" and out.reply == "chatty answer" and needle.seen == []


def test_agent_loop_observes_and_continues_until_done(cascade):
    """The weather task: plan only what can be planned, look at the result, continue."""
    laya, _ = cascade
    laya.complexity, laya.domain = "multi", "web"
    s2 = FakeS2([
        System2Result(subtasks=["search the web for current weather in Kathmandu"]),
        System2Result(subtasks=["open notepad", 'type "Kathmandu: 18°C, light rain"', "press ctrl+s"]),
        System2Result(done=True, reply="Saved weather.txt with today's summary."),
    ])
    r = make_router(s2)
    out = r.run("Search the web for the current weather in Kathmandu, then open Notepad and write a one-line "
                "summary of it, then save the file as weather.txt on the Desktop")
    assert out.success and out.reply == "Saved weather.txt with today's summary."
    assert r.executed == ["web_search", "launch_app", "type_text", "send_hotkey"]
    assert [c["round"] for c in s2.calls] == [1, 2, 3]
    assert s2.calls[1]["completed"] == ["search the web for current weather in Kathmandu"]   # it saw round 1


def test_agent_loop_stops_on_repeated_batch(cascade):
    laya, _ = cascade
    laya.complexity = "multi"
    same = System2Result(subtasks=["click [6]"])
    out = make_router(FakeS2([same, same, same])).run("keep checking the page and then tell me when it changes")
    assert not out.success and "same steps" in out.reply


def test_round_limit_asks_to_continue(monkeypatch, cascade):
    laya, _ = cascade
    laya.complexity = "multi"
    monkeypatch.setattr(settings, "max_rounds", 1)
    asked = []

    def ask(kind, payload, options, timeout=None):
        asked.append((kind, options))
        return "stop"
    s2 = FakeS2([System2Result(subtasks=["click [1]"]), System2Result(subtasks=["click [2]"])])
    out = make_router(s2, ask=ask).run("work through the list and then finish")
    assert asked == [("continue", ["continue", "stop"])]
    assert not out.success and "Paused" in out.reply


def test_confirmation_skip_continues_with_next_step(cascade):
    """Proceed / Skip / Stop: skipping a risky step moves on instead of failing the run."""
    laya, _ = cascade
    laya.complexity = "multi"

    def ask(kind, payload, options, timeout=None):
        assert kind == "confirm" and options == ["proceed", "skip", "stop"]
        return "skip"
    r = make_router(FakeS2([System2Result(subtasks=["click [9]", "press enter"])]), ask=ask)

    def fake_exec(step):
        r.executed.append(step.tool_name)
        if step.tool_name == "click_element":          # pretend the risky click asked for permission
            assert r._confirm(ConfirmationRequest(sub_goal="Click Delete", tool_call="click(1)", element_label="Delete",
                                                  risk_level="high", reason="kw", hwnd=0)) is False
            return StepOutcome(success=False, kind="policy", error="Action rejected by user confirmation")
        return StepOutcome(success=True)
    r._execute = fake_exec
    out = r.run("click Delete, then press enter")
    assert out.success and r.executed == ["click_element", "send_hotkey"]
    progress = [(e.title, e.status) for e in r.tracer.events if e.kind == "progress" and e.status != "running"]
    assert progress == [("click [9]", "skipped"), ("press enter", "ok")]


def test_confirmation_stop_aborts(cascade):
    laya, _ = cascade
    laya.complexity = "multi"
    r = make_router(FakeS2([System2Result(subtasks=["click [9]", "press enter"])]),
                    ask=lambda kind, payload, options, timeout=None: "stop")

    def fake_exec(step):
        r._confirm(ConfirmationRequest(sub_goal="x", tool_call="click(1)", element_label="Delete",
                                       risk_level="high", reason="kw", hwnd=0))
        return StepOutcome(success=False, kind="policy", error="Action rejected by user confirmation")
    r._execute = fake_exec
    out = r.run("click Delete, then press enter")
    assert out.aborted


def test_step_mode_waits_for_next(cascade):
    laya, _ = cascade
    laya.complexity = "multi"
    choices = iter(["next", "run_all"])
    asked = []

    def ask(kind, payload, options, timeout=None):
        asked.append((kind, payload["index"]))
        return next(choices)
    r = make_router(FakeS2([System2Result(subtasks=["open notepad", 'type "a"', "press enter"])]),
                    ask=ask, step_mode=True)
    out = r.run('open notepad, type "a", then press enter')
    assert out.success and r.executed == ["launch_app", "type_text", "send_hotkey"]
    assert asked == [("step", 1), ("step", 2)]          # "run the rest" stops asking


def test_perception_miss_replans_with_context(cascade):
    laya, _ = cascade
    laya.complexity = "multi"
    s2 = FakeS2([
        System2Result(subtasks=["click [5]"]),
        System2Result(subtasks=["press ctrl+b"]),
    ])
    r = make_router(s2, outcomes=[StepOutcome(success=False, error="No UI element found matching target 'Bold'")])
    out = r.run("make the selected text bold and then check it")
    assert out.success and out.replans == 1
    failure = s2.calls[1]["failure"]
    assert failure["kind"] == "perception" and "Bold" in failure["error"]
    assert r.executed == ["click_element", "send_hotkey"]


def test_unreadable_subtask_is_sent_back_up(cascade):
    laya, _ = cascade
    laya.complexity = "multi"
    s2 = FakeS2([System2Result(subtasks=["make it pretty please"]), System2Result(done=True, reply="ok")])
    out = make_router(s2).run("tidy up this document and then save")
    assert out.success and s2.calls[1]["failure"]["kind"] == "reasoning"
    assert "numbered control" in s2.calls[1]["failure"]["error"]


def test_policy_rejection_does_not_replan(cascade):
    laya, _ = cascade
    laya.complexity = "multi"
    s2 = FakeS2([System2Result(subtasks=["click [9]"])])
    r = make_router(s2, outcomes=[StepOutcome(success=False, kind="policy", error="Action rejected by user confirmation")])
    out = r.run("clean up my files and then empty them")
    assert not out.success and len(s2.calls) == 1


def test_replans_are_bounded(monkeypatch, cascade):
    laya, _ = cascade
    laya.complexity = "multi"
    monkeypatch.setattr(settings, "max_replans", 1)
    s2 = FakeS2([System2Result(subtasks=["click [1]"]), System2Result(subtasks=["click [2]"])])
    fail = StepOutcome(success=False, error="No UI element found matching target")
    out = make_router(s2, outcomes=[fail, fail]).run("find the thing and then click it")
    assert not out.success and out.replans == 1


def test_abort_stops_before_next_step(cascade):
    laya, _ = cascade
    laya.complexity = "multi"
    ev = threading.Event()
    s2 = FakeS2([System2Result(subtasks=["open notepad", "click [8]"])])
    r = make_router(s2, should_abort=ev)
    real = r._execute

    def exec_and_abort(step):
        ev.set()
        return real(step)
    r._execute = exec_and_abort  # type: ignore[assignment]
    out = r.run("open notepad and then save it")
    assert out.aborted and r.executed == ["launch_app"]


# ------------------------------------------------------------ number grounding

def _el(i, role, label, context=None, **kw):
    return SceneElement(element_id=i, role=role, label=label, bbox_screen=(0, i * 10, 50, i * 10 + 9),
                        bbox_norm=(0, 0, 1, 1), center_screen=(25, i * 10 + 4), context=context, **kw)


def test_describe_scene_numbers_controls():
    scene = SceneIndex(hwnd=1, window_title="Settings", source="uia", elements={
        1: _el(1, "Button", "Save", "Dialog"),
        2: _el(2, "Text", "Welcome", interactive=False),
        3: _el(3, "CheckBox", "Wrap"),
    })
    numbered: Dict[int, Dict[str, Any]] = {}
    text = describe_scene(scene, numbered=numbered)
    assert "[1] Button: Save (Dialog)" in text and "[2] CheckBox: Wrap" in text
    assert "Welcome" in text and "] Text: Welcome" not in text       # plain text is not numbered
    assert numbered[1]["role"] == "Button" and numbered[1]["label"] == "Save" and numbered[1]["context"] == "Dialog"
    assert numbered[2]["label"] == "Wrap"


@pytest.mark.parametrize("target", ["17", "[17]", " [17] ", "#17"])
def test_number_is_grounded_to_the_described_control(target):
    r = make_router()
    r._numbered = {17: {"role": "Button", "label": "Save", "context": "Dialog", "bbox": (0, 0, 1, 1)}}
    step = to_steps("click", {"control": target})[0]
    g = r._ground(step)
    assert g.arguments["target"] == "Button: Save (Dialog)" and g.tool_name == "click_element"


def test_unknown_number_or_label_is_left_alone():
    r = make_router()
    r._numbered = {1: {"role": "Button", "label": "OK", "context": None, "bbox": (0, 0, 1, 1)}}
    assert r._ground(to_steps("click", {"control": 99})[0]).arguments["target"] == "99"
    assert r._ground(to_steps("click", {"control": "OK"})[0]).arguments["target"] == "OK"
    assert r._ground(to_steps("click", {"control": 1})[0]).arguments["target"] == "Button: OK"


def test_planned_number_reaches_the_executor_grounded(cascade):
    laya, _ = cascade
    laya.complexity = "multi"
    s2 = FakeS2([System2Result(subtasks=["click [17]"])])
    r = make_router(s2)

    def screen(kind=None):
        r._numbered = {17: {"role": "Hyperlink", "label": "Sign in", "context": None, "bbox": (0, 0, 1, 1)}}
        return "[17] Hyperlink: Sign in"
    r._screen_summary = screen
    out = r.run("sign in to the site and then wait")
    assert out.success and r.steps[0].arguments["target"] == "Hyperlink: Sign in"


# ------------------------------------------------------------------ misc

def test_classify_error():
    assert classify_error("Execution cancelled by user") == "abort"
    assert classify_error("Action rejected by user confirmation") == "policy"
    assert classify_error("Ambiguous target 'OK'") == "reasoning"
    assert classify_error("Typed text was not observed in any control value") == "verify"
    assert classify_error("UIA failed: x; Mirage failed: y") == "perception"


def test_launch_matches_process_not_title():
    from servo.daemon.copilot import _matches_app
    edge_tab = {"hwnd": 1, "title": "notepad tips - Search - Microsoft Edge", "process": "msedge.exe"}
    notepad = {"hwnd": 2, "title": "Untitled - Notepad", "process": "notepad.exe"}
    calc = {"hwnd": 3, "title": "Calculator", "process": "calculatorapp.exe"}
    assert not _matches_app(edge_tab, "notepad", "notepad.exe")
    assert _matches_app(notepad, "notepad", "notepad.exe")
    assert _matches_app(calc, "calculator", "calc.exe")


# ------------------------------------------------------------ describe_screen

def test_describe_screen_is_a_needle_tool():
    assert "describe_screen" in {t["name"] for t in NEEDLE_TOOLS}
    step = to_steps("describe_screen", {})[0]
    assert step.tool_name == "describe_screen" and step.arguments == {}


def _mirage_router(monkeypatch, detections, error=None):
    from servo.cognition import router as router_mod
    from servo.perception import mirage_sensor

    class FakeSensor:
        def __init__(self, hwnd):
            self.last_detections = []

        def sense(self, window_title=""):
            self.last_detections = detections
            return SceneIndex(hwnd=5, window_title=window_title, source="mirage", sense_error=error)

    monkeypatch.setattr(mirage_sensor, "MirageSensor", FakeSensor)
    monkeypatch.setattr(router_mod.windows, "is_window", lambda h: h == 5)
    r = CascadeRouter(tracer=tracer(), system2=System2(provider="none"), laya=JevGate(), memory=None)
    r.hwnd, r.title = 5, "Paint"
    monkeypatch.setattr(r, "_refresh_target", lambda: None)
    return r


def test_describe_screen_returns_mirages_summary(monkeypatch):
    r = _mirage_router(monkeypatch, [Detection(label="Brushes", bbox=(1, 2, 30, 40), kind="text")])
    out = r._execute(to_steps("describe_screen", {})[0])
    assert out.success and out.tier == "mirage"
    assert "Mirage sees in “Paint”" in out.output and "text 'Brushes' at (1, 2, 30, 40)" in out.output


def test_describe_screen_fails_when_mirage_does(monkeypatch):
    r = _mirage_router(monkeypatch, [], error="Cactus Mirage is unavailable: not installed")
    out = r._execute(to_steps("describe_screen", {})[0])
    assert not out.success and out.kind == "perception" and "Mirage is unavailable" in out.error


# --- Fixes from WAA smoke run 4 -------------------------------------------------------

def test_press_keys_keep_punctuation_and_must_match_the_step():
    from servo.cognition.needle import _keys, check_call
    assert _keys("ctrl+,") == ["ctrl", ","]
    assert _keys("ctrl++") == ["ctrl", "+"]
    assert _keys("ctrl + shift + t") == ["ctrl", "shift", "t"]
    # Needle dropped the comma: the check must refuse it, not send a bare ctrl.
    assert check_call("press ctrl+,", "press", {"keys": "ctrl"})
    assert check_call("press ctrl+,", "press", {"keys": "ctrl+,"}) is None


def test_clear_is_an_explicit_empty_value():
    from servo.actuation.alias_mapping import AliasMapper
    a = AliasMapper.canonicalize_action("set_value", target="Edit: File name", value="")
    assert a.text == ""
    with pytest.raises(ValueError):
        AliasMapper.canonicalize_action("set_value", target="Edit: File name")


def test_type_into_replaces_the_field():
    steps = to_steps("type_into", {"text": "draft.txt", "control": 20})
    assert steps[0].arguments["clear_first"] is True
    assert steps[0].canonical_action.clear_first is True
    assert "clear_first" not in to_steps("type_text", {"text": "hi"})[0].arguments


def test_addresses_with_a_scheme_keep_it():
    from servo.cognition.needle import normalize_url
    assert normalize_url("chrome://history") == "chrome://history"
    assert normalize_url("about:blank") == "about:blank"
    assert normalize_url("github.com") == "https://github.com"


def test_clicking_a_list_item_is_not_judged_by_its_toggle_state():
    from servo.control.loop import ServoControlLoop
    el = SceneElement(element_id=1, label="Edit timer, 5 min", role="DataItem", bbox_screen=(0, 0, 10, 10),
                      bbox_norm=(0, 0, 10, 10), center_screen=(5, 5), toggle="off")
    assert ServoControlLoop._stateful_click_verdict(None, None, el) is None  # type: ignore[arg-type]


# --- Fixes from WAA smoke run 5 -------------------------------------------------------

def test_a_closed_window_is_a_change_not_a_sensing_failure():
    """Save / OK / Cancel close their dialog. Reporting "nothing changed" made each such click fail."""
    from servo.control.jev_gate import describe_delta
    el = SceneElement(element_id=1, label="Save", role="Button", bbox_screen=(0, 0, 9, 9), bbox_norm=(0, 0, 9, 9),
                      center_screen=(4, 4))
    before = SceneIndex(hwnd=1, window_title="Save as", source="uia", elements={1: el})
    after = SceneIndex(hwnd=1, window_title="", source="uia", sense_error="window closed")
    d = SceneDelta.compute_diff(before, after)
    assert d.has_changed and d.window_closed and d.removed_labels == ["Save"]
    assert describe_delta(d) == "The window 'Save as' closed."
    # a real sensing failure is still not a change
    broken = SceneIndex(hwnd=1, window_title="Save as", source="uia", sense_error="COM error")
    assert not SceneDelta.compute_diff(before, broken).has_changed


def test_step_forms_match_the_whole_step():
    from servo.cognition.specialists import form_hint
    assert step_form('type "C:/x/draft.txt" into the File name box') is None   # it typed into the document
    assert "type \"hello\" into [4]" in form_hint('type "C:/x/draft.txt" into the File name box')
    assert step_form("click [1] Timer") is None
    assert step_form('type "a" into [3] and press enter').tool == "type_into"
    assert step_form('run command "Get-Date \\"2024-01-03\\""').tool == "run_command"


def test_punctuation_keys_by_name():
    from servo.actuation.sandboxing import normalize_keys
    from servo.cognition.needle import check_call
    assert normalize_keys(["ctrl", "comma"]) == ["ctrl", ","]
    assert check_call("press ctrl+comma", "press", {"keys": "ctrl+comma"}) is None
