"""The MCP front end: same engine as the copilot, forwarded to it when it runs."""

from __future__ import annotations

from typing import Any, Dict

import pytest

from servo.cognition.router import CascadeRouter, RunOutcome, StepOutcome
from servo.cognition.system2 import System2
from servo.cognition.trace import Tracer
from servo.control.jev_gate import JevGate
from servo.daemon import discovery, mcp_server
from servo.perception import windows


def _router(**kw) -> CascadeRouter:
    r = CascadeRouter(tracer=Tracer("t"), system2=System2(provider="none"), laya=JevGate(), **kw)
    r.steps = []  # type: ignore[attr-defined]

    def fake_exec(step):
        r.steps.append(step)  # type: ignore[attr-defined]
        return StepOutcome(success=True)
    r._execute = fake_exec  # type: ignore[assignment]
    return r


def test_run_step_uses_the_numbers_from_the_callers_screen(laya, needle, monkeypatch, tmp_path):
    monkeypatch.setattr("servo.telemetry.run_record.save_run", lambda rec: None)
    needle.calls['type "hi" into [3]'] = [("type_into", {"text": "hi", "control": 3})]
    r = _router()
    out = r.run_step('type "hi" into [3]', {3: {"role": "Edit", "label": "Search", "context": None}})
    assert out.success and out.route == "mcp-step"
    step = r.steps[0]  # type: ignore[attr-defined]
    assert step.tool_name == "type_text" and step.arguments["target"] == "Edit: Search"


def test_run_step_allows_every_specialist(laya, needle, monkeypatch):
    """The caller is its own planner: file and command steps aren't limited to one area agent."""
    monkeypatch.setattr("servo.telemetry.run_record.save_run", lambda rec: None)
    needle.calls["list folder downloads"] = [("list_folder", {"path": "downloads"})]
    out = _router().run_step("list folder downloads")
    assert out.success


def test_run_step_rejects_text_that_is_not_a_step_form(laya, needle, monkeypatch):
    monkeypatch.setattr("servo.telemetry.run_record.save_run", lambda rec: None)
    out = _router().run_step("please tidy my desktop")
    assert not out.success and "step forms" in out.reply


def test_standalone_refuses_actions_that_need_confirmation(monkeypatch):
    """No copilot open: nobody can approve a risky step, so it is refused, and the reply says why."""
    monkeypatch.setattr(mcp_server, "_copilot", lambda: None)
    seen: Dict[str, Any] = {}

    class FakeRouter:
        def __init__(self, **kw):
            seen.update(kw)
            self.hwnd = None

        def run(self, goal):
            req = type("Req", (), {"tool_call": "run_command('del x')", "risk_level": "high"})()
            seen["approved"] = kw_confirm(req)
            return RunOutcome(run_id="r", goal=goal, success=False, reply="declined", route="system2")

    def kw_confirm(req):
        return seen["confirmation_callback"](req)
    monkeypatch.setattr("servo.cognition.router.CascadeRouter", FakeRouter)
    res = mcp_server.run_goal("delete x")
    assert seen["approved"] is False
    assert res["note"] == mcp_server.STANDALONE_NOTE


def test_calls_go_to_the_copilot_when_it_runs(monkeypatch):
    monkeypatch.setattr(mcp_server, "_copilot", lambda: {"port": 1, "token": "t"})
    sent = []
    monkeypatch.setattr(mcp_server, "_forward", lambda info, path, body: sent.append((path, body)) or {"success": True})
    assert mcp_server.run_step("click [2]") == {"success": True}
    assert mcp_server.run_goal("open notepad", window="Untitled - Notepad") == {"success": True}
    assert sent == [("step", {"text": "click [2]", "window": None}),
                    ("do", {"text": "open notepad", "window": "Untitled - Notepad"})]


def test_discovery_file_round_trip(monkeypatch, tmp_path):
    monkeypatch.setattr(discovery, "servo_home", lambda: tmp_path)
    assert discovery.read() is None
    discovery.publish(8765, "tok")
    assert discovery.read()["port"] == 8765
    discovery.withdraw()
    assert discovery.read() is None


def test_find_window_by_title(monkeypatch):
    monkeypatch.setattr(windows, "list_windows", lambda: [
        {"hwnd": 1, "title": "Untitled - Notepad", "process": "notepad.exe"},
        {"hwnd": 2, "title": "Downloads - File Explorer", "process": "explorer.exe"},
        {"hwnd": 3, "title": "Documents - File Explorer", "process": "explorer.exe"},
    ])
    assert windows.find_by_title("untitled - notepad") == 1
    assert windows.find_by_title("Downloads") == 2
    with pytest.raises(ValueError, match="Several"):
        windows.find_by_title("File Explorer")
    with pytest.raises(ValueError, match="No window"):
        windows.find_by_title("Paint")


def test_copilot_runs_mcp_calls_like_typed_requests(monkeypatch):
    """The copilot runs an MCP step through its own router (live trace, its confirmations)
    and hands the outcome back to the waiting caller."""
    from servo.daemon.copilot_app import CopilotSession

    class FakeRouter:
        def __init__(self, **kw):
            self.kw = kw
            self.hwnd = None
            self.context = "desktop"

        def run_step(self, text, numbered):
            assert self.kw["confirmation_callback"] is not None   # risky steps ask the user
            return RunOutcome(run_id="x", goal=text, success=True, reply="Done.", route="mcp-step")

        def run(self, goal):
            raise AssertionError("a step must not be planned")

    session = CopilotSession(router_factory=FakeRouter)
    session.mcp_numbered = {2: {"role": "Button", "label": "Save"}}
    res = session.run_for_mcp("step", "click [2]", None, timeout=10)
    assert res["success"] and res["route"] == "mcp-step"


def test_copilot_reports_busy_instead_of_running_over_the_user(monkeypatch):
    from servo.daemon.copilot_app import CopilotSession
    session = CopilotSession(router_factory=lambda **kw: None)
    session.busy = True
    res = session.run_for_mcp("goal", "open notepad", None, timeout=0.5)
    assert not res["success"] and "busy" in res["reply"]
