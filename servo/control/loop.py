"""Closed-Loop State Machine and Local Recovery Ladder.

See ADR-09 and Section 6 in SERVO_ARCHITECTURE_V1.md, and SERVO_HARDENING_NOTES.md.

Ladder: resolve the target with UIA; if UIA cannot find it, try Mirage. Actuate at
most ONCE per sub-goal. If the actuation cannot be verified, escalate to System 2
instead of acting again; a second click or a second burst of typing is a different
action from the one that was gated and approved.
"""

from __future__ import annotations

import logging
import time
from typing import Callable, Literal, Optional

from pydantic import BaseModel

from servo.config import settings
from servo.perception.uia_sensor import UIASensor
from servo.perception.mirage_sensor import MirageSensor
from servo.perception.scene_index import SceneElement, SceneIndex
from servo.actuation.needle_tools import ActuationError, NeedleToolSurface
from servo.actuation.sandboxing import (
    FailsafeException,
    SandboxViolationException,
    check_target_allowed,
)
from servo.control.confirmation import ConfirmationCallback, ConfirmationRequest
from servo.control.jev_gate import JevGate
from servo.control.settling import SettlingEngine
from servo.perception import windows

logger = logging.getLogger(__name__)

SUPPORTED_ACTIONS = ("click", "type")

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
    # None when no confirmation was needed; otherwise "approved" or "rejected".
    confirmation: Optional[str] = None


class ExecutionResult(BaseModel):
    """Result of an atomic step or sub-goal execution through the reflex arc."""
    success: bool
    attempts_used: int
    final_scene: SceneIndex
    step_log: StepLog
    escalation_required: bool = False
    # True once input has been sent to the OS for this sub-goal (even if it then
    # failed verification). Used to guarantee we never actuate twice.
    actuated: bool = False
    failure_artifact: Optional[dict] = None


