"""Dedicated STA COM Automation Worker Thread.

See ADR-05 in SERVO_ARCHITECTURE_V1.md and SERVO_HARDENING_NOTES.md.
Isolates all synchronous Windows UI Automation COM calls and Win32 input syntheses
away from the FastAPI / FastMCP asyncio event loop.
"""

from __future__ import annotations

import logging
import queue
import sys
import threading
from typing import Any, Callable, Dict, List, Optional, Set

from pydantic import BaseModel, Field

from servo.actuation.sandboxing import FailsafeException
from servo.config import settings
from servo.control.confirmation import build_confirmation_callback
from servo.control.loop import ServoControlLoop
from servo.perception.uia_sensor import UIASensor
from servo.planner import EscalationContext, PlannerError, get_planner

logger = logging.getLogger(__name__)

MAX_RECOVERIES = 1


class TaskJob(BaseModel):
    """Job submitted from async daemon thread to STA COM worker thread."""
    job_id: str
    action_type: str  # 'sense_scene', 'execute_subgoal', 'run_plan'
    hwnd: Optional[int] = None
    params: Dict[str, Any] = Field(default_factory=dict)


class JobResult(BaseModel):
    """Result returned from STA COM worker thread back to async caller."""
    job_id: str
    success: bool
    data: Optional[Dict[str, Any]] = None
    error: Optional[str] = None


