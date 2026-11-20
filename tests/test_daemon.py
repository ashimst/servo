"""Unit tests for the Servo Daemon and STA Worker Thread."""

import time

import pytest
from fastapi.testclient import TestClient

from servo.daemon import auth, tasks
from servo.daemon.server import app
from servo.daemon.worker import AutomationWorker, TaskJob

TOKEN = "test-token"


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(auth, "_token", TOKEN)
    from servo.config import settings
    monkeypatch.setattr(settings, "trace_dir", tmp_path)
    monkeypatch.setattr(tasks, "_trace_store", None)
    return TestClient(app)


AUTH = {"Authorization": f"Bearer {TOKEN}"}


def test_daemon_health_endpoint_is_public_and_minimal(client):
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert data["service"] == "servo"
    assert "hardware" not in data


def test_endpoints_require_token(client):
    assert client.get("/traces").status_code == 401
    assert client.get("/traces", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.post("/task", json={"goal": "x", "hwnd": 1}).status_code == 401


def test_daemon_traces_endpoint(client):
    response = client.get("/traces", headers=AUTH)
    assert response.status_code == 200
    assert "runs" in response.json()


def test_status_endpoint_has_hardware(client):
    data = client.get("/status", headers=AUTH).json()
    assert "hardware" in data


def test_task_requires_explicit_hwnd(client):
    response = client.post("/task", json={"goal": "click Save"}, headers=AUTH)
    assert response.status_code == 400
    assert "hwnd" in response.json()["detail"]


def test_task_rejects_mode_override(client, monkeypatch):
    from servo.perception import windows
    monkeypatch.setattr(windows, "is_window", lambda h: True)
    response = client.post("/task", json={"goal": "click Save", "hwnd": 1234, "mode": "harness"}, headers=AUTH)
    assert response.status_code == 400
    assert "mode" in response.json()["detail"]


def test_worker_lifecycle():
    worker = AutomationWorker()
    worker.start()
    assert worker._running is True

    result = worker.submit_job(TaskJob(job_id="test_01", action_type="unknown_action", hwnd=10), timeout=1.0)
    assert result.success is False
    assert "Unknown action" in (result.error or "")

    worker.stop()
    assert worker._running is False


def test_timed_out_queued_job_is_cancelled(monkeypatch):
    worker = AutomationWorker()
    ran = []

    def slow_dispatch(job):
        ran.append(job.job_id)
        time.sleep(0.3)
        from servo.daemon.worker import JobResult
        return JobResult(job_id=job.job_id, success=True)

    monkeypatch.setattr(worker, "_dispatch_job", slow_dispatch)
    worker.start()
    import threading
    t = threading.Thread(target=lambda: worker.submit_job(TaskJob(job_id="first", action_type="x", hwnd=1), timeout=2))
    t.start()
    time.sleep(0.05)
    # Second job waits behind the first, times out while still queued -> must never run.
    res = worker.submit_job(TaskJob(job_id="second", action_type="x", hwnd=1), timeout=0.05)
    assert res.success is False
    t.join()
    time.sleep(0.1)
    worker.stop()
    assert ran == ["first"]


# =============================================================================
# Feature 17 & Worker / Telemetry Integration Tests
# =============================================================================

def test_worker_cancel_job():
    worker = AutomationWorker()
    job_id = "test_cancel_123"
    assert not worker.is_cancelled(job_id)

    worker.cancel_job(job_id)
    assert worker.is_cancelled(job_id)


def test_record_plan_trace(tmp_path, monkeypatch):
    import gc
    from servo.config import settings
    from servo.control.handoff import PlanExecutionSummary, StepExecutionReport

    monkeypatch.setattr(settings, "trace_dir", tmp_path)
    monkeypatch.setattr(tasks, "_trace_store", None)

    summary = PlanExecutionSummary(
        goal="write test document",
        success=True,
        total_steps=2,
        completed_steps=2,
        total_latency_ms=250,
        step_reports=[
            StepExecutionReport(
                step_index=1,
                action_type="launch_app",
                target="notepad",
                description="Launched notepad",
                success=True,
                latency_ms=100,
                perception_source="uia",
                jev_risk="low",
            ),
            StepExecutionReport(
                step_index=2,
                action_type="type_text",
                target="editor",
                description="Typed hello",
                success=True,
                latency_ms=150,
                perception_source="uia",
                jev_risk="low",
            ),
        ],
    )

    payload = tasks.record_plan_trace(summary, mode="assistant")
    assert payload.goal == "write test document"
    assert payload.status == "passed"
    assert payload.metrics.total_latency_ms == 250
    assert payload.metrics.uia_steps == 2
    assert len(payload.steps) == 2

    store = tasks.get_trace_store()
    runs = store.list_runs()
    assert len(runs) == 1
    assert runs[0]["goal"] == "write test document"
    assert runs[0]["status"] == "passed"
    gc.collect()


def test_tasks_run_goal_dual_format_step_reports(tmp_path, monkeypatch):
    import gc
    from unittest.mock import patch
    from servo.config import settings
    from servo.daemon.worker import JobResult
    from servo.perception import windows

    monkeypatch.setattr(settings, "trace_dir", tmp_path)
    monkeypatch.setattr(tasks, "_trace_store", None)
    monkeypatch.setattr(windows, "is_window", lambda h: True)

    fake_result = JobResult(
        job_id="test_job_dual",
        success=True,
        data={
            "status": "passed",
            "plan": [{"step_index": 1, "instruction": "click 7"}],
            "steps": [
                {
                    # Dual format: flat StepExecutionReport fields without nested step_log
                    "step_index": 1,
                    "action_type": "click_element",
                    "description": "click 7",
                    "perception_source": "uia",
                    "jev_risk": "low",
                    "latency_ms": 42,
                    "success": True,
                    "confirmation": None,
                }
            ],
        },
    )

    with patch("servo.daemon.tasks.worker.submit_job", return_value=fake_result):
        res = tasks.run_goal("click 7", hwnd=9999)

    assert res["success"] is True
    assert res["status"] == "passed"

    store = tasks.get_trace_store()
    runs = store.list_runs()
    assert len(runs) == 1
    assert runs[0]["goal"] == "click 7"
    assert runs[0]["status"] == "passed"
    gc.collect()


def test_tasks_run_goal_with_precomputed_macro_plan(tmp_path, monkeypatch):
    import gc
    from unittest.mock import patch
    from servo.config import settings
    from servo.daemon.worker import JobResult
    from servo.perception import windows
    from servo.planner.tool_calling import MacroPlan, PlanStep

    monkeypatch.setattr(settings, "trace_dir", tmp_path)
    monkeypatch.setattr(tasks, "_trace_store", None)
    monkeypatch.setattr(windows, "is_window", lambda h: True)

    precomputed = MacroPlan(
        goal="custom precomputed plan",
        steps=[PlanStep(step_index=1, tool_name="click_element", arguments={"target": "OK"})],
    )

    fake_result = JobResult(
        job_id="test_job_plan",
        success=True,
        data={
            "status": "passed",
            "plan": [s.model_dump() for s in precomputed.steps],
            "steps": [
                {
                    "step_log": {
                        "sub_goal": "click OK",
                        "perception_source": "uia",
                        "tool_call": "click_element(OK)",
                        "jev_pre_risk": "low",
                        "jev_post_ok": True,
                        "latency_ms": 30,
                    }
                }
            ],
        },
    )

    with patch("servo.daemon.tasks.worker.submit_job", return_value=fake_result) as mock_submit:
        res = tasks.run_goal("custom precomputed plan", hwnd=1234, plan=precomputed)
        assert mock_submit.called
        submitted_job = mock_submit.call_args[0][0]
        assert "plan" in submitted_job.params
        assert submitted_job.params["plan"]["goal"] == "custom precomputed plan"

    assert res["success"] is True
    assert res["status"] == "passed"
    gc.collect()


def test_worker_run_plan_mid_plan_cancellation(monkeypatch):
    from unittest.mock import MagicMock, patch
    from servo.daemon.worker import TaskJob, run_plan
    from servo.planner.base import SubGoal
    from servo.perception.scene_index import SceneIndex

    mock_scene = SceneIndex(hwnd=10, window_title="App", source="uia", elements={})
    subgoals = [
        SubGoal(step_index=1, instruction="step 1", action_type="click", target_element_hint="btn1"),
        SubGoal(step_index=2, instruction="step 2", action_type="click", target_element_hint="btn2"),
    ]

    mock_planner = MagicMock()
    mock_planner.decompose.return_value = subgoals

    cancel_flag = {"cancelled": False}

    def fake_execute_subgoal(**kwargs):
        # Cancel right after step 1 executes
        cancel_flag["cancelled"] = True
        mock_res = MagicMock(
            success=True,
            attempts_used=1,
            actuated=True,
            escalation_required=False,
            step_log=MagicMock(model_dump=lambda: {"sub_goal": "step 1", "latency_ms": 10}),
        )
        return mock_res

    with patch("servo.control.loop.ServoControlLoop.execute_subgoal", side_effect=fake_execute_subgoal), \
         patch("servo.perception.uia_sensor.UIASensor.sense", return_value=mock_scene), \
         patch("servo.perception.windows.is_window", return_value=True), \
         patch("servo.daemon.worker.get_planner", return_value=mock_planner):

        job = TaskJob(job_id="test_mid_cancel", action_type="run_plan", hwnd=10, params={"goal": "test goal"})
        data = run_plan(job, is_cancelled=lambda: cancel_flag["cancelled"])

    assert data["status"] == "cancelled"
    assert len(data["steps"]) == 1

