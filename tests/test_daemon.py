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
