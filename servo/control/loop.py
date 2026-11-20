"""Closed-Loop State Machine and 2-Step Local Recovery Ladder.

See ADR-09 and Section 6 in SERVO_ARCHITECTURE_V1.md.
Attempt 1 (UIA) -> Attempt 2 (Mirage Fallback) -> System 2 Escalation.
"""

from __future__ import annotations

import time
from typing import Callable, Optional
from pydantic import BaseModel, Field

from servo.config import settings
from servo.perception.uia_sensor import UIASensor
from servo.perception.mirage_sensor import MirageSensor
from servo.perception.scene_index import SceneIndex, SceneDelta
from servo.actuation.needle_tools import NeedleToolSurface
from servo.control.jev_gate import JevGate, PreRiskResult, PostTransitionResult
from servo.control.settling import SettlingEngine


class StepLog(BaseModel):
    """Detailed log record of a single step execution."""
    step_index: int
    sub_goal: str
    perception_source: str
    tool_call: str
    jev_pre_risk: str
    jev_post_ok: bool
    latency_ms: int
    error: Optional[str] = None


class ExecutionResult(BaseModel):
    """Result of an atomic step or sub-goal execution through the reflex arc."""
    success: bool
    attempts_used: int
    final_scene: SceneIndex
    step_log: StepLog
    escalation_required: bool = False
    failure_artifact: Optional[dict] = None


