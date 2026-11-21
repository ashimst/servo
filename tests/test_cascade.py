"""Cognitive cascade: trace bus, Kev contract, Tier 1, Mirage parsing, and router routing."""

from __future__ import annotations

import json
import threading
from typing import Any, Dict, List, Optional

import httpx
import pytest

from servo.cognition import cactus_runtime
from servo.cognition.needle import Tier1Intent, parse_grammar
from servo.cognition.router import CascadeRouter, StepOutcome, classify_error
from servo.cognition.system2 import System2, System2Result, _parse_plan, _split_think
from servo.cognition.trace import TraceBus, Tracer
from servo.config import settings
from servo.control.confirmation import ConfirmationRequest
from servo.control.jev_gate import JevGate
from servo.perception.mirage_sensor import Detection, match_target, parse_detections


@pytest.fixture(autouse=True)
def _no_cactus(monkeypatch):
    """Hermetic: never load real Cactus weights in unit tests."""
    monkeypatch.setattr(cactus_runtime, "get_needle", lambda tools: None)
    monkeypatch.setattr(cactus_runtime, "get_mirage", lambda: None)
    monkeypatch.setattr(settings, "kev_url", "")


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


# ------------------------------------------------------------- Kev contract

class _Transport:
    def __init__(self, answers: Dict[str, Any]) -> None:
        self.answers = answers
        self.requests: List[Dict[str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append({"url": str(request.url), "body": json.loads(request.content), "headers": dict(request.headers)})
        return httpx.Response(200, json={"model": "kev-latest", "answers": self.answers})


@pytest.fixture
def kev_server(monkeypatch):
    def make(answers: Dict[str, Any]) -> _Transport:
        t = _Transport(answers)
        real = httpx.Client
        monkeypatch.setattr(httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(t.handler), **kw))
        return t
    return make


def test_kev_request_uses_system_one_contract(kev_server):
    t = kev_server({
        "risk_level": {"type": "score", "score": 0.2, "confidence": 0.8, "legend": {"0": "low", "1": "medium", "2": "high"},
                       "probabilities": {"0": 0.85, "1": 0.1, "2": 0.05}},
        "scope_valid": {"type": "noul", "noul": 0.97},
    })
    gate = JevGate(endpoint_url="http://127.0.0.1:8009", use_fallback=False)
    res = gate.evaluate_pre_risk("Click the 7 button", "click(element_id=3)", "7")
    req = t.requests[0]
    assert req["url"] == "http://127.0.0.1:8009/v1/systemone"
    body = req["body"]
    assert set(body) == {"state", "model", "questions"} and body["model"] == "kev-latest"
    assert body["questions"]["risk_level"]["type"] == "score"
    assert body["questions"]["risk_level"]["criteria"] == ["low", "medium", "high"]
    assert body["questions"]["scope_valid"]["type"] == "noul"
    assert res.source == "kev" and res.risk_level == "low" and res.scope_valid


def test_kev_cannot_lower_keyword_risk(kev_server):
    kev_server({
        "risk_level": {"type": "score", "probabilities": {"0": 0.9, "1": 0.08, "2": 0.02}},
        "scope_valid": {"type": "noul", "noul": 0.9},
    })
    res = JevGate(endpoint_url="http://kev", use_fallback=False).evaluate_pre_risk("Delete the file", "click(1)", "Delete")
    assert res.risk_level == "high" and "keyword floor" in res.reason


def test_kev_high_mass_upgrades_to_high(kev_server):
    kev_server({
        "risk_level": {"type": "score", "probabilities": {"0": 0.45, "1": 0.25, "2": 0.30}},
        "scope_valid": {"type": "noul", "noul": 0.9},
    })
    res = JevGate(endpoint_url="http://kev", use_fallback=False).evaluate_pre_risk("Click OK", "click(1)", "OK")
    assert res.risk_level == "high"


def test_kev_malformed_answer_falls_back_to_heuristic(kev_server):
    kev_server({"risk_level": {"type": "score"}})
    res = JevGate(endpoint_url="http://kev", use_fallback=False).evaluate_pre_risk("Click OK", "click(1)", "OK")
    assert res.source == "heuristic"


def test_kev_routing_question(kev_server):
    kev_server({
        "desktop_task": {"type": "noul", "noul": 0.95},
        "complexity": {"type": "choice", "choice": "multi", "confidence": 0.6,
                       "probabilities": {"single": 0.2, "multi": 0.7, "ambiguous": 0.1}},
    })
    a = JevGate(endpoint_url="http://kev", use_fallback=False).assess_goal("find the cheapest flight and book it")
    assert a.source == "kev" and a.desktop_task and a.complexity == "multi"


@pytest.mark.parametrize("goal,complexity", [
    ("click Save", "single"),
    ("open notepad and write hello", "single"),
    ("open chrome, find the cheapest flight to Pokhara and then book it", "multi"),
])
def test_heuristic_complexity(goal, complexity):
    parsed = parse_grammar(goal) is not None
    assert JevGate(use_fallback=True).assess_goal(goal, tier1_parsed=parsed).complexity == complexity


# ----------------------------------------------------------------- Tier 1

@pytest.mark.parametrize("text,tool,arg", [
    ("go to github.com", "open_url", ("url", "https://github.com")),
    ("search the web for weather in Kathmandu", "web_search", ("query", "weather in Kathmandu")),
    ("open chrome and search for cats", "web_search", ("query", "cats")),
    ('type "a into b" into Search', "type_text", ("text", "a into b")),
    ("double click Readme", "double_click", ("target", "Readme")),
    ("press ctrl+s", "send_hotkey", ("keys", ["ctrl", "s"])),
])
def test_grammar(text, tool, arg):
    plan = parse_grammar(text)
    assert plan is not None and plan.steps[0].tool_name == tool
    assert plan.steps[0].arguments[arg[0]] == arg[1]


def test_grammar_rejects_swallowed_sentences():
    assert parse_grammar("open notepad and then figure out what to write and save it somewhere") is None


def test_needle_used_when_grammar_misses(monkeypatch):
    class Agent:
        def complete(self, text, max_new_tokens=256):
            return {"type": "call", "function_calls": [{"name": "click_element", "arguments": {"target": "Bold"}}],
                    "reasoning": "'make it bold' -> click Bold", "confidence": 0.91}
    monkeypatch.setattr(cactus_runtime, "get_needle", lambda tools: Agent())
    prop = Tier1Intent(tracer=tracer()).propose("make it bold")
    assert prop and prop.source == "needle" and prop.confidence == 0.91
    assert prop.steps[0].arguments == {"target": "Bold"}


def test_needle_absent_is_a_reasoning_miss():
    assert Tier1Intent(tracer=tracer()).propose("make the heading look nicer") is None


# ----------------------------------------------------------------- Mirage

def test_parse_detections_shapes():
    raw = {
        "objects": [{"label": "Submit", "bbox": [10, 20, 110, 60], "score": 0.9}],
        "text": [{"text": "Cancel", "box": {"x": 200, "y": 20, "w": 80, "h": 40}},
                 {"text": "tiny", "bbox": [0.5, 0.5, 0.6, 0.6]}],
        "junk": [{"no": "label"}],
    }
    dets = parse_detections(raw, 1000, 500)
    by = {d.label: d for d in dets}
    assert by["Submit"].bbox == (10, 20, 110, 60) and by["Submit"].score == 0.9
    assert by["Cancel"].bbox == (200, 20, 280, 60) and by["Cancel"].kind == "text"
    assert by["tiny"].bbox == (500, 250, 600, 300)  # normalized → pixels


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


# ------------------------------------------------------------------ router

class FakeS2(System2):
    def __init__(self, plans: List[System2Result]) -> None:
        super().__init__(provider="none")
        self.plans = list(plans)
        self.calls: List[Dict[str, Any]] = []

    def available(self) -> bool:
        return True

    def plan(self, goal, screen="", completed=None, failure=None, round_no=1):
        self.calls.append({"goal": goal, "completed": list(completed or []), "failure": failure, "round": round_no})
        # Once the scripted plans run out, the model looks at the screen and says "done".
        return self.plans.pop(0) if self.plans else System2Result(done=True, reply="All done.")

    def chat(self, message, history=None):
        return System2Result(reply="chatty answer")


def make_router(s2: Optional[System2] = None, outcomes: Optional[List[StepOutcome]] = None, **kw) -> CascadeRouter:
    r = CascadeRouter(tracer=tracer(), system2=s2 or System2(provider="none"), kev=JevGate(use_fallback=True), **kw)
    r._screen_summary = lambda kind=None: "screen"  # type: ignore[assignment]
    queue = list(outcomes or [])
    r.executed = []  # type: ignore[attr-defined]

    def fake_exec(step):
        r.executed.append(step.tool_name)  # type: ignore[attr-defined]
        return queue.pop(0) if queue else StepOutcome(success=True)
    r._execute = fake_exec  # type: ignore[assignment]
    return r


def test_simple_command_stays_in_system1():
    s2 = FakeS2([])
    out = make_router(s2).run("open notepad and write hello")
    assert out.success and out.route == "system1"
    assert s2.calls == []
    assert out.steps[0].tool == "launch_app" and out.steps[1].tool == "type_text"


def test_compound_of_direct_commands_runs_without_llm():
    s2 = FakeS2([])
    r = make_router(s2)
    out = r.run("open notepad, write hi and then save as notes.txt on the desktop")
    assert out.route == "system1" and out.success and s2.calls == []
    assert r.executed == ["launch_app", "type_text", "send_hotkey", "wait", "type_text"]


def test_multi_step_goes_to_system2_and_back_down_to_tier1():
    s2 = FakeS2([System2Result(subtasks=["open notepad", 'type "hi"', "press ctrl+s"], thought="t")])
    r = make_router(s2)
    out = r.run("open notepad, write a short poem and then save it")
    assert out.route == "system2" and out.success
    assert r.executed == ["launch_app", "type_text", "send_hotkey"]


def test_agent_loop_observes_and_continues_until_done():
    """The weather task: plan only what can be planned, look at the result, continue."""
    s2 = FakeS2([
        System2Result(subtasks=["search the web for current weather in Kathmandu"]),
        System2Result(subtasks=["open notepad", 'type "Kathmandu: 18°C, light rain"', "save as weather.txt on the desktop"]),
        System2Result(done=True, reply="Saved weather.txt with today's summary."),
    ])
    r = make_router(s2)
    out = r.run("Search the web for the current weather in Kathmandu, then open Notepad and write a one-line "
                "summary of it, then save the file as weather.txt on the Desktop")
    assert out.success and out.reply == "Saved weather.txt with today's summary."
    assert r.executed == ["web_search", "launch_app", "type_text", "send_hotkey", "wait", "type_text"]
    assert [c["round"] for c in s2.calls] == [1, 2, 3]
    assert s2.calls[1]["completed"] == ["search the web for current weather in Kathmandu"]   # it saw round 1


def test_agent_loop_stops_on_repeated_batch():
    same = System2Result(subtasks=["click Refresh"])
    s2 = FakeS2([same, same, same])
    out = make_router(s2).run("keep checking the page and then tell me when it changes")
    assert not out.success and "repeat" in out.reply


def test_round_limit_asks_to_continue(monkeypatch):
    monkeypatch.setattr(settings, "max_rounds", 1)
    asked = []

    def ask(kind, payload, options, timeout=None):
        asked.append((kind, options))
        return "stop"
    s2 = FakeS2([System2Result(subtasks=["click A"]), System2Result(subtasks=["click B"])])
    out = make_router(s2, ask=ask).run("work through the list and then finish")
    assert asked == [("continue", ["continue", "stop"])]
    assert not out.success and "Paused" in out.reply


def test_confirmation_skip_continues_with_next_step():
    """Proceed / Skip / Stop: skipping a risky step moves on instead of failing the run."""
    def ask(kind, payload, options, timeout=None):
        assert kind == "confirm" and options == ["proceed", "skip", "stop"]
        return "skip"
    r = make_router(FakeS2([]), ask=ask)

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
    assert progress == [("click_element(target='Delete')", "skipped"), ("send_hotkey(keys=['enter'])", "ok")]


def test_confirmation_stop_aborts():
    r = make_router(FakeS2([]), ask=lambda kind, payload, options, timeout=None: "stop")

    def fake_exec(step):
        r._confirm(ConfirmationRequest(sub_goal="x", tool_call="click(1)", element_label="Delete",
                                       risk_level="high", reason="kw", hwnd=0))
        return StepOutcome(success=False, kind="policy", error="Action rejected by user confirmation")
    r._execute = fake_exec
    out = r.run("click Delete, then press enter")
    assert out.aborted


def test_step_mode_waits_for_next():
    choices = iter(["next", "run_all"])
    asked = []

    def ask(kind, payload, options, timeout=None):
        asked.append((kind, payload["index"]))
        return next(choices)
    r = make_router(FakeS2([]), ask=ask, step_mode=True)
    out = r.run("open notepad, type \"a\", then press enter")
    assert out.success and r.executed == ["launch_app", "type_text", "send_hotkey"]
    assert asked == [("step", 1), ("step", 2)]          # "run the rest" stops asking


def test_perception_miss_replans_with_context():
    s2 = FakeS2([
        System2Result(subtasks=["click Bold"]),
        System2Result(subtasks=["press ctrl+b"]),
    ])
    r = make_router(s2, outcomes=[StepOutcome(success=False, error="No UI element found matching target 'Bold'")])
    out = r.run("make the selected text bold and then check it")
    assert out.success and out.replans == 1
    failure = s2.calls[1]["failure"]
    assert failure["kind"] == "perception" and "Bold" in failure["error"]
    assert r.executed == ["click_element", "send_hotkey"]


def test_unparseable_subtask_is_sent_back_up():
    s2 = FakeS2([System2Result(subtasks=["make it pretty please"]), System2Result(done=True, reply="ok")])
    out = make_router(s2).run("tidy up this document and then save")
    assert out.success and s2.calls[1]["failure"]["kind"] == "reasoning"


def test_policy_rejection_does_not_replan():
    s2 = FakeS2([System2Result(subtasks=["click Delete"])])
    r = make_router(s2, outcomes=[StepOutcome(success=False, kind="policy", error="Action rejected by user confirmation")])
    out = r.run("clean up my files and then empty them")
    assert not out.success and len(s2.calls) == 1


def test_replans_are_bounded(monkeypatch):
    monkeypatch.setattr(settings, "max_replans", 1)
    s2 = FakeS2([System2Result(subtasks=["click A"]), System2Result(subtasks=["click B"])])
    fail = StepOutcome(success=False, error="No UI element found matching target")
    out = make_router(s2, outcomes=[fail, fail]).run("find the thing and then click it")
    assert not out.success and out.replans == 1


def test_abort_stops_before_next_step():
    ev = threading.Event()
    s2 = FakeS2([System2Result(subtasks=["open notepad", "click Save"])])
    r = make_router(s2, should_abort=ev)
    real = r._execute

    def exec_and_abort(step):
        ev.set()
        return real(step)
    r._execute = exec_and_abort  # type: ignore[assignment]
    out = r.run("open notepad and then save it")
    assert out.aborted and r.executed == ["launch_app"]


def test_without_system2_complex_request_explains():
    out = make_router().run("research the best laptop and then email a summary")
    assert not out.success and "SERVO_PLANNER_PROVIDER" in out.reply


def test_chat_goes_to_system2_chat():
    out = make_router(FakeS2([])).run("why is the sky blue?")
    assert out.route == "chat" and out.reply == "chatty answer"


def test_classify_error():
    assert classify_error("Execution cancelled by user") == "abort"
    assert classify_error("Action rejected by user confirmation") == "policy"
    assert classify_error("Ambiguous target 'OK'") == "reasoning"
    assert classify_error("Typed text was not observed in any control value") == "verify"
    assert classify_error("UIA failed: x; Mirage failed: y") == "perception"


@pytest.mark.parametrize("text,clauses", [
    ("Search the web for the weather in Kathmandu, then open Notepad and write a summary of it, then save the file as w.txt",
     ["Search the web for the weather in Kathmandu", "open Notepad", "write a summary of it", "save the file as w.txt"]),
    ("search the web for ashim shrestha and store the links in notepad",
     ["search the web for ashim shrestha", "store the links in notepad"]),
    ('type "rock and roll, then jazz" then press enter', ['type "rock and roll, then jazz"', "press enter"]),
    ("type hello and world", ["type hello and world"]),
])
def test_split_clauses(text, clauses):
    from servo.cognition.needle import split_clauses
    assert split_clauses(text) == clauses


@pytest.mark.parametrize("text,literal", [
    ("write hello world", True), ("write my name", True), ('type "a summary of it"', True),
    ("write a one-line summary of it", False), ("write the results into notepad", False),
    ("write a poem about rain", False),
])
def test_composed_text_needs_reasoning(text, literal):
    assert (parse_grammar(text) is not None) == literal


def test_swallowed_second_action_is_rejected():
    assert parse_grammar("search the web for ashim shrestha and store the links in notepad") is None


def test_launch_matches_process_not_title():
    from servo.daemon.copilot import _matches_app
    edge_tab = {"hwnd": 1, "title": "notepad tips - Search - Microsoft Edge", "process": "msedge.exe"}
    notepad = {"hwnd": 2, "title": "Untitled - Notepad", "process": "notepad.exe"}
    calc = {"hwnd": 3, "title": "Calculator", "process": "calculatorapp.exe"}
    assert not _matches_app(edge_tab, "notepad", "notepad.exe")
    assert _matches_app(notepad, "notepad", "notepad.exe")
    assert _matches_app(calc, "calculator", "calc.exe")
