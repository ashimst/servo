"""The WindowsAgentArena adapter: Servo runs inside the VM, the WAA agent starts and polls it."""

import base64
import json
import sys
import types

# waa_agent runs in WAA's client container, which has `requests`; Servo's own env may not.
sys.modules.setdefault("requests", types.SimpleNamespace(post=None))

from servo.integrations.winarena import waa_agent, vm_runner  # noqa: E402


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


def test_agent_starts_servo_in_the_vm_then_reports_done(monkeypatch):
    calls = []
    polls = iter([None, {"state": "running"}, {"state": "done", "success": True, "reply": "Done"}])

    def fake_post(url, json=None, timeout=None):
        calls.append((url, json))
        if url.endswith("/execute_windows"):
            return FakeResponse({"status": "success"})
        state = next(polls)
        return FakeResponse({"output": "" if state is None else __import__("json").dumps(state)})

    monkeypatch.setattr(waa_agent.requests, "post", fake_post)
    monkeypatch.setattr(waa_agent.time, "sleep", lambda s: None)
    agent = waa_agent.ServoAgent(vm_ip="1.2.3.4")
    agent.reset()
    reply, actions, logs, update = agent.predict("Open Notepad and type “hi” — then save", {})
    assert actions == ["DONE"] and reply == "Done" and update is None
    url, body = calls[0]
    assert url == "http://1.2.3.4:5000/execute_windows"
    goal_b64 = body["command"].split("'--goal-b64', '")[1].split("'")[0]
    assert base64.b64decode(goal_b64).decode("utf-8") == "Open Notepad and type “hi” — then save"
    # The next step of the same episode just ends it.
    assert agent.predict("x", {})[1] == ["DONE"]


def test_agent_reports_fail_when_servo_fails_or_times_out(monkeypatch):
    monkeypatch.setattr(waa_agent.time, "sleep", lambda s: None)
    monkeypatch.setattr(waa_agent.requests, "post", lambda url, json=None, timeout=None: FakeResponse(
        {"status": "success"} if url.endswith("_windows") else
        {"output": __import__("json").dumps({"state": "done", "success": False, "reply": "stuck"})}))
    agent = waa_agent.ServoAgent()
    assert agent.predict("t", {})[1] == ["FAIL"]

    clock = iter(range(0, 10_000, 100))
    monkeypatch.setattr(waa_agent.time, "time", lambda: next(clock))
    monkeypatch.setattr(waa_agent.requests, "post", lambda url, json=None, timeout=None: FakeResponse(
        {"status": "success"} if url.endswith("_windows") else {"output": '{"state": "running"}'}))
    slow = waa_agent.ServoAgent(task_timeout=300)
    reply, actions, _, _ = slow.predict("t", {})
    assert actions == ["FAIL"] and "did not finish" in reply


def test_vm_runner_always_leaves_a_final_status(tmp_path, monkeypatch):
    from servo.cognition import router as router_mod

    class Out:
        success, reply, route, run_id, steps = True, "Done — 1 step completed.", "system1", "r1", []

    monkeypatch.setattr(router_mod.CascadeRouter, "run", lambda self, goal: Out())
    status = tmp_path / "s.json"
    goal = base64.b64encode("open notepad".encode()).decode()
    assert vm_runner.main(["--goal-b64", goal, "--status", str(status)]) == 0
    raw = status.read_bytes()
    assert raw.isascii()  # WAA's server decodes `type` output as cp1252
    data = json.loads(raw)
    assert data["state"] == "done" and data["success"] is True and data["goal"] == "open notepad"

    monkeypatch.setattr(router_mod.CascadeRouter, "run", lambda self, goal: 1 / 0)
    assert vm_runner.main(["--goal-b64", goal, "--status", str(status)]) == 1
    assert json.loads(status.read_text(encoding="utf-8"))["state"] == "error"