class ServoControlLoop:
    """The closed-loop servomechanism coordinating sensing, gating, actuation, and recovery."""

    def __init__(
        self,
        hwnd: int,
        fast_mode: bool = False,
        confirmation_callback: Optional[Callable[[str, PreRiskResult], bool]] = None,
    ) -> None:
        self.hwnd = hwnd
        self.fast_mode = fast_mode
        self.confirmation_callback = confirmation_callback

        self.uia_sensor = UIASensor(hwnd)
        self.mirage_sensor = MirageSensor(hwnd)
        self.tools = NeedleToolSurface(hwnd, fast_mode=fast_mode)
        self.jev = JevGate()
        self.settling = SettlingEngine()

    def execute_subgoal(
        self,
        step_index: int,
        sub_goal: str,
        target_label_hint: str,
        action_type: str = "click",
        text_to_type: str = "",
        expected_outcome: str = "",
    ) -> ExecutionResult:
        """Execute a sub-goal through the 2-step closed-loop recovery ladder."""
        t_start = time.perf_counter()

        # =========================================================================
        # ATTEMPT 1: Fast Path via UI Automation (UIA)
        # =========================================================================
        initial_scene = self.uia_sensor.sense()

        # Check if UIA detected controls. If controls are sparse (< 3), force Attempt 2 immediately
        if len(initial_scene.elements) >= 3:
            attempt1_res = self._run_reflex_pass(
                scene=initial_scene,
                step_index=step_index,
                sub_goal=sub_goal,
                target_label_hint=target_label_hint,
                action_type=action_type,
                text_to_type=text_to_type,
                expected_outcome=expected_outcome,
                perception_source="uia",
                sensor_fn=self.uia_sensor.sense,
            )
            if attempt1_res.success:
                attempt1_res.attempts_used = 1
                return attempt1_res

        # =========================================================================
        # ATTEMPT 2: Visual Fallback via Cactus Mirage (22 MB)
        # =========================================================================
        mirage_scene = self.mirage_sensor.sense(window_title=initial_scene.window_title)
        attempt2_res = self._run_reflex_pass(
            scene=mirage_scene,
            step_index=step_index,
            sub_goal=sub_goal,
            target_label_hint=target_label_hint,
            action_type=action_type,
            text_to_type=text_to_type,
            expected_outcome=expected_outcome,
            perception_source="mirage",
            sensor_fn=lambda: self.mirage_sensor.sense(window_title=initial_scene.window_title),
        )
        if attempt2_res.success:
            attempt2_res.attempts_used = 2
            return attempt2_res

        # =========================================================================
        # ESCALATION: Attempt 1 & 2 Failed -> Package context for System 2
        # =========================================================================
        elapsed_ms = int((time.perf_counter() - t_start) * 1000)
        failure_log = StepLog(
            step_index=step_index,
            sub_goal=sub_goal,
            perception_source="hybrid",
            tool_call=f"{action_type}('{target_label_hint}')",
            jev_pre_risk="escalated",
            jev_post_ok=False,
            latency_ms=elapsed_ms,
            error="Failed post-action verification after 2-step recovery ladder.",
        )

        return ExecutionResult(
            success=False,
            attempts_used=2,
            final_scene=attempt2_res.final_scene,
            step_log=failure_log,
            escalation_required=True,
            failure_artifact={
                "failed_step": step_index,
                "sub_goal": sub_goal,
                "target_hint": target_label_hint,
                "scene_elements_count": len(attempt2_res.final_scene.elements),
            },
        )

    def _run_reflex_pass(
        self,
        scene: SceneIndex,
        step_index: int,
        sub_goal: str,
        target_label_hint: str,
        action_type: str,
        text_to_type: str,
        expected_outcome: str,
        perception_source: str,
        sensor_fn: Callable[[], SceneIndex],
    ) -> ExecutionResult:
        """Execute a single pass: target element resolution, pre-risk gate, actuation, settling, and post-verify."""
        t_pass_start = time.perf_counter()

        # 1. Resolve Target Element
        matches = scene.find_by_label(target_label_hint)
        if not matches:
            # Fall back to first element if hint not matched
            matches = list(scene.elements.values())[:1]

        if not matches:
            return ExecutionResult(
                success=False,
                attempts_used=1,
                final_scene=scene,
                step_log=StepLog(
                    step_index=step_index,
                    sub_goal=sub_goal,
                    perception_source=perception_source,
                    tool_call=f"{action_type}('{target_label_hint}')",
                    jev_pre_risk="aborted",
                    jev_post_ok=False,
                    latency_ms=0,
                    error=f"No matching element for hint '{target_label_hint}'",
                ),
            )

        target_el = matches[0]
        tool_call_repr = (
            f"type_text(element_id={target_el.element_id}, text='{text_to_type}')"
            if action_type == "type"
            else f"click(element_id={target_el.element_id})"
        )

        # 2. Jev Pre-Action Risk Gate
        pre_risk = self.jev.evaluate_pre_risk(sub_goal, tool_call_repr, target_el.label)

        # In assistant mode, prompt user on medium or high risk
        if settings.mode == "assistant" and pre_risk.risk_level in ("medium", "high"):
            if self.confirmation_callback is not None:
                approved = self.confirmation_callback(sub_goal, pre_risk)
                if not approved:
                    return ExecutionResult(
                        success=False,
                        attempts_used=1,
                        final_scene=scene,
                        step_log=StepLog(
                            step_index=step_index,
                            sub_goal=sub_goal,
                            perception_source=perception_source,
                            tool_call=tool_call_repr,
                            jev_pre_risk=pre_risk.risk_level,
                            jev_post_ok=False,
                            latency_ms=0,
                            error="Action rejected by user confirmation.",
                        ),
                    )

        # 3. Actuate
        if action_type == "type":
            self.tools.type_text(scene, target_el.element_id, text_to_type)
        else:
            self.tools.click(scene, target_el.element_id, button="left")

        # 4. Adaptive Settling & Post-Action Sensing
        post_scene, delta = self.settling.settle_and_diff(scene, sensor_fn)

        # 5. Jev Post-Action Verification Gate
        post_eval = self.jev.evaluate_post_transition(sub_goal, delta, expected_outcome)

        elapsed_ms = int((time.perf_counter() - t_pass_start) * 1000)
        step_log = StepLog(
            step_index=step_index,
            sub_goal=sub_goal,
            perception_source=perception_source,
            tool_call=tool_call_repr,
            jev_pre_risk=pre_risk.risk_level,
            jev_post_ok=post_eval.transition_ok,
            latency_ms=elapsed_ms,
        )

        return ExecutionResult(
            success=post_eval.transition_ok,
            attempts_used=1,
            final_scene=post_scene,
            step_log=step_log,
        )
