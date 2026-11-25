"""The closed loop for one step: find the target, gate, act once, verify.

Finding the target: UIA; if UIA cannot find it, a Windows OCR text match; then the
Qwen3.5-4B vision locator; then the step fails with the reasons. Actuate at most ONCE per
step. If the effect cannot be verified, the step reports that instead of acting again: a
second click or a second burst of typing is a different action from the one that was
gated and approved.
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
    split_hotkeys,
)
from servo.config import settings
from servo.control.confirmation import ConfirmationCallback, ConfirmationRequest
from servo.control.jev_gate import JevGate, LayaUnavailable, PostTransitionResult, _post
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
    # State-setting actions (servo.actuation.patterns): verified by reading the control back.
    "set_toggle",
    "set_expanded",
    "select_option",
    "set_value",
)
PATTERN_ACTIONS = ("set_toggle", "set_expanded", "select_option", "set_value")

_ITEM_ROLES = {"dataitem", "listitem", "treeitem", "tabitem", "custom", "group", "pane"}


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
    # confirmed | unconfirmed | failed (see PostTransitionResult.verdict)
    verification: str = "failed"
    note: Optional[str] = None  # why an unconfirmed step could not be confirmed


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

        state = str(kwargs.get("state") or "").lower()
        clearing = normalized_action == "set_value" and (
            kwargs.get("value") == "" or kwargs.get("clearing") or "clear" in sub_goal.lower() or text_to_type == ""
        )
        if normalized_action == "select_option" and not str(text_to_type).strip():
            return self._fail(
                SceneIndex(hwnd=self.hwnd, window_title="", source="uia"),
                step_index, sub_goal, "none", tool_desc,
                f"action_type '{normalized_action}' needs the option to select.",
            )
        if normalized_action == "set_value" and not str(text_to_type).strip() and not clearing:
            return self._fail(
                SceneIndex(hwnd=self.hwnd, window_title="", source="uia"),
                step_index, sub_goal, "none", tool_desc,
                f"action_type '{normalized_action}' needs the value to set.",
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
                normalized_keys = split_hotkeys(keys)
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
        requires_target = normalized_action in ("click", "double_click", "right_click", "invoke", "type")             + PATTERN_ACTIONS or (normalized_action == "scroll" and bool(target_label_hint))
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
                state=state,
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
            if target is None and not str(uia_diag or "").lower().startswith("ambiguous"):
                target, uia_scene = self._scroll_to_find(uia_scene, target_label_hint, canon_type, target)
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
                    state=state,
                )
                res.attempts_used = 1
                return self._finalize(res, t_start)

        # ATTEMPT 2: the target is not in the accessibility tree. A perception miss goes
        # SIDEWAYS to the screen, not up to the LLM: first the text on screen (Windows OCR,
        # ~0.1 s), then the vision model (Qwen3.5-4B, ~5 s) for icons and unlabelled
        # controls. Only reached when nothing has been actuated.
        located: Optional[SceneElement] = None
        visual_scene = uia_scene
        reasons = [f"UIA failed: {uia_scene.sense_error}" if uia_scene.sense_error
                   else (uia_diag or f"UIA found no unique element matching '{target_label_hint}'")]
        img = self.mirage_sensor.capture_window_image()
        b = self.mirage_sensor.viewport.bounds
        source = "ocr"

        def _at(box: Tuple[int, int, int, int], label: str, role: str) -> SceneElement:
            sb = (b.left + box[0], b.top + box[1], b.left + box[2], b.top + box[3])
            return SceneElement(
                element_id=max(uia_scene.elements, default=-1) + 1, label=label, role=role,
                bbox_screen=sb, bbox_norm=self.mirage_sensor.viewport.bbox_screen_to_norm(sb),
                center_screen=((sb[0] + sb[2]) // 2, (sb[1] + sb[3]) // 2))

        if img is None:
            reasons.append("could not capture the window")
        elif not target_label_hint:
            reasons.append("no target description to look for on screen")
        else:
            from servo.perception.mirage_sensor import match_target
            from servo.perception.ocr_sensor import OcrUnavailable, read_lines
            tr.emit("escalate", "Perception miss → text on screen",
                    "Target is not in the accessibility tree; reading the window with Windows OCR.",
                    tier="ocr", status="warn")
            try:
                lines, ms = read_lines(img)
                best, score, ranked = match_target(lines, target_label_hint)
                if best is not None and score >= settings.text_match_threshold:
                    located = _at(best.bbox, best.label, "TextOnScreen")
                    tr.emit("perception", f"OCR · “{target_label_hint}” → “{best.label}”", "", tier="ocr",
                            status="ok", confidence=score, ranked=ranked, duration_ms=ms)
                else:
                    reasons.append(f"OCR read {len(lines)} lines; none matches '{target_label_hint}'")
                    tr.emit("perception", "OCR · no matching text", f"{len(lines)} lines read", tier="ocr",
                            status="warn", ranked=ranked, duration_ms=ms)
            except OcrUnavailable as e:
                reasons.append(str(e))
                tr.emit("perception", "OCR failed", str(e), tier="ocr", status="fail")

            if located is None:
                from servo.perception.locator import LocatorUnavailable, locate
                source = "locator"
                tr.emit("escalate", "Text not found → vision locator",
                        f"Asking the vision model where “{target_label_hint}” is.", tier="locator", status="warn")
                t_v = time.perf_counter()
                try:
                    x, y, ms = locate(img, target_label_hint)
                    located = _at((x - 4, y - 4, x + 4, y + 4), target_label_hint, "VisualControl")
                    tr.emit("perception", f"Vision · “{target_label_hint}” at {located.center_screen}", "",
                            tier="locator", status="ok", duration_ms=ms)
                except LocatorUnavailable as e:
                    reasons.append(str(e))
                    tr.emit("perception", "Vision locator failed", str(e), tier="locator", status="fail",
                            duration_ms=int((time.perf_counter() - t_v) * 1000))
        if located is not None:
            visual_scene = uia_scene.model_copy(update={"elements": {**uia_scene.elements,
                                                                     located.element_id: located}})
            res = self._run_reflex_pass(
                scene=visual_scene,
                target_el=located,
                step_index=step_index,
                sub_goal=sub_goal,
                action_type=normalized_action,
                text_to_type=text_to_type,
                expected_outcome=expected_outcome,
                perception_source=source,
                sensor_fn=self.uia_sensor.sense,
                button=act_button,
                clear_first=clear_first,
                press_enter=press_enter,
                keys=normalized_keys,
                direction=direction,
                scroll_amount=scroll_amount,
                duration_seconds=duration_seconds,
                state=state,
            )
            res.attempts_used = 2
            return self._finalize(res, t_start)

        res = self._fail(uia_scene, step_index, sub_goal, "hybrid", tool_desc, "; ".join(reasons))
        res.attempts_used = 2
        return self._finalize(res, t_start)

    def _stateful_click_verdict(self, scene: SceneIndex, el: SceneElement) -> Optional[Tuple[str, str]]:
        """A click on something with an on/off state is judged by that state.

        Clicking the "File name extensions" menu item closed the menu, which counted as
        "UI changed → confirmed", while the setting itself never changed. For a control
        that reports a toggle state, the click must flip it.
        """
        if el.toggle not in ("on", "off"):
            return None
        # Only for controls that ARE a setting. A list or data item can expose a toggle
        # state too (the Clock's timer cards do), but clicking one opens it: the screen
        # change decides that, not the toggle.
        if el.role.lower().replace(" ", "") in _ITEM_ROLES:
            return None
        from servo.actuation import patterns
        try:
            w = patterns.wrapper_for(scene, el)
        except Exception:
            return None
        want = "off" if el.toggle == "on" else "on"
        if patterns._wait_until(lambda: patterns._toggle_state(w) == want, timeout=1.0):
            return "confirmed", f"“{el.label}” went from {el.toggle} to {want}"
        after = patterns._toggle_state(w)
        if after is None:
            # The control is gone (its menu closed), so the new state can't be read. A
            # changed screen is NOT evidence the setting flipped: say so honestly.
            return "unconfirmed", (f"Clicked “{el.label}” (it was {el.toggle}); it closed before its new "
                                   f"state could be read. Use check/uncheck <label> to set it verifiably.")
        return "failed", f"Clicked “{el.label}” but it is still {after}"

    def _scroll_to_find(self, scene: SceneIndex, hint: str, canon_type: Any,
                        target: Optional[SceneElement], pages: int = 6) -> Tuple[Optional[SceneElement], SceneIndex]:
        """The control may simply be below the fold: scroll the page and look again.

        Only visible controls are in the scene, so on a long Settings page or web page a
        correct label used to fail as "not found" and go to the vision fallback or the LLM.
        """
        if not hint:
            return target, scene
        from servo.actuation import patterns
        for page in range(1, pages + 1):
            try:
                moved = patterns.scroll_page(scene.hwnd or self.hwnd, "down")
            except Exception:
                moved = False
            if not moved:
                break
            fresh = self.uia_sensor.sense()
            if fresh.sense_error:
                break
            scene = fresh
            found, _ = AliasMapper.resolve_element(scene, hint, action_type=canon_type)
            if found is not None:
                self.tracer.emit("perception", f"Scrolled to find “{hint}”",
                                 f"Found it after {page} page{'s' if page > 1 else ''}: {found.role} “{found.label}”",
                                 tier="uia", status="ok")
                return found, scene
        return target, scene

    def _run_pattern_action(self, scene: SceneIndex, el: SceneElement, action_type: str, state: str,
                            value: str) -> Tuple[str, str]:
        """Set a toggle/expansion/option/value and read the control back for the verdict."""
        from servo.actuation import patterns
        click = lambda: self.tools.click(scene, el.element_id)  # noqa: E731
        if action_type == "set_toggle":
            return patterns.set_toggle(scene, el, state not in ("off", "uncheck", "disable", "false"), click)
        if action_type == "set_expanded":
            return patterns.set_expanded(scene, el, state not in ("collapse", "close", "false"), click)
        if action_type == "select_option":
            return patterns.select_option(scene, el, value, click)
        try:
            return patterns.set_value(scene, el, value)
        except patterns.PatternUnavailable:
            # No settable value: type it like a person would, replacing what's there.
            self.tools.type_text(scene, el.element_id, value, clear_first=True)
            got = (self.tools.read_text(scene, el.element_id) or "").strip()
            if got or not value.strip():
                return ("confirmed" if got == value.strip() else "failed"), f"“{el.label}” = {got!r}"
            return "unconfirmed", f"Typed {value!r} into “{el.label}”; it can't be read back"

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
        state: str = "",
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
        elif action_type == "set_toggle":
            tool_call_repr = f"set_toggle(element_id={eid}, state={state or 'on'!r})"
        elif action_type == "set_expanded":
            tool_call_repr = f"set_expanded(element_id={eid}, state={state or 'expand'!r})"
        elif action_type in ("select_option", "set_value"):
            tool_call_repr = f"{action_type}(element_id={eid}, value={text_to_type!r})"
        else:
            tool_call_repr = f"{action_type}()"

        element_label = target_el.label if target_el is not None else scene.window_title

        # 1. Laya pre-action risk gate
        try:
            target_process = windows.get_process_name(scene.hwnd or self.hwnd)
        except Exception:
            target_process = ""
        try:
            pre_risk = self.jev.evaluate_pre_risk(sub_goal, tool_call_repr, element_label, target_process)
        except LayaUnavailable as e:
            self.tracer.emit("decision", "Laya pre-check failed", str(e), tier="laya", status="fail")
            return self._fail(scene, step_index, sub_goal, perception_source, tool_call_repr,
                              f"No safety check possible: {e}")
        self.tracer.emit(
            "decision", f"Laya pre-check · {pre_risk.risk_level} risk", pre_risk.reason, tier="laya",
            status="ok" if pre_risk.risk_level == "low" and pre_risk.scope_valid else "warn",
            source=pre_risk.source, scope_valid=pre_risk.scope_valid, probabilities=pre_risk.probabilities,
            tool_call=tool_call_repr,
        )

        if not pre_risk.scope_valid:
            return self._fail(
                scene, step_index, sub_goal, perception_source, tool_call_repr,
                f"Laya rejected the action as out of scope: {pre_risk.reason}", risk=pre_risk.risk_level,
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
            self.tracer.emit("decision", f"Your confirmation · {confirmation}", tool_call_repr, tier="laya",
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
        pattern_verdict: Optional[Tuple[str, str]] = None
        try:
            if action_type in PATTERN_ACTIONS:
                if target_el is None:
                    raise ActuationError(f"Action '{action_type}' requires a target element.")
                pattern_verdict = self._run_pattern_action(scene, target_el, action_type, state, text_to_type)
            elif action_type == "scroll" and target_el is None:
                from servo.actuation.patterns import scroll_page
                if not scroll_page(scene.hwnd or self.hwnd, direction):
                    raise ActuationError("Nothing here scrolls (or it is already at the end).")
            elif action_type == "type":
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

        # 5. Post-action verification (control state, read-back, or Laya)
        if pattern_verdict is None and action_type == "click" and target_el is not None:
            pattern_verdict = self._stateful_click_verdict(scene, target_el)
        if pattern_verdict is not None:
            # The control's own state after the action is the verdict (read back).
            post_eval = _post(pattern_verdict[0], 0.95 if pattern_verdict[0] == "confirmed" else 0.5,  # type: ignore[arg-type]
                              pattern_verdict[1], source="readback")
        elif action_type == "wait":
            post_eval = PostTransitionResult(
                transition_ok=True,
                confidence=1.0,
                explanation=f"Wait of {duration_seconds}s completed.",
                source="readback",
            )
        else:
            observed_text: Optional[str] = None
            if action_type == "type" and target_el is not None:
                try:
                    observed_text = self.tools.read_text(scene, target_el.element_id)
                except Exception:
                    observed_text = None
            try:
                post_eval = self.jev.evaluate_post_transition(
                    sub_goal, delta, expected_outcome, typed_text=text_to_type if action_type == "type" else "",
                    observed_text=observed_text,
                )
            except LayaUnavailable as e:
                post_eval = _post("failed", 0.0, f"Could not verify the step: {e}", source="laya")

        title = {"confirmed": "confirmed", "unconfirmed": "ran, effect not visible", "failed": "expected change not seen"}
        self.tracer.emit(
            "verify",
            "Post-check · " + title[post_eval.verdict],
            post_eval.explanation, tier="laya",
            status={"confirmed": "ok", "unconfirmed": "warn", "failed": "fail"}[post_eval.verdict],
            confidence=post_eval.confidence, delta=delta.model_dump(), verdict=post_eval.verdict,
            source=post_eval.source,
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
                verification=post_eval.verdict,
                note=post_eval.explanation if post_eval.verdict == "unconfirmed" else None,
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
