"""Unit tests for the Servo Daemon and STA Worker Thread."""

from fastapi.testclient import TestClient
from servo.daemon.server import app
from servo.daemon.worker import AutomationWorker, TaskJob


def test_daemon_health_endpoint():
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert data["service"] == "servo"
    assert "hardware" in data


def test_daemon_traces_endpoint():
    client = TestClient(app)
    response = client.get("/traces")
    assert response.status_code == 200
    data = response.json()
    assert "runs" in data


def test_worker_lifecycle():
    worker = AutomationWorker()
    worker.start()
    assert worker._running is True

    # Submit mock job
    job = TaskJob(
        job_id="test_01",
        action_type="unknown_action",
        hwnd=10,
    )
    result = worker.submit_job(job, timeout=1.0)
    assert result.success is False
    assert "Unknown action" in (result.error or "")

    worker.stop()
    assert worker._running is False
