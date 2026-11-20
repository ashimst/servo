"""Shared task entry points used by the REST API, MCP tools, and voice ingestion."""

from __future__ import annotations

import time
import uuid
from typing import Any, Dict, Optional

from servo.config import settings
from servo.daemon.worker import TaskJob, worker
from servo.perception import windows
from servo.telemetry.models import RunMetrics, StepTrace, TracePayload
from servo.telemetry.store import TraceStore

_trace_store: Optional[TraceStore] = None


def get_trace_store() -> TraceStore:
    global _trace_store
    if _trace_store is None:
        _trace_store = TraceStore()
    return _trace_store


class TaskRequestError(ValueError):
    """Invalid task request (bad HWND, mode mismatch, ...)."""


def validate_target_hwnd(hwnd: Optional[int]) -> int:
    """Actions require an explicit, existing HWND.

    The old behaviour defaulted to the foreground window. When Servo is driven over
    MCP or REST, the foreground window is usually the caller's own terminal or IDE,
    so "no hwnd" meant "automate the agent's own console". Callers can discover
    HWNDs with list_windows.
    """
    if not hwnd:
        raise TaskRequestError("An explicit 'hwnd' is required for actions. Use list_windows to find one.")
    if not windows.is_window(hwnd):
        raise TaskRequestError(f"HWND {hwnd} is not a valid window.")
    return hwnd


def resolve_mode(requested: Optional[str]) -> str:
    """The execution mode is fixed by server configuration (SERVO_MODE).

    A request may restate it but cannot change it: letting a client pick 'harness'
    would let any caller turn off confirmation prompts. Previously the field was
    accepted and silently ignored, which was misleading in the other direction.
    """
    if requested and requested != settings.mode:
        raise TaskRequestError(
            f"Requested mode '{requested}' does not match the server mode '{settings.mode}' (set via SERVO_MODE)."
        )
    return settings.mode


def run_goal(
    goal: str,
    hwnd: Optional[int],
    mode: Optional[str] = None,
    fast_mode: bool = False,
    planner: Optional[str] = None,
) -> Dict[str, Any]:
    """Plan and execute a goal on the worker thread, record a trace, and return the result."""
    if not goal or not goal.strip():
        raise TaskRequestError("Goal must not be empty.")
    target_hwnd = validate_target_hwnd(hwnd)
    run_mode = resolve_mode(mode)

    t0 = time.perf_counter()
    job = TaskJob(
        job_id=f"task_{uuid.uuid4().hex[:12]}",
        action_type="run_plan",
        hwnd=target_hwnd,
        params={"goal": goal, "mode": run_mode, "fast_mode": fast_mode, "planner": planner},
    )
    res = worker.submit_job(job, timeout=settings.task_timeout_seconds)
    elapsed_ms = int((time.perf_counter() - t0) * 1000)

    data = res.data or {}
    steps = data.get("steps", [])
    status = data.get("status") or ("passed" if res.success else "failed")

    # Trace reflects what actually happened (previously every run was recorded as a
    # single "uia" step with jev_pre_risk="low", whatever the real path was).
    payload = TracePayload(
        mode=run_mode,
        status=status,
        goal=goal,
        metrics=RunMetrics(
            total_latency_ms=elapsed_ms,
            uia_steps=sum(1 for s in steps if s["step_log"]["perception_source"] == "uia"),
            mirage_fallbacks=sum(1 for s in steps if s["step_log"]["perception_source"] == "mirage"),
            system2_escalations=sum(1 for s in steps if s["escalation_required"]),
        ),
        steps=[
            StepTrace(
                step=i + 1,
                sub_goal=s["step_log"]["sub_goal"],
                perception_source=s["step_log"]["perception_source"],
                tool_call=s["step_log"]["tool_call"],
                jev_pre_risk=s["step_log"]["jev_pre_risk"],
                jev_post_ok=s["step_log"]["jev_post_ok"],
                latency_ms=s["step_log"]["latency_ms"],
                confirmation=s["step_log"].get("confirmation"),
            )
            for i, s in enumerate(steps)
        ],
        failure_artifact=data.get("failure_artifact"),
    )
    get_trace_store().record_run(payload)

    return {
        "success": res.success,
        "status": status,
        "run_id": payload.run_id,
        "latency_ms": elapsed_ms,
        "plan": data.get("plan", []),
        "steps": steps,
        "error": res.error,
    }


def sense_scene(hwnd: Optional[int]) -> Dict[str, Any]:
    """Read the SceneIndex of a window. Read-only, so the foreground default is kept."""
    target = hwnd or windows.get_foreground_window()
    if not target:
        raise TaskRequestError("No HWND specified and no foreground window found.")
    job = TaskJob(job_id=f"sense_{uuid.uuid4().hex[:12]}", action_type="sense_scene", hwnd=target)
    result = worker.submit_job(job, timeout=10.0)
    if not result.success:
        raise RuntimeError(result.error or "Failed to sense scene.")
    return result.data or {}
