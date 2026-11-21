"""Closed-Loop State Machine and Local Recovery Ladder.

See ADR-09 and Section 6 in SERVO_ARCHITECTURE_V1.md, and SERVO_HARDENING_NOTES.md.

Ladder: resolve the target with UIA; if UIA cannot find it, try Mirage. Actuate at
most ONCE per sub-goal. If the actuation cannot be verified, escalate to System 2
instead of acting again; a second click or a second burst of typing is a different
action from the one that was gated and approved.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Callable, List, Literal, Optional, Tuple, Union

from pydantic import BaseModel

from servo.actuation.alias_mapping import ACTION_ALIAS_MAP, AliasMapper, CanonicalActionType
from servo.actuation.needle_tools import ActuationError, NeedleToolSurface
from servo.actuation.sandboxing import (
    FailsafeException,
    SandboxViolationException,
    check_target_allowed,
    normalize_keys,
)
from servo.config import settings
from servo.control.confirmation import ConfirmationCallback, ConfirmationRequest
from servo.control.jev_gate import JevGate, PostTransitionResult
from servo.control.settling import SettlingEngine
from servo.perception import windows
from servo.perception.mirage_sensor import MirageSensor
from servo.perception.scene_index import SceneElement, SceneIndex
from servo.perception.uia_sensor import UIASensor
from servo.cognition.trace import NullTracer, Tracer

logger = logging.getLogger(__name__)

SUPPORTED_ACTIONS = (
    "click",
    "double_click",
    "right_click",
    "invoke",
    "type",
    "hotkey",
    "scroll",
    "wait",
)

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
        tracer: Optional[Tracer] = None,
    ) -> None:
        self.hwnd = hwnd
        self.tracer = tracer or NullTracer()
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
    def resolve_target(
        scene: SceneIndex,
        hint: Optional[str],
        action_type: Union[str, CanonicalActionType] = "click",
    ) -> Optional[SceneElement]:
        """Resolve a label hint to exactly one element, or None.

        Delegates to AliasMapper.resolve_element while maintaining backwards compatibility.
        """
        if not hint or not str(hint).strip():
            if action_type in ("type", CanonicalActionType.TYPE):
                el, _ = AliasMapper.resolve_element(scene, "", action_type=CanonicalActionType.TYPE)
                return el
            return None

        canon_type = CanonicalActionType.CLICK
        if isinstance(action_type, CanonicalActionType):
            canon_type = action_type
        elif isinstance(action_type, str):
            norm = re.sub(r"[\s\-_]+", "_", action_type.strip().lower())
            canon_type = ACTION_ALIAS_MAP.get(norm, CanonicalActionType.CLICK)

        el, _ = AliasMapper.resolve_element(scene, hint, action_type=canon_type)
        return el

    # --------------------------------------------------------------- main entry

    def execute_subgoal(
        self,
        step_index: int,
        sub_goal: str,
        target_label_hint: str = "",
        action_type: str = "click",
        text_to_type: str = "",
        expected_outcome: str = "",
        button: Literal["left", "right", "double"] = "left",
        clear_first: bool = False,
        press_enter: bool = False,
        keys: Optional[Union[List[str], str]] = None,
        direction: Literal["up", "down"] = "down",
        scroll_amount: int = 3,
        duration_seconds: float = 0.0,
        **kwargs: Any,
    ) -> ExecutionResult:
        """Execute a sub-goal through the closed-loop recovery ladder."""
        t_start = time.perf_counter()

        # Parameter synonym resolution from kwargs
        if "amount" in kwargs and scroll_amount == 3:
            try:
                scroll_amount = int(kwargs["amount"])
            except (ValueError, TypeError):
                pass
        if "scroll_direction" in kwargs and direction == "down":
            direction = kwargs["scroll_direction"]
        if "seconds" in kwargs and duration_seconds == 0.0:
            try:
                duration_seconds = float(kwargs["seconds"])
            except (ValueError, TypeError):
                pass

        # Normalize action_type
        raw_action = action_type.value if hasattr(action_type, "value") else str(action_type)
        norm_key = re.sub(r"[\s\-_]+", "_", raw_action.strip().lower())
        canon = ACTION_ALIAS_MAP.get(norm_key)
        normalized_action = canon.value if canon is not None else norm_key

        # Button resolution
        act_button = button
        if normalized_action == "double_click":
            act_button = "double"
        elif normalized_action == "right_click":
            act_button = "right"

        tool_desc = f"{normalized_action}('{target_label_hint}')"

        if normalized_action not in SUPPORTED_ACTIONS:
            return self._fail(
                SceneIndex(hwnd=self.hwnd, window_title="", source="uia"),
                step_index, sub_goal, "none", tool_desc,
                f"Unsupported action_type '{action_type}'. Supported: {', '.join(SUPPORTED_ACTIONS)}.",
            )

        # Action-specific parameter validation
        if normalized_action == "type" and not text_to_type:
            return self._fail(
                SceneIndex(hwnd=self.hwnd, window_title="", source="uia"),
                step_index, sub_goal, "none", tool_desc,
                "action_type 'type' requires non-empty text_to_type.",
            )

        normalized_keys: List[str] = []
        if normalized_action == "hotkey":
            if not keys:
                return self._fail(
                    SceneIndex(hwnd=self.hwnd, window_title="", source="uia"),
                    step_index, sub_goal, "none", tool_desc,
                    "action_type 'hotkey' requires non-empty keys.",
                )
            if isinstance(keys, str):
                normalized_keys = [k.strip() for k in re.split(r"[\+\s,\-]+", keys) if k.strip()]
            else:
                normalized_keys = list(keys)
            try:
                normalized_keys = normalize_keys(normalized_keys)
            except Exception as e:
                return self._fail(
                    SceneIndex(hwnd=self.hwnd, window_title="", source="uia"),
                    step_index, sub_goal, "none", tool_desc,
                    f"Invalid hotkey specification: {e}",
                )

        if normalized_action == "wait" and duration_seconds < 0:
            return self._fail(
                SceneIndex(hwnd=self.hwnd, window_title="", source="uia"),
                step_index, sub_goal, "none", tool_desc,
                "action_type 'wait' requires non-negative duration_seconds.",
            )

        try:
            check_target_allowed(self.hwnd, self.mode)
        except SandboxViolationException as e:
            return self._fail(
                SceneIndex(hwnd=self.hwnd, window_title="", source="uia"),
                step_index, sub_goal, "none", tool_desc, str(e), escalate=False,
            )

        canon_type = canon or CanonicalActionType.CLICK

        # Non-targeted actions: wait, or hotkey without target hint
        requires_target = normalized_action in ("click", "double_click", "right_click", "invoke", "scroll", "type")
        if not requires_target or (normalized_action == "hotkey" and not target_label_hint):
            uia_scene = self.uia_sensor.sense()
            res = self._run_reflex_pass(
                scene=uia_scene,
                target_el=None,
                step_index=step_index,
                sub_goal=sub_goal,
                action_type=normalized_action,
                text_to_type=text_to_type,
                expected_outcome=expected_outcome,
                perception_source="uia",
                sensor_fn=self.uia_sensor.sense,
                button=act_button,
                clear_first=clear_first,
                press_enter=press_enter,
                keys=normalized_keys,
                direction=direction,
                scroll_amount=scroll_amount,
                duration_seconds=duration_seconds,
            )
            res.attempts_used = 1
            return self._finalize(res, t_start)

        # ATTEMPT 1: UIA
        tr = self.tracer
        t_sense = time.perf_counter()
        uia_scene = self.uia_sensor.sense()
        uia_diag: Optional[str] = None
        if uia_scene.sense_error:
            tr.emit("perception", "UIA sense failed", uia_scene.sense_error, tier="uia", status="fail",
                    duration_ms=int((time.perf_counter() - t_sense) * 1000))
        if not uia_scene.sense_error:
            target, uia_diag = AliasMapper.resolve_element(uia_scene, target_label_hint, action_type=canon_type)
            tr.emit(
                "perception",
                f"UIA · {len(uia_scene.elements)} controls in “{uia_scene.window_title}”",
                (f"Resolved “{target_label_hint}” → #{target.element_id} {target.role} “{target.label}”"
                 if target is not None else (uia_diag or "no match")),
                tier="uia",
                status="ok" if target is not None else "warn",
                confidence=1.0 if target is not None else 0.0,
                duration_ms=int((time.perf_counter() - t_sense) * 1000),
                labels=[f"{e.role}: {e.label}" for e in list(uia_scene.elements.values())[:80]],
            )
            if target is not None:
                res = self._run_reflex_pass(
                    scene=uia_scene,
                    target_el=target,
                    step_index=step_index,
                    sub_goal=sub_goal,
                    action_type=normalized_action,
                    text_to_type=text_to_type,
                    expected_outcome=expected_outcome,
                    perception_source="uia",
                    sensor_fn=self.uia_sensor.sense,
                    button=act_button,
                    clear_first=clear_first,
                    press_enter=press_enter,
                    keys=normalized_keys,
                    direction=direction,
                    scroll_amount=scroll_amount,
                    duration_seconds=duration_seconds,
                )
                res.attempts_used = 1
                return self._finalize(res, t_start)

        # ATTEMPT 2: Mirage visual fallback. A perception miss goes SIDEWAYS to vision,
        # not up to the LLM. Only reached when nothing has been actuated.
        tr.emit("escalate", "Perception miss → Mirage",
                "Target is not in the accessibility tree; looking at the pixels instead.",
                tier="mirage", status="warn")
        mirage_sense = lambda: self.mirage_sensor.sense(window_title=uia_scene.window_title)  # noqa: E731
        t_m = time.perf_counter()
        mirage_scene = mirage_sense()
        mirage_diag: Optional[str] = None
        if not mirage_scene.sense_error:
            target, mirage_diag = AliasMapper.resolve_element(mirage_scene, target_label_hint, action_type=canon_type)
            score = 1.0 if target is not None else 0.0
            ranked: list = []
            if target is None and target_label_hint:
                from servo.perception.mirage_sensor import match_target
                best, score, ranked = match_target(self.mirage_sensor.last_detections, target_label_hint)
                if best is not None and score >= settings.mirage_match_threshold:
                    idx = self.mirage_sensor.last_detections.index(best)
                    target = mirage_scene.elements.get(idx)
                    mirage_diag = None
            tr.emit(
                "perception",
                f"Mirage · {len(mirage_scene.elements)} detections",
                (f"Matched “{target_label_hint}” → “{target.label}” at {target.center_screen}"
                 if target is not None else (mirage_diag or "no confident match")),
                tier="mirage", status="ok" if target is not None else "fail",
                confidence=score, duration_ms=int((time.perf_counter() - t_m) * 1000),
                ranked=ranked,
            )
            if target is not None:
                res = self._run_reflex_pass(
                    scene=mirage_scene,
                    target_el=target,
                    step_index=step_index,
                    sub_goal=sub_goal,
                    action_type=normalized_action,
                    text_to_type=text_to_type,
                    expected_outcome=expected_outcome,
                    perception_source="mirage",
                    sensor_fn=mirage_sense,
                    button=act_button,
                    clear_first=clear_first,
                    press_enter=press_enter,
                    keys=normalized_keys,
                    direction=direction,
                    scroll_amount=scroll_amount,
                    duration_seconds=duration_seconds,
                )
                res.attempts_used = 2
                return self._finalize(res, t_start)

        if mirage_scene.sense_error:
            tr.emit("perception", "Mirage unavailable", mirage_scene.sense_error, tier="mirage", status="fail",
                    duration_ms=int((time.perf_counter() - t_m) * 1000))
        reasons = []
        if uia_scene.sense_error:
            reasons.append(f"UIA failed: {uia_scene.sense_error}")
        else:
            reasons.append(uia_diag or f"UIA found no unique element matching '{target_label_hint}'")
        if mirage_scene.sense_error:
            reasons.append(f"Mirage failed: {mirage_scene.sense_error}")
        else:
            reasons.append(mirage_diag or f"Mirage found no unique element matching '{target_label_hint}'")

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
        target_el: Optional[SceneElement],
        step_index: int,
        sub_goal: str,
        action_type: str,
        text_to_type: str,
        expected_outcome: str,
        perception_source: str,
        sensor_fn: Callable[[], SceneIndex],
        button: str = "left",
        clear_first: bool = False,
        press_enter: bool = False,
        keys: Optional[List[str]] = None,
        direction: str = "down",
        scroll_amount: int = 3,
        duration_seconds: float = 0.0,
    ) -> ExecutionResult:
        """Single pass: pre-risk gate, actuation, settling, and post-verify."""
        eid = target_el.element_id if target_el is not None else 0
        if action_type == "type":
            tool_call_repr = f"type_text(element_id={eid}, text={text_to_type!r})"
        elif action_type in ("click", "double_click", "right_click"):
            if button != "left":
                tool_call_repr = f"click(element_id={eid}, button={button!r})"
            else:
                tool_call_repr = f"click(element_id={eid})"
        elif action_type == "invoke":
            tool_call_repr = f"invoke(element_id={eid})"
        elif action_type == "hotkey":
            tool_call_repr = f"hotkey(keys={keys})"
        elif action_type == "scroll":
            tool_call_repr = f"scroll(element_id={eid}, direction={direction!r}, amount={scroll_amount})"
        elif action_type == "wait":
            tool_call_repr = f"wait(duration_seconds={duration_seconds})"
        else:
            tool_call_repr = f"{action_type}()"

        element_label = target_el.label if target_el is not None else scene.window_title

        # 1. Jev Pre-Action Risk Gate
        try:
            target_process = windows.get_process_name(scene.hwnd or self.hwnd)
        except Exception:
            target_process = ""
        pre_risk = self.jev.evaluate_pre_risk(sub_goal, tool_call_repr, element_label, target_process)
        self.tracer.emit(
            "decision", f"Kev pre-check · {pre_risk.risk_level} risk", pre_risk.reason, tier="kev",
            status="ok" if pre_risk.risk_level == "low" and pre_risk.scope_valid else "warn",
            source=pre_risk.source, scope_valid=pre_risk.scope_valid, probabilities=pre_risk.probabilities,
            tool_call=tool_call_repr,
        )

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
                    element_label=element_label,
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
            self.tracer.emit("decision", f"Your confirmation · {confirmation}", tool_call_repr, tier="kev",
                             status="ok" if approved else "fail")
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
        self.tracer.emit(
            "action", tool_call_repr,
            (f"{perception_source.upper()} target “{element_label}” at {target_el.center_screen}"
             + (" — cursor follows an eased path" if action_type in ("click", "double_click", "right_click") else "")
             if target_el is not None else f"in “{scene.window_title}”"),
            tier="mirage" if perception_source == "mirage" else "uia",
        )
        try:
            if action_type == "type":
                if target_el is None:
                    raise ActuationError("Action 'type' requires a target element.")
                self.tools.type_text(
                    scene, target_el.element_id, text_to_type,
                    press_enter=press_enter, clear_first=clear_first,
                )
            elif action_type in ("click", "double_click", "right_click"):
                if target_el is None:
                    raise ActuationError(f"Action '{action_type}' requires a target element.")
                self.tools.click(scene, target_el.element_id, button=button)
            elif action_type == "invoke":
                if target_el is None:
                    raise ActuationError("Action 'invoke' requires a target element.")
                self.tools.invoke(scene, target_el.element_id)
            elif action_type == "hotkey":
                if not keys:
                    raise ActuationError("Action 'hotkey' requires non-empty keys.")
                self.tools.hotkey(keys)
            elif action_type == "scroll":
                if target_el is None:
                    raise ActuationError("Action 'scroll' requires a target element.")
                self.tools.scroll(scene, target_el.element_id, direction=direction, amount=scroll_amount)
            elif action_type == "wait":
                self.tools.wait(duration_seconds)
            else:
                raise ActuationError(f"Unsupported action_type '{action_type}'.")
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
        if action_type == "wait":
            post_eval = PostTransitionResult(
                transition_ok=True,
                confidence=1.0,
                explanation=f"Wait of {duration_seconds}s completed.",
            )
        else:
            post_eval = self.jev.evaluate_post_transition(
                sub_goal, delta, expected_outcome, typed_text=text_to_type if action_type == "type" else "",
            )

        self.tracer.emit(
            "verify",
            "Kev post-check · " + ("change confirmed" if post_eval.transition_ok else "expected change not seen"),
            post_eval.explanation, tier="kev", status="ok" if post_eval.transition_ok else "fail",
            confidence=post_eval.confidence, delta=delta.model_dump(),
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
