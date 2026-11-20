"""Shared task entry points used by the REST API, MCP tools, and voice ingestion."""

from __future__ import annotations

import time
import uuid
from typing import Any, Dict, Optional, Union

from servo.config import settings
from servo.control.handoff import PlanExecutionSummary
from servo.daemon.worker import TaskJob, worker
from servo.perception import windows
from servo.planner.tool_calling import MacroPlan
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


def record_plan_trace(
    summary: PlanExecutionSummary,
    mode: Optional[str] = None,
) -> TracePayload:
    """Record a completed or aborted PlanExecutionSummary to TraceStore (SQLite + JSONL)."""
    run_mode = mode or settings.mode
    status = "passed" if summary.success else ("cancelled" if summary.aborted else "failed")

    uia_count = sum(1 for r in summary.step_reports if (r.perception_source or "uia") == "uia")
    mirage_count = sum(1 for r in summary.step_reports if r.perception_source == "mirage")

    step_traces = [
        StepTrace(
            step=r.step_index,
            sub_goal=r.description or f"Step {r.step_index}",
            perception_source=r.perception_source or "uia",
            tool_call=f"{r.action_type}({r.target or ''})",
            jev_pre_risk=r.jev_risk or "low",
            jev_post_ok=r.success,
            latency_ms=r.latency_ms,
            confirmation=r.confirmation,
        )
        for r in summary.step_reports
    ]

    failure_artifact = None
    if not summary.success and summary.step_reports:
        last_step = summary.step_reports[-1]
        if last_step.error:
            failure_artifact = {"error": last_step.error, "step_index": last_step.step_index}

    payload = TracePayload(
        mode=run_mode,
        status=status,
        goal=summary.goal,
        metrics=RunMetrics(
            total_latency_ms=summary.total_latency_ms,
            uia_steps=uia_count,
            mirage_fallbacks=mirage_count,
            system2_escalations=0,
        ),
        steps=step_traces,
        failure_artifact=failure_artifact,
    )
    get_trace_store().record_run(payload)
    return payload


def _build_step_trace(i: int, s: Dict[str, Any]) -> StepTrace:
    if "step_log" in s and isinstance(s["step_log"], dict):
        log = s["step_log"]
        return StepTrace(
            step=i + 1,
            sub_goal=str(log.get("sub_goal") or s.get("description") or f"Step {i + 1}"),
            perception_source=str(log.get("perception_source") or "uia"),
            tool_call=str(log.get("tool_call") or s.get("action_type") or "action"),
            jev_pre_risk=str(log.get("jev_pre_risk") or s.get("jev_risk") or "low"),
            jev_post_ok=bool(log.get("jev_post_ok", s.get("success", True))),
            latency_ms=int(log.get("latency_ms") or s.get("latency_ms") or 0),
            confirmation=log.get("confirmation") or s.get("confirmation"),
        )
    return StepTrace(
        step=int(s.get("step_index", i + 1)),
        sub_goal=str(s.get("description") or s.get("sub_goal") or f"Step {i + 1}"),
        perception_source=str(s.get("perception_source") or "uia"),
        tool_call=str(s.get("action_type") or "action"),
        jev_pre_risk=str(s.get("jev_risk") or "low"),
        jev_post_ok=bool(s.get("success", True)),
        latency_ms=int(s.get("latency_ms") or 0),
        confirmation=s.get("confirmation"),
    )


def run_goal(
    goal: str,
    hwnd: Optional[int] = None,
    mode: Optional[str] = None,
    fast_mode: bool = False,
    planner: Optional[str] = None,
    plan: Optional[Union[MacroPlan, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Plan and execute a goal on the worker thread, record a trace, and return the result."""
    if not goal or not goal.strip():
        raise TaskRequestError("Goal must not be empty.")
    if plan is not None:
        target_hwnd = validate_target_hwnd(hwnd) if hwnd is not None else None
    else:
        target_hwnd = validate_target_hwnd(hwnd)
    run_mode = resolve_mode(mode)

    t0 = time.perf_counter()
    params: Dict[str, Any] = {
        "goal": goal,
        "mode": run_mode,
        "fast_mode": fast_mode,
        "planner": planner,
    }
    if plan is not None:
        params["plan"] = plan.model_dump() if hasattr(plan, "model_dump") else plan

    job = TaskJob(
        job_id=f"task_{uuid.uuid4().hex[:12]}",
        action_type="run_plan",
        hwnd=target_hwnd,
        params=params,
    )
    res = worker.submit_job(job, timeout=settings.task_timeout_seconds)
    elapsed_ms = int((time.perf_counter() - t0) * 1000)

    data = res.data or {}
    steps = data.get("steps", [])
    status = data.get("status") or ("passed" if res.success else "failed")

    def _get_perception_source(s: Dict[str, Any]) -> str:
        if "step_log" in s and isinstance(s["step_log"], dict):
            return str(s["step_log"].get("perception_source") or "uia")
        return str(s.get("perception_source") or "uia")

    payload = TracePayload(
        mode=run_mode,
        status=status,
        goal=goal,
        metrics=RunMetrics(
            total_latency_ms=elapsed_ms,
            uia_steps=sum(1 for s in steps if _get_perception_source(s) == "uia"),
            mirage_fallbacks=sum(1 for s in steps if _get_perception_source(s) == "mirage"),
            system2_escalations=sum(1 for s in steps if s.get("escalation_required", False)),
        ),
        steps=[_build_step_trace(i, s) for i, s in enumerate(steps)],
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
