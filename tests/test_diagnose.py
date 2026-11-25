"""Self-diagnosis: requests, error-to-code tracing, failure patterns, router route."""

import pytest

from servo.cognition import diagnose as dg


@pytest.mark.parametrize("text,expected", [
    ("why did that fail?", ""), ("what went wrong", ""), ("Why didn't it work?", ""),
    ("diagnose the last run", ""), ("diagnose run abc123", "abc123"),
    ("why is the sky blue", None), ("open notepad", None),
])
def test_requests(text, expected):
    assert dg.parse_diagnose_request(text) == expected


@pytest.fixture(scope="module")
def graph(tmp_path_factory):
    from servo.cognition.graph_memory import GraphMemory
    return GraphMemory(graph_path=tmp_path_factory.mktemp("g") / "g.json").graph


@pytest.mark.parametrize("error,function", [
    ("Could not bring HWND 66886 to the foreground; refusing to send keystrokes to another window.",
     "ensure_target_in_foreground"),
    ("Needle (pointer) found no tool for this step", "propose"),
    ("the step is not one of the step forms", "propose"),
    ("No UI element found matching target 'Notepad'. Available elements sample: Text editor", "_resolve_core"),
    ("Typed text is not in the target control. It now reads: “hello”", "evaluate_post_transition"),
])
def test_error_traces_to_the_function_that_raised_it(graph, error, function):
    locs = dg.locate_error(error, graph)
    assert locs and locs[0].function == function, [l.function for l in locs]
    assert locs[0].line > 0 and locs[0].file.endswith(".py")


def _record(error, success=False):
    return {"run_id": "r1", "goal": "open notepad and write a poem", "success": success, "reply": "I got stuck",
            "steps": [{"command": "open notepad", "call": "launch_app(app_name='notepad')", "tier": "uia",
                       "success": True, "verification": "confirmed"},
                      {"command": "type", "call": "type_text(text='x', target='Notepad')", "tier": "uia",
                       "success": False, "error": error}],
            "rounds": [{"subtasks": ["open notepad", 'type "x" into Notepad']}],
            "events": [{"kind": "decision", "tier": "laya", "status": "warn", "title": "Laya pre-check · medium risk",
                        "detail": "Writes a file"}]}


def test_diagnosis_names_step_cause_and_code(graph):
    d = dg.diagnose(record=_record("No UI element found matching target 'Notepad'."), graph=graph)
    assert d.status == "failed" and d.failing_step["call"].startswith("type_text")
    assert "accessibility tree" in d.cause
    assert d.code[0].function == "_resolve_core"
    text = d.to_text()
    assert "Failing step" in text and "alias_mapping.py:" in text and "The planner tried" in text
    assert d.gate and "medium risk" in d.gate[0]


def test_unconfirmed_runs_are_diagnosed_too(graph):
    rec = _record("", success=True)
    rec["steps"][1].update(success=True, verification="unconfirmed", note="no visible change", error=None)
    d = dg.diagnose(record=rec, graph=graph)
    assert d.status == "unconfirmed" and "effect wasn't visible" in d.cause


def test_nothing_to_diagnose(monkeypatch, tmp_path):
    from servo.config import settings
    monkeypatch.setattr(settings, "trace_dir", tmp_path)
    d = dg.diagnose()
    assert not d.found and "nothing to diagnose" in d.to_text()


def test_router_answers_why_without_planning(monkeypatch, tmp_path):
    from servo.config import settings
    from servo.cognition.router import CascadeRouter
    from servo.control.jev_gate import JevGate
    from servo.telemetry.run_record import save_run
    monkeypatch.setattr(settings, "trace_dir", tmp_path)
    save_run({**_record("No UI element found matching target 'Notepad'."), "ts": 0, "time": "t", "route": "system2",
              "aborted": False, "latency_ms": 1, "replans": 0, "verdicts": {"failed": 1}})

    class T:
        run_id = "x"
        def emit(self, *a, **k): return type("E", (), {"seq": 0})()
        def span(self, *a, **k):
            import contextlib
            @contextlib.contextmanager
            def cm():
                yield type("S", (), {"done": lambda *a, **k: None})()
            return cm()
        events = []

    r = CascadeRouter(tracer=T(), laya=JevGate(), memory=None)
    monkeypatch.setattr(r.system2, "available", lambda: False)
    out = r.run("why did that fail?")
    assert out.route == "diagnose" and "_resolve_core" in out.reply