class ServoControlLoop:
    """The closed-loop servomechanism coordinating sensing, gating, actuation, and recovery."""

    def __init__(
        self,
        hwnd: int,
        fast_mode: bool = False,
        confirmation_callback: Optional[ConfirmationCallback] = None,
        mode: Optional[Literal["assistant", "harness"]] = None,
        should_abort: Optional[Callable[[], bool]] = None,
    ) -> None:
        self.hwnd = hwnd
        self.fast_mode = fast_mode
        self.confirmation_callback = confirmation_callback
        self.mode = mode or settings.mode
        # Checked right before actuating. A human may take a while to answer the
        # confirmation dialog; if the caller has given up in the meantime, a late
        # "Yes" must not act on a run that was already reported as failed.
        self.should_abort = should_abort

        self.uia_sensor = UIASensor(hwnd)
        self.mirage_sensor = MirageSensor(hwnd)
        self.tools = NeedleToolSurface(hwnd, fast_mode=fast_mode)
        self.jev = JevGate()
        self.settling = SettlingEngine()

    # ------------------------------------------------------------------ helpers

    def _fail(
        self,
        scene: SceneIndex,
        step_index: int,
        sub_goal: str,
        source: str,
        tool_call: str,
        error: str,
        risk: str = "aborted",
        escalate: bool = True,
        actuated: bool = False,
        latency_ms: int = 0,
    ) -> ExecutionResult:
        return ExecutionResult(
            success=False,
            attempts_used=1,
            final_scene=scene,
            step_log=StepLog(
                step_index=step_index,
                sub_goal=sub_goal,
                perception_source=source,
                tool_call=tool_call,
                jev_pre_risk=risk,
                jev_post_ok=False,
                latency_ms=latency_ms,
                error=error,
            ),
            escalation_required=escalate,
            actuated=actuated,
            failure_artifact={
                "failed_step": step_index,
                "sub_goal": sub_goal,
                "error": error,
                "scene_elements_count": len(scene.elements),
            },
        )

    @staticmethod
    def resolve_target(scene: SceneIndex, hint: str) -> Optional[SceneElement]:
        """Resolve a label hint to exactly one element, or None.

        Prefers an exact (case-insensitive) label match, then a unique substring match.
        Never falls back to an arbitrary element: the old "use the first element"
        fallback turned every unmatched hint into a click on whatever control UIA
        happened to enumerate first.
        """
        if not hint or not hint.strip():
            return None
        q = hint.strip().lower()
        exact = [el for el in scene.elements.values() if el.label.strip().lower() == q]
        if exact:
            return exact[0]
        partial = scene.find_by_label(hint)
        if len(partial) == 1:
            return partial[0]
        return None

    # --------------------------------------------------------------- main entry

    def execute_subgoal(
        self,
        step_index: int,
        sub_goal: str,
        target_label_hint: str,
        action_type: str = "click",
        text_to_type: str = "",
        expected_outcome: str = "",
    ) -> ExecutionResult:
        """Execute a sub-goal through the closed-loop recovery ladder."""
        t_start = time.perf_counter()
        tool_desc = f"{action_type}('{target_label_hint}')"

        if action_type not in SUPPORTED_ACTIONS:
            # Previously any unknown action ("hotkey", "scroll", ...) fell through to a click.
            return self._fail(
                SceneIndex(hwnd=self.hwnd, window_title="", source="uia"),
                step_index, sub_goal, "none", tool_desc,
                f"Unsupported action_type '{action_type}'. Supported: {', '.join(SUPPORTED_ACTIONS)}.",
            )
        if action_type == "type" and not text_to_type:
            return self._fail(
                SceneIndex(hwnd=self.hwnd, window_title="", source="uia"),
                step_index, sub_goal, "none", tool_desc,
                "action_type 'type' requires non-empty text_to_type.",
            )

        try:
            check_target_allowed(self.hwnd, self.mode)
        except SandboxViolationException as e:
            return self._fail(
                SceneIndex(hwnd=self.hwnd, window_title="", source="uia"),
                step_index, sub_goal, "none", tool_desc, str(e), escalate=False,
            )

        # ATTEMPT 1: UIA. Used whenever it can resolve the target, regardless of how
        # many controls the window has (the old "< 3 controls => skip UIA" rule sent
        # small dialogs straight to the visual fallback).
        uia_scene = self.uia_sensor.sense()
        if not uia_scene.sense_error:
            target = self.resolve_target(uia_scene, target_label_hint)
            if target is not None:
                res = self._run_reflex_pass(
                    uia_scene, target, step_index, sub_goal, action_type, text_to_type,
                    expected_outcome, "uia", self.uia_sensor.sense,
                )
                res.attempts_used = 1
                return self._finalize(res, t_start)

        # ATTEMPT 2: Mirage visual fallback, only reached when nothing has been actuated.
        mirage_sense = lambda: self.mirage_sensor.sense(window_title=uia_scene.window_title)  # noqa: E731
        mirage_scene = mirage_sense()
        target = None if mirage_scene.sense_error else self.resolve_target(mirage_scene, target_label_hint)
        if target is not None:
            res = self._run_reflex_pass(
                mirage_scene, target, step_index, sub_goal, action_type, text_to_type,
                expected_outcome, "mirage", mirage_sense,
            )
            res.attempts_used = 2
            return self._finalize(res, t_start)

        reasons = []
        if uia_scene.sense_error:
            reasons.append(f"UIA failed: {uia_scene.sense_error}")
        else:
            reasons.append(f"UIA found no unique element matching '{target_label_hint}'")
        reasons.append(
            f"Mirage failed: {mirage_scene.sense_error}" if mirage_scene.sense_error
            else f"Mirage found no unique element matching '{target_label_hint}'"
        )
        res = self._fail(
            uia_scene, step_index, sub_goal, "hybrid", tool_desc, "; ".join(reasons),
        )
        res.attempts_used = 2
        return self._finalize(res, t_start)

    def _finalize(self, res: ExecutionResult, t_start: float) -> ExecutionResult:
        res.step_log.latency_ms = int((time.perf_counter() - t_start) * 1000)
        return res

    def _run_reflex_pass(
        self,
        scene: SceneIndex,
        target_el: SceneElement,
        step_index: int,
        sub_goal: str,
        action_type: str,
        text_to_type: str,
        expected_outcome: str,
        perception_source: str,
        sensor_fn: Callable[[], SceneIndex],
    ) -> ExecutionResult:
        """Single pass: pre-risk gate, actuation, settling, and post-verify."""
        tool_call_repr = (
            f"type_text(element_id={target_el.element_id}, text={text_to_type!r})"
            if action_type == "type"
            else f"click(element_id={target_el.element_id})"
        )

        # 1. Jev Pre-Action Risk Gate
        pre_risk = self.jev.evaluate_pre_risk(sub_goal, tool_call_repr, target_el.label)

        if not pre_risk.scope_valid:
            return self._fail(
                scene, step_index, sub_goal, perception_source, tool_call_repr,
                f"Jev gate rejected action as out of scope: {pre_risk.reason}", risk=pre_risk.risk_level,
            )

        # 2. Confirmation. Fails CLOSED: in assistant mode a medium/high-risk action with
        # nobody to ask is refused. Previously a missing callback meant "proceed".
        confirmation: Optional[str] = None
        if self.mode == "assistant" and pre_risk.risk_level in ("medium", "high"):
            approved = False
            if self.confirmation_callback is not None:
                request = ConfirmationRequest(
                    sub_goal=sub_goal,
                    tool_call=tool_call_repr,
                    element_label=target_el.label,
                    risk_level=pre_risk.risk_level,
                    reason=pre_risk.reason,
                    hwnd=self.hwnd,
                    window_title=scene.window_title,
                    process_name=windows.get_process_name(self.hwnd),
                )
                try:
                    approved = bool(self.confirmation_callback(request))
                except Exception as e:
                    logger.warning("Confirmation callback raised %s; treating as rejection.", e)
            confirmation = "approved" if approved else "rejected"
            if not approved:
                reason = (
                    "Action rejected by user confirmation (declined, timed out, or dialog unavailable)."
                    if self.confirmation_callback is not None
                    else f"{pre_risk.risk_level.capitalize()}-risk action requires confirmation "
                         f"({pre_risk.reason}) and no confirmation channel is available."
                )
                res = self._fail(
                    scene, step_index, sub_goal, perception_source, tool_call_repr,
                    reason, risk=pre_risk.risk_level, escalate=False,
                )
                res.step_log.confirmation = confirmation
                return res

        if self.should_abort is not None and self.should_abort():
            res = self._fail(
                scene, step_index, sub_goal, perception_source, tool_call_repr,
                "Run was cancelled before actuation (caller timed out).",
                risk=pre_risk.risk_level, escalate=False,
            )
            res.step_log.confirmation = confirmation
            return res

        # 3. Actuate (exactly once)
        try:
            if action_type == "type":
                self.tools.type_text(scene, target_el.element_id, text_to_type)
            else:
                self.tools.click(scene, target_el.element_id, button="left")
        except FailsafeException:
            raise  # operator abort must propagate and stop the whole plan
        except (ActuationError, SandboxViolationException) as e:
            # We cannot be sure no input reached the OS (e.g. click then foreground
            # failure while typing), so mark actuated and do not retry.
            res = self._fail(
                scene, step_index, sub_goal, perception_source, tool_call_repr,
                f"Actuation failed: {e}", risk=pre_risk.risk_level, actuated=True,
            )
            res.step_log.confirmation = confirmation
            return res

        # 4. Adaptive Settling & Post-Action Sensing
        post_scene, delta = self.settling.settle_and_diff(scene, sensor_fn)

        # 5. Jev Post-Action Verification Gate
        post_eval = self.jev.evaluate_post_transition(
            sub_goal, delta, expected_outcome, typed_text=text_to_type if action_type == "type" else "",
        )

        return ExecutionResult(
            success=post_eval.transition_ok,
            attempts_used=1,
            final_scene=post_scene,
            step_log=StepLog(
                step_index=step_index,
                sub_goal=sub_goal,
                perception_source=perception_source,
                tool_call=tool_call_repr,
                jev_pre_risk=pre_risk.risk_level,
                jev_post_ok=post_eval.transition_ok,
                latency_ms=0,
                error=None if post_eval.transition_ok else post_eval.explanation,
                confirmation=confirmation,
            ),
            escalation_required=not post_eval.transition_ok,
            actuated=True,
            failure_artifact=None if post_eval.transition_ok else {
                "failed_step": step_index,
                "sub_goal": sub_goal,
                "error": post_eval.explanation,
                "delta": delta.model_dump(),
            },
        )