class AutomationWorker:
    """Manages the dedicated STA COM background thread and task queues."""

    def __init__(self) -> None:
        self.job_queue: queue.Queue[Optional[TaskJob]] = queue.Queue()
        self.result_futures: Dict[str, queue.Queue[JobResult]] = {}
        self._cancelled: Set[str] = set()
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._lock = threading.Lock()

    def start(self) -> None:
        """Start the worker thread with STA COM initialization."""
        with self._lock:
            if self._running:
                return
            self._running = True
            self._thread = threading.Thread(target=self._worker_loop, name="ServoSTAWorker", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        """Stop the worker thread cleanly."""
        with self._lock:
            if not self._running:
                return
            self._running = False
            self.job_queue.put(None)
            thread = self._thread
            self._thread = None
        if thread is not None:
            thread.join(timeout=2.0)

    def is_cancelled(self, job_id: str) -> bool:
        with self._lock:
            return job_id in self._cancelled

    def cancel_job(self, job_id: str) -> bool:
        """Mark a job for cancellation (stops before next step if in-flight or skips if queued)."""
        with self._lock:
            self._cancelled.add(job_id)
            return True

    def submit_job(self, job: TaskJob, timeout: float = 10.0) -> JobResult:
        """Submit a job synchronously from any thread and wait for result.

        On timeout the job is marked cancelled. A queued job is then skipped, and a
        running plan stops before its next sub-goal. Previously a timed-out job kept
        running and could click/type long after the caller was told it had failed.
        An action already in progress cannot be interrupted mid-way.
        """
        if not self._running:
            self.start()

        res_q: queue.Queue[JobResult] = queue.Queue(maxsize=1)
        with self._lock:
            self.result_futures[job.job_id] = res_q

        self.job_queue.put(job)
        try:
            return res_q.get(timeout=timeout)
        except queue.Empty:
            with self._lock:
                self._cancelled.add(job.job_id)
            return JobResult(
                job_id=job.job_id,
                success=False,
                error=f"Job {job.job_id} timed out after {timeout} seconds and was cancelled "
                      "(any action already in progress may have completed).",
            )
        finally:
            with self._lock:
                self.result_futures.pop(job.job_id, None)

    def _worker_loop(self) -> None:
        """Execution loop running inside dedicated thread with COM STA initialized."""
        if sys.platform == "win32":
            try:
                import pythoncom
                pythoncom.CoInitialize()
            except Exception:
                logger.warning("CoInitialize failed on STA worker thread.", exc_info=True)

        try:
            while self._running:
                job = self.job_queue.get()
                if job is None:
                    break

                if self.is_cancelled(job.job_id):
                    result = JobResult(job_id=job.job_id, success=False, error="Cancelled before start.")
                else:
                    result = self._dispatch_job(job)

                with self._lock:
                    self._cancelled.discard(job.job_id)
                    res_q = self.result_futures.get(job.job_id)
                if res_q is not None:
                    res_q.put(result)
        finally:
            if sys.platform == "win32":
                try:
                    import pythoncom
                    pythoncom.CoUninitialize()
                except Exception:
                    pass

    def _dispatch_job(self, job: TaskJob) -> JobResult:
        """Dispatch job within the STA COM thread context."""
        try:
            if job.action_type == "sense_scene":
                scene = UIASensor(job.hwnd).sense()
                if scene.sense_error:
                    return JobResult(job_id=job.job_id, success=False, error=scene.sense_error)
                return JobResult(job_id=job.job_id, success=True, data=scene.model_dump())

            if job.action_type == "execute_subgoal":
                loop = ServoControlLoop(
                    hwnd=job.hwnd,
                    fast_mode=job.params.get("fast_mode", False),
                    mode=job.params.get("mode"),
                    confirmation_callback=build_confirmation_callback(),
                    should_abort=lambda: self.is_cancelled(job.job_id),
                )
                res = loop.execute_subgoal(
                    step_index=job.params.get("step_index", 1),
                    sub_goal=job.params.get("sub_goal", ""),
                    target_label_hint=job.params.get("target_label_hint", ""),
                    action_type=job.params.get("action_type", "click"),
                    text_to_type=job.params.get("text_to_type", ""),
                    expected_outcome=job.params.get("expected_outcome", ""),
                )
                return JobResult(job_id=job.job_id, success=res.success, data=res.model_dump(), error=res.step_log.error)

            if job.action_type == "run_plan":
                data = run_plan(job, lambda: self.is_cancelled(job.job_id))
                return JobResult(job_id=job.job_id, success=data["status"] == "passed", data=data, error=data.get("error"))

            return JobResult(job_id=job.job_id, success=False, error=f"Unknown action: {job.action_type}")
        except FailsafeException as e:
            logger.warning("Operator failsafe triggered: %s", e)
            return JobResult(job_id=job.job_id, success=False, error=str(e))
        except Exception as e:
            logger.exception("Job %s failed", job.job_id)
            return JobResult(job_id=job.job_id, success=False, error=f"{e.__class__.__name__}: {e}")


def run_plan(job: TaskJob, is_cancelled: Callable[[], bool]) -> Dict[str, Any]:
    """Execute a MacroPlan through SystemHandoffCoordinator on the STA worker thread."""
    from servo.control.handoff import SystemHandoffCoordinator
    from servo.daemon.copilot import launch_or_focus_app
    from servo.perception import windows
    from servo.perception.scene_index import SceneIndex
    from servo.perception.uia_sensor import UIASensor
    from servo.planner.tool_calling import MacroPlan, PlanStep, parse_deterministic_intent

    p = job.params
    goal: str = p.get("goal", "")
    mode = p.get("mode") or settings.mode

    # 1. Resolve or Decompose Plan
    plan: Optional[MacroPlan] = None
    if "plan" in p and p["plan"]:
        raw_plan = p["plan"]
        if isinstance(raw_plan, MacroPlan):
            plan = raw_plan
        elif isinstance(raw_plan, dict):
            plan = MacroPlan.model_validate(raw_plan)

    if plan is None:
        scene = None
        if job.hwnd and windows.is_window(job.hwnd):
            try:
                scene = UIASensor(job.hwnd).sense()
            except Exception:
                scene = None

        try:
            planner = get_planner(p.get("planner"))
            if hasattr(planner, "decompose_macro"):
                try:
                    res = planner.decompose_macro(goal, scene)
                    if isinstance(res, MacroPlan):
                        plan = res
                except Exception as e:
                    logger.warning("decompose_macro failed (%s); attempting fallback", e)
            if plan is None and hasattr(planner, "decompose"):
                subgoals = list(planner.decompose(goal, scene or SceneIndex(hwnd=job.hwnd or 0, window_title="")))
                steps = [
                    PlanStep(
                        step_index=sg.step_index,
                        tool_name=sg.tool_name or ("click_element" if sg.action_type == "click" else ("type_text" if sg.action_type == "type" else sg.action_type)),
                        arguments=sg.arguments or (
                            {"target": sg.target_element_hint, "text": sg.text_to_type}
                            if sg.action_type == "type"
                            else ({"target": sg.target_element_hint} if sg.target_element_hint else {})
                        ),
                        rationale=sg.instruction,
                        canonical_action=sg.canonical_action,
                    )
                    for sg in subgoals
                ]
                plan = MacroPlan(goal=goal, steps=steps)
            if plan is None:
                plan = parse_deterministic_intent(goal)
        except Exception as e:
            return {
                "goal": goal,
                "mode": mode,
                "plan": [],
                "steps": [],
                "status": "failed",
                "escalated": False,
                "error": f"Planning failed: {e}",
                "failure_artifact": None,
            }

    if not plan or not plan.steps:
        return {
            "goal": goal,
            "mode": mode,
            "plan": [],
            "steps": [],
            "status": "failed",
            "escalated": False,
            "error": "No actionable steps in plan.",
            "failure_artifact": None,
        }

    # 2. Execute via SystemHandoffCoordinator
    coordinator = SystemHandoffCoordinator(
        target_hwnd=job.hwnd,
        fast_mode=p.get("fast_mode", False),
        confirmation_callback=build_confirmation_callback(),
        should_abort=is_cancelled,
        mode=mode,
    )
    summary = coordinator.execute_plan(plan, app_launcher=launch_or_focus_app)

    # 3. Package steps with dual format (both flat report and nested step_log dict)
    steps_data = []
    for r in summary.step_reports:
        if summary.aborted and r.description == "Execution aborted by user or caller timeout.":
            continue
        step_dict = r.model_dump()
        step_dict["step_log"] = {
            "sub_goal": r.description or r.target or "",
            "perception_source": r.perception_source or "uia",
            "tool_call": f"{r.action_type}({r.target or ''})",
            "jev_pre_risk": r.jev_risk or "low",
            "jev_post_ok": r.success,
            "latency_ms": r.latency_ms,
            "confirmation": r.confirmation,
            "error": r.error,
        }
        step_dict["escalation_required"] = False
        steps_data.append(step_dict)

    status = "passed" if summary.success else ("cancelled" if summary.aborted else "failed")
    last_err = "Cancelled (caller timed out)." if summary.aborted else (
        summary.step_reports[-1].error if (not summary.success and summary.step_reports) else None
    )
    return {
        "goal": summary.goal,
        "mode": mode,
        "plan": [s.model_dump() for s in plan.steps],
        "steps": steps_data,
        "summary": summary.model_dump(),
        "status": status,
        "escalated": False,
        "error": last_err,
        "failure_artifact": None,
    }


# Global default STA worker instance
worker = AutomationWorker()
