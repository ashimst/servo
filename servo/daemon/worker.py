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
    hwnd: int
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
    """Plan a goal with System 2 and drive each sub-goal through the control loop.

    Runs on the STA worker thread (sensing for the planner needs UIA). Stops at the
    first failed sub-goal; the planner gets one chance to supply replacement steps.
    """
    p = job.params
    goal: str = p["goal"]
    mode = p.get("mode") or settings.mode
    loop = ServoControlLoop(
        hwnd=job.hwnd,
        fast_mode=p.get("fast_mode", False),
        mode=mode,
        # Assistant mode asks the person at the desktop (SERVO_CONFIRMATION_CHANNEL).
        confirmation_callback=build_confirmation_callback(),
        should_abort=is_cancelled,
    )

    out: Dict[str, Any] = {"goal": goal, "mode": mode, "plan": [], "steps": [], "status": "failed",
                           "escalated": False, "error": None, "failure_artifact": None}

    scene = loop.uia_sensor.sense()
    if scene.sense_error:
        out["error"] = f"Could not read the target window: {scene.sense_error}"
        return out

    try:
        planner = get_planner(p.get("planner"))
        pending: List = list(planner.decompose(goal, scene))
    except PlannerError as e:
        out["error"] = f"Planning failed: {e}"
        return out
    out["plan"] = [sg.model_dump() for sg in pending]

    recoveries = 0
    while pending:
        if is_cancelled():
            out["status"] = "cancelled"
            out["error"] = "Cancelled (caller timed out)."
            return out
        sg = pending.pop(0)
        res = loop.execute_subgoal(
            step_index=sg.step_index,
            sub_goal=sg.instruction,
            target_label_hint=sg.target_element_hint or "",
            action_type=sg.action_type,
            text_to_type=sg.text_to_type,
            expected_outcome=sg.expected_outcome,
        )
        out["steps"].append({
            "sub_goal": sg.model_dump(),
            "success": res.success,
            "attempts_used": res.attempts_used,
            "actuated": res.actuated,
            "escalation_required": res.escalation_required,
            "step_log": res.step_log.model_dump(),
        })
        if res.success:
            continue

        out["error"] = res.step_log.error
        out["failure_artifact"] = res.failure_artifact
        if res.escalation_required:
            out["escalated"] = True
            if recoveries < MAX_RECOVERIES:
                replacement = planner.recover(EscalationContext(
                    failed_subgoal=sg,
                    attempt_count=res.attempts_used,
                    last_tool_call=res.step_log.tool_call,
                    scene_delta=(res.failure_artifact or {}).get("delta", {}),
                ))
                if replacement:
                    recoveries += 1
                    pending = list(replacement)
                    out["plan"].extend(s.model_dump() for s in replacement)
                    continue
            out["status"] = "escalated"
        return out

    out["status"] = "passed"
    out["error"] = None
    return out


# Global default STA worker instance
worker = AutomationWorker()
