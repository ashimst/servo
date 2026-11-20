"""FastAPI REST/WebSocket and FastMCP unified daemon server.

See ADR-12 and Section 6 in SERVO_ARCHITECTURE_V1.md.
Exposes MCP tools (run_task, assert_ui_state, get_scene_index) and REST endpoints.
"""

from __future__ import annotations

import contextlib
import sys
import time
import uuid
from typing import Any, AsyncIterator, Dict, Optional
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from servo.config import settings, get_hardware_profile
from servo.daemon.worker import worker, TaskJob
from servo.telemetry.store import TraceStore
from servo.telemetry.models import TracePayload, StepTrace, RunMetrics


trace_store = TraceStore()


@contextlib.asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    """Modern lifespan handler managing STA COM worker thread lifecycle."""
    worker.start()
    yield
    worker.stop()


app = FastAPI(
    title="Servo Desktop Control Runtime",
    version="0.1.0",
    description="Local-First, Closed-Loop Neuro-Symbolic Desktop Control Runtime",
    lifespan=lifespan,
)


class TaskRequest(BaseModel):
    goal: str
    hwnd: Optional[int] = None
    mode: str = "assistant"
    fast_mode: bool = False


@app.get("/health")
def health() -> Dict[str, Any]:
    """Health check endpoint and hardware profile."""
    return {
        "status": "healthy",
        "service": "servo",
        "version": "0.1.0",
        "hardware": get_hardware_profile(),
        "config": {
            "mode": settings.mode,
            "device": settings.device,
            "fast_mode": settings.fast_mode,
        },
    }


@app.get("/scene")
def get_scene(hwnd: Optional[int] = None) -> Dict[str, Any]:
    """Query current SceneIndex for the specified or foreground HWND."""
    target_hwnd = hwnd
    if target_hwnd is None and sys.platform == "win32":
        import ctypes
        target_hwnd = ctypes.windll.user32.GetForegroundWindow()

    if not target_hwnd:
        raise HTTPException(status_code=400, detail="No active window found or HWND specified.")

    job = TaskJob(
        job_id=f"sense_{uuid.uuid4().hex[:8]}",
        action_type="sense_scene",
        hwnd=target_hwnd,
    )
    result = worker.submit_job(job, timeout=5.0)
    if not result.success:
        raise HTTPException(status_code=500, detail=result.error or "Failed to sense scene.")
    return result.data or {}


@app.post("/task")
def run_task(req: TaskRequest) -> Dict[str, Any]:
    """Execute a task goal through Servo closed-loop reflex arc."""
    target_hwnd = req.hwnd
    if target_hwnd is None and sys.platform == "win32":
        import ctypes
        target_hwnd = ctypes.windll.user32.GetForegroundWindow()

    if not target_hwnd:
        raise HTTPException(status_code=400, detail="No active target window found.")

    t0 = time.perf_counter()
    job = TaskJob(
        job_id=f"task_{uuid.uuid4().hex[:8]}",
        action_type="execute_subgoal",
        hwnd=target_hwnd,
        params={
            "step_index": 1,
            "sub_goal": req.goal,
            "target_label_hint": req.goal,
            "action_type": "click",
            "fast_mode": req.fast_mode,
        },
    )
    res = worker.submit_job(job, timeout=15.0)
    elapsed_ms = int((time.perf_counter() - t0) * 1000)

    # Persist trace
    payload = TracePayload(
        mode=req.mode,
        status="passed" if res.success else "failed",
        goal=req.goal,
        metrics=RunMetrics(total_latency_ms=elapsed_ms, uia_steps=1),
        steps=[
            StepTrace(
                step=1,
                sub_goal=req.goal,
                perception_source="uia",
                tool_call=f"action('{req.goal}')",
                jev_pre_risk="low",
                jev_post_ok=res.success,
                latency_ms=elapsed_ms,
            )
        ],
    )
    trace_store.record_run(payload)

    return {
        "success": res.success,
        "run_id": payload.run_id,
        "latency_ms": elapsed_ms,
        "data": res.data,
        "error": res.error,
    }


@app.get("/traces")
def get_traces(limit: int = 50) -> Dict[str, Any]:
    """Retrieve recent automation traces from SQLite."""
    return {"runs": trace_store.list_runs(limit=limit)}
