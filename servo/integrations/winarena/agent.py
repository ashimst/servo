"""Windows Agent Arena (WAA) & OSWorld standard Agent interface implementation for Servo."""

from __future__ import annotations

import time
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from servo.cognition.router import CascadeRouter, RunOutcome, new_run_id
from servo.cognition.trace import Tracer
from servo.daemon.copilot import launch_or_focus_app
from servo.integrations.winarena.action_converter import tool_call_to_pyautogui


class ServoAgent:
    """WindowsAgentArena-compliant agent powered by Servo's cognitive cascade runtime.

    Interfaces with WindowsAgentArena and OSWorld benchmark environments via the standard
    reset() -> predict() contract.

    Attributes:
        model_name: Name of the underlying planner model or "servo-cascade".
        confirmation_mode: "auto" (approve read/safe), "strict" (block high-risk), or custom callback.
    """

    def __init__(
        self,
        model_name: str = "servo-cascade",
        confirmation_mode: str = "auto",
        router_factory: Optional[Callable[[], CascadeRouter]] = None,
    ) -> None:
        self.model_name = model_name
        self.confirmation_mode = confirmation_mode
        self.router_factory = router_factory
        self.current_run_id: str = new_run_id()
        self.tracer: Tracer = Tracer(self.current_run_id)
        self.last_outcome: Optional[RunOutcome] = None
        self.trajectory_history: List[Dict[str, Any]] = []
        self._action_queue: List[str] = []

    def reset(self, setup_config: Optional[Dict[str, Any]] = None) -> None:
        """Reset internal agent state at the beginning of a benchmark task."""
        self.current_run_id = new_run_id()
        self.tracer = Tracer(self.current_run_id)
        self.last_outcome = None
        self.trajectory_history.clear()
        self._action_queue.clear()

    def _default_confirm(self, req: Any) -> bool:
        """Default confirmation policy for non-interactive benchmark evaluation."""
        if self.confirmation_mode == "strict" and req.risk_level in ("medium", "high"):
            return False
        # In benchmark mode, permit read/safe ops, block destructive deletions
        call = str(getattr(req, "tool_call", ""))
        if req.risk_level == "high" and any(k in call.lower() for k in ("format", "rmdir /s", "del /f")):
            return False
        return True

    def _get_router(self) -> CascadeRouter:
        if self.router_factory:
            return self.router_factory()
        return CascadeRouter(
            tracer=self.tracer,
            app_launcher=launch_or_focus_app,
            confirmation_callback=self._default_confirm,
        )

    def predict(
        self,
        instruction: str,
        observation: Optional[Dict[str, Any]] = None,
    ) -> Union[str, List[str]]:
        """Predict the next action(s) for the current task instruction and environment observation.

        If actions were queued from a multi-step trajectory, returns the next action in sequence.
        When all actions complete, returns "DONE".
        """
        # If we still have actions queued from a previous planning phase, pop the next action
        if self._action_queue:
            action = self._action_queue.pop(0)
            return action

        # If a run already finished, signal DONE
        if self.last_outcome is not None and self.last_outcome.success:
            return "DONE"

        # Execute instruction via Servo's CascadeRouter
        t0 = time.perf_counter()
        router = self._get_router()
        try:
            outcome = router.run(instruction)
            self.last_outcome = outcome
        except Exception as e:
            return f"# Error during execution: {e}\nFAIL"

        latency = time.perf_counter() - t0

        # Translate executed StepRecords into standard WindowsAgentArena pyautogui code snippets
        actions: List[str] = []
        for step in outcome.steps:
            call = getattr(step, "call", "") or step.command
            pyautogui_code = tool_call_to_pyautogui(call)
            actions.append(pyautogui_code)

        self.trajectory_history.append({
            "run_id": outcome.run_id,
            "instruction": instruction,
            "success": outcome.success,
            "route": outcome.route,
            "steps": len(outcome.steps),
            "latency_seconds": round(latency, 2),
            "reply": outcome.reply,
        })

        if not actions:
            return "DONE" if outcome.success else "FAIL"

        # Queue remaining actions and return the first one
        self._action_queue.extend(actions[1:])
        return actions[0]

    def execute_goal(self, goal: str) -> Dict[str, Any]:
        """Direct execution entrypoint for benchmarking full closed-loop runs on Windows."""
        t0 = time.perf_counter()
        router = self._get_router()
        try:
            outcome = router.run(goal)
            self.last_outcome = outcome
            passed = bool(outcome.success)
            err = None
        except Exception as e:
            passed = False
            outcome = None
            err = str(e)

        duration = time.perf_counter() - t0
        return {
            "goal": goal,
            "passed": passed,
            "error": err,
            "route": getattr(outcome, "route", "error") if outcome else "error",
            "reply": getattr(outcome, "reply", "") if outcome else "",
            "steps": [s.call or s.command for s in getattr(outcome, "steps", [])] if outcome else [],
            "latency_seconds": round(duration, 2),
            "run_id": getattr(outcome, "run_id", self.current_run_id) if outcome else self.current_run_id,
        }
