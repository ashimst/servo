"""Dedicated STA COM Automation Worker Thread.

See ADR-05 in SERVO_ARCHITECTURE_V1.md.
Isolates all synchronous Windows UI Automation COM calls and Win32 input syntheses
away from the FastAPI / FastMCP asyncio event loop.
"""

from __future__ import annotations

import queue
import sys
import threading
import time
from typing import Any, Callable, Dict, Optional
from pydantic import BaseModel

from servo.control.loop import ServoControlLoop, ExecutionResult
from servo.perception.scene_index import SceneIndex
from servo.perception.uia_sensor import UIASensor


class TaskJob(BaseModel):
    """Job submitted from async daemon thread to STA COM worker thread."""
    job_id: str
    action_type: str  # 'execute_subgoal', 'sense_scene', 'run_plan'
    hwnd: int
    params: Dict[str, Any] = {}


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
            if self._thread is not None:
                self._thread.join(timeout=2.0)
                self._thread = None

    def submit_job(self, job: TaskJob, timeout: float = 10.0) -> JobResult:
        """Submit a job synchronously from any thread and wait for result."""
        if not self._running:
            self.start()

        res_q: queue.Queue[JobResult] = queue.Queue(maxsize=1)
        with self._lock:
            self.result_futures[job.job_id] = res_q

        self.job_queue.put(job)
        try:
            return res_q.get(timeout=timeout)
        except queue.Empty:
            return JobResult(
                job_id=job.job_id,
                success=False,
                error=f"Job {job.job_id} timed out after {timeout} seconds on STA worker thread.",
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
                pass

        try:
            while self._running:
                job = self.job_queue.get()
                if job is None:
                    break

                result = self._dispatch_job(job)

                with self._lock:
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
                sensor = UIASensor(job.hwnd)
                scene = sensor.sense()
                return JobResult(
                    job_id=job.job_id,
                    success=True,
                    data=scene.model_dump(),
                )

            elif job.action_type == "execute_subgoal":
                loop = ServoControlLoop(hwnd=job.hwnd, fast_mode=job.params.get("fast_mode", False))
                res = loop.execute_subgoal(
                    step_index=job.params.get("step_index", 1),
                    sub_goal=job.params.get("sub_goal", ""),
                    target_label_hint=job.params.get("target_label_hint", ""),
                    action_type=job.params.get("action_type", "click"),
                    text_to_type=job.params.get("text_to_type", ""),
                    expected_outcome=job.params.get("expected_outcome", ""),
                )
                return JobResult(
                    job_id=job.job_id,
                    success=res.success,
                    data=res.model_dump(),
                )

            return JobResult(job_id=job.job_id, success=False, error=f"Unknown action: {job.action_type}")
        except Exception as e:
            return JobResult(job_id=job.job_id, success=False, error=str(e))


# Global default STA worker instance
worker = AutomationWorker()
