"""Servo as a WindowsAgentArena agent (copied into WAA as client/mm_agents/servo/agent.py).

This file runs in WAA's client container, NOT in Servo's environment: it must only use the
standard library and ``requests`` (both available there).

How it works: on the first ``predict()`` of a task it starts Servo inside the VM through
WAA's own VM server (``/execute_windows`` runs Python in the server process, which lives in
the logged-in desktop session), then polls a status file until Servo finishes, and ends the
episode with DONE (or FAIL if Servo says it could not do it). WAA's evaluator then scores
the VM's final state, the same as for every other agent. Servo never sees the evaluator.

The VM server's ``/execute`` endpoint kills commands after 120 s, which is why the task is
started detached and polled instead of run in one call.
"""

from __future__ import annotations

import base64
import json
import logging
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger("desktopenv.agent.servo")

STATUS_DIR = r"C:\servo_waa"
# Written by servo.integrations.winarena.export_planner into WAA's shared setup folder.
PLANNER_CONFIG = r"\\host.lan\Data\servo_planner.json"


class ServoAgent:
    action_space = "code_block"

    def __init__(self, vm_ip: str = "20.20.20.21", port: int = 5000, task_timeout: float = 900.0,
                 poll_seconds: float = 5.0) -> None:
        self.base = f"http://{vm_ip}:{port}"
        self.task_timeout = task_timeout
        self.poll_seconds = poll_seconds
        self.result: Optional[Dict[str, Any]] = None

    def reset(self, *args: Any, **kwargs: Any) -> None:
        self.result = None

    # ------------------------------------------------------------------ VM calls

    def _start(self, instruction: str, status_path: str) -> None:
        goal = base64.b64encode(instruction.encode("utf-8")).decode("ascii")
        code = (
            "import subprocess, sys\n"
            f"subprocess.Popen([sys.executable, '-m', 'servo.integrations.winarena.vm_runner', "
            f"'--goal-b64', {goal!r}, '--status', {status_path!r}, '--planner-config', {PLANNER_CONFIG!r}], "
            f"creationflags=0x08000000)\n"
        )
        r = requests.post(f"{self.base}/execute_windows", json={"command": code}, timeout=60)
        r.raise_for_status()

    def _status(self, status_path: str) -> Optional[Dict[str, Any]]:
        try:
            r = requests.post(f"{self.base}/execute",
                              json={"command": ["cmd", "/c", "type", status_path], "shell": False}, timeout=60)
            out = (r.json() or {}).get("output") or ""
            return json.loads(out) if out.strip() else None
        except Exception:
            return None

    # ------------------------------------------------------------------ WAA interface

    def predict(self, instruction: str, obs: Dict[str, Any]) -> Tuple[str, List[str], Dict[str, Any], None]:
        if self.result is not None:
            return self.result.get("reply", ""), ["DONE" if self.result.get("success") else "FAIL"], \
                {"servo": self.result}, None
        status_path = rf"{STATUS_DIR}\{uuid.uuid4().hex}.json"
        t0 = time.time()
        try:
            self._start(instruction, status_path)
        except Exception as e:
            logger.error("Could not start Servo in the VM: %s", e)
            self.result = {"success": False, "reply": f"could not start Servo in the VM: {e}"}
            return self.result["reply"], ["FAIL"], {"servo": self.result}, None
        state: Optional[Dict[str, Any]] = None
        while time.time() - t0 < self.task_timeout:
            time.sleep(self.poll_seconds)
            state = self._status(status_path)
            if state and state.get("state") in ("done", "error"):
                break
        if not state or state.get("state") not in ("done", "error"):
            state = {"success": False, "reply": f"Servo did not finish within {self.task_timeout:.0f}s", **(state or {})}
        self.result = state
        logger.info("Servo finished: success=%s reply=%s", state.get("success"), str(state.get("reply"))[:200])
        return str(state.get("reply") or ""), ["DONE" if state.get("success") else "FAIL"], {"servo": state}, None
