"""Queued and scheduled goals."""

import json
from datetime import datetime

import pytest

from servo.daemon.scheduler import MISSED_GRACE_S, Scheduler, parse_schedule

NOW = datetime(2026, 11, 21, 10, 0, 0)


@pytest.mark.parametrize("text,kind,goal,delta_s", [
    ("in 10 minutes open notepad", "once", "open notepad", 600),
    ("in 2 hours, check my email", "once", "check my email", 7200),
    ("every 30 minutes run command \"Get-Date\"", "interval", 'run command "Get-Date"', 1800),
    ("every hour list folder downloads", "interval", "list folder downloads", 3600),
    ("at 15:30 open calculator", "once", "open calculator", 5.5 * 3600),
    ("at 9am open notepad", "once", "open notepad", 23 * 3600),       # 9am already passed today
    ("tomorrow at 8:15 open notepad", "once", "open notepad", 22.25 * 3600),
    ("every day at 9:00 read file desktop\\todo.txt", "daily", "read file desktop\\todo.txt", 23 * 3600),
])
def test_parse(text, kind, goal, delta_s):
    req = parse_schedule(text, now=NOW)
    assert req.kind == kind and req.goal == goal
    assert req.next_run - NOW.timestamp() == pytest.approx(delta_s)


@pytest.mark.parametrize("text", ["open notepad", "at 3 open notepad", "click every item", "type \"in 5 minutes\""])
def test_not_a_schedule(text):
    assert parse_schedule(text, now=NOW) is None


def test_interval_has_a_floor():
    assert parse_schedule("every 5 seconds open notepad", now=NOW).interval_s == 60


class Clock:
    def __init__(self, t): self.t = t
    def __call__(self): return self.t


def make(tmp_path, busy=lambda: False, t=None):
    started = []
    clock = Clock(t or NOW.timestamp())

    def start(goal):
        if busy():
            return False
        started.append(goal)
        return True
    s = Scheduler(start, path=tmp_path / "schedule.json", clock=clock)
    return s, started, clock


def test_once_job_fires_once_and_persists(tmp_path):
    s, started, clock = make(tmp_path)
    s.handle_command("in 10 minutes open notepad")
    assert json.loads((tmp_path / "schedule.json").read_text())["jobs"][0]["goal"] == "open notepad"
    assert s.tick() is None
    clock.t += 601
    assert s.tick().goal == "open notepad" and started == ["open notepad"]
    assert s.jobs() == [] and s.tick() is None


def test_interval_job_repeats(tmp_path):
    s, started, clock = make(tmp_path)
    s.handle_command("every 30 minutes list folder downloads")
    for _ in range(3):
        clock.t += 1800
        s.tick()
    assert started == ["list folder downloads"] * 3 and len(s.jobs()) == 1


def test_busy_defers_without_losing_job(tmp_path):
    state = {"busy": True}
    s, started, clock = make(tmp_path, busy=lambda: state["busy"])
    s.enqueue("open notepad")
    assert s.tick() is None and len(s.jobs()) == 1
    state["busy"] = False
    assert s.tick().goal == "open notepad" and s.jobs() == []


def test_queue_runs_in_order(tmp_path):
    s, started, clock = make(tmp_path)
    s.enqueue("first"); clock.t += 1
    s.enqueue("second")
    s.tick(); s.tick()
    assert started == ["first", "second"]


def test_queue_is_not_persisted(tmp_path):
    s, _, _ = make(tmp_path)
    s.enqueue("x")
    assert json.loads((tmp_path / "schedule.json").read_text())["jobs"] == []


def test_missed_jobs_are_not_run_late(tmp_path):
    s, _, clock = make(tmp_path)
    s.handle_command("in 1 minutes delete everything")
    s.handle_command("every 1 hours list folder downloads")
    later = clock.t + 3 * 86400
    s2, started, _ = make(tmp_path, t=later)
    goals = [j.goal for j in s2.jobs()]
    assert goals == ["list folder downloads"]              # one-shot dropped as missed
    assert s2.jobs()[0].next_run > later                    # recurring skipped ahead
    assert s2.tick() is None and started == []


def test_list_and_cancel(tmp_path):
    s, _, _ = make(tmp_path)
    reply = s.handle_command("in 10 minutes open notepad")
    job_id = s.jobs()[0].id
    assert job_id in reply
    assert "open notepad" in s.handle_command("list scheduled tasks")
    assert "Cancelled" in s.handle_command(f"cancel {job_id}")
    assert s.handle_command("show schedule") == "Nothing is scheduled or queued."


def test_ordinary_request_passes_through(tmp_path):
    s, _, _ = make(tmp_path)
    assert s.handle_command("open notepad") is None


# ---------------------------------------------------------- session wiring

@pytest.fixture
def session(tmp_path, monkeypatch):
    from servo.config import settings
    monkeypatch.setattr(settings, "schedule_file", tmp_path / "schedule.json")
    from servo.daemon.copilot_app import CopilotSession
    s = CopilotSession()
    s.sent = []
    monkeypatch.setattr(s, "send", lambda text: s.sent.append(text) or "run-1")
    return s


def test_submit_runs_now_when_idle(session):
    assert session.submit("open notepad") == {"run_id": "run-1"}


def test_submit_queues_when_busy(session):
    session.busy = True
    res = session.submit("open notepad")
    assert res["queued"] and res["run_id"] is None and session.sent == []
    session.busy = False
    session.scheduler.tick()
    assert session.sent == ["open notepad"]


def test_submit_schedules_without_running(session):
    res = session.submit("in 5 minutes open notepad")
    assert res["run_id"] is None and "Scheduled" in res["notice"] and session.sent == []


def test_after_this_prefix_is_stripped(session):
    session.submit("after this, open notepad")
    assert session.sent == ["open notepad"]


def test_schedule_api(session):
    from fastapi.testclient import TestClient
    from servo.daemon.copilot_app import create_app
    c = TestClient(create_app(session), base_url="http://127.0.0.1")
    h = {"X-Servo-Token": session.token}
    c.post("/api/send", json={"text": "in 5 minutes open notepad"}, headers=h)
    jobs = c.get("/api/schedule", headers=h).json()["jobs"]
    assert len(jobs) == 1
    assert c.post("/api/schedule/cancel", json={"id": jobs[0]["id"]}, headers=h).json()["cancelled"] == 1
    assert c.get("/api/schedule", headers=h).json()["jobs"] == []
