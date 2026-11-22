"""System 1 and System 2 Handoff Architecture for Servo.

See Section 5.4 in SERVO_ARCHITECTURE_V1.md and SERVO_ARCHITECTURE_V3.md.
Coordinates the high-frequency reflex arc (System 1) with the macro setpoint
planner (System 2), managing alias resolution, app lifecycle, safety gating,
settling verification, and real-time execution telemetry.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Literal, Optional, Tuple, Union
from pydantic import BaseModel

from servo.actuation.alias_mapping import (
    AliasMapper,
    CanonicalAction,
    CanonicalActionType,
)
from servo.control.confirmation import ConfirmationCallback
from servo.cognition.trace import NullTracer, Tracer
from servo.control.loop import ExecutionResult, ServoControlLoop
from servo.perception import windows
from servo.perception.scene_index import SceneIndex
from servo.perception.uia_sensor import UIASensor
from servo.planner.tool_calling import (
    MacroPlan,
    PlanStep,
    SYSTEM_PROMPT_TEMPLATE,
    build_tool_planning_prompt,
    parse_llm_tool_plan,
)

logger = logging.getLogger("servo.control.handoff")


def _to_str_or_none(val: Any) -> Optional[str]:
    if val is None or hasattr(val, "_mock_name") or hasattr(val, "return_value"):
        return None
    return str(val) if not isinstance(val, str) else val


def _fresh_notepad_tab(hwnd: int) -> bool:
    """Windows 11 Notepad reopens earlier tabs, so "open notepad and write X" used to type
    into (append to) whatever document the user had open. If the visible document has
    content, open a new tab first. Returns True if a new tab was opened."""
    if sys.platform != "win32" or windows.get_process_name(hwnd) != "notepad.exe":
        return False
    try:
        from servo.perception.uia_sensor import UIASensor
        from servo.actuation.needle_tools import NeedleToolSurface
        scene = UIASensor(hwnd).sense()
        docs = [e for e in scene.elements.values() if e.role == "Document"]
        if not docs or not (docs[0].value or "").strip():
            return False
        NeedleToolSurface(hwnd, fast_mode=True).hotkey(["ctrl", "n"])
        time.sleep(0.5)
        return True
    except Exception as e:
        logger.debug("fresh notepad tab skipped: %s", e)
        return False


class StepExecutionReport(BaseModel):
    step_index: int
    action_type: str
    target: Optional[str] = None
    description: str
    success: bool
    latency_ms: int
    error: Optional[str] = None
    attempts_used: int = 1
    perception_source: Optional[str] = None
    jev_risk: Optional[str] = None
    confirmation: Optional[str] = None
    # confirmed | unconfirmed | failed: what the post-check could actually observe.
    verification: str = "confirmed"
    note: Optional[str] = None


class PlanExecutionSummary(BaseModel):
    goal: str
    success: bool
    total_steps: int
    completed_steps: int
    total_latency_ms: int
    step_reports: List[StepExecutionReport]
    active_hwnd: Optional[int] = None
    active_title: str = ""
    aborted: bool = False


# Event callback types for streaming execution UI
OnPlanStartFn = Callable[[MacroPlan], None]
OnStepStartFn = Callable[[PlanStep], None]
OnStepProgressFn = Callable[[PlanStep, StepExecutionReport], None]
OnPlanCompleteFn = Callable[[PlanExecutionSummary], None]


class SystemHandoffCoordinator:
    """Manages the closed-loop handoff between System 2 planning and System 1 execution."""

    def __init__(
        self,
        target_hwnd: Optional[int] = None,
        fast_mode: bool = False,
        confirmation_callback: Optional[ConfirmationCallback] = None,
        should_abort: Optional[Union[Callable[[], bool], threading.Event]] = None,
        mode: Optional[Literal["assistant", "harness"]] = None,
        on_plan_start: Optional[OnPlanStartFn] = None,
        on_step_start: Optional[OnStepStartFn] = None,
        on_step_progress: Optional[OnStepProgressFn] = None,
        on_plan_complete: Optional[OnPlanCompleteFn] = None,
        tracer: Optional[Tracer] = None,
        exclude_hwnds: Optional[Callable[[], set]] = None,
    ) -> None:
        self.target_hwnd = target_hwnd
        self.tracer = tracer or NullTracer()
        # Windows that must never be adopted as the target (the copilot itself).
        # Servo's own windows are never a target. Without a list from the copilot
        # (CLI, task suite, another process) fall back to finding them by title.
        if exclude_hwnds is None:
            from servo.actuation.overlay import own_windows as exclude_hwnds
        self.exclude_hwnds = exclude_hwnds
        self.fast_mode = fast_mode
        self.confirmation_callback = confirmation_callback
        if isinstance(should_abort, threading.Event):
            self.should_abort: Optional[Callable[[], bool]] = should_abort.is_set
        else:
            self.should_abort = should_abort
        self.mode = mode
        self.on_plan_start = on_plan_start
        self.on_step_start = on_step_start
        self.on_step_progress = on_step_progress
        self.on_plan_complete = on_plan_complete

    def _emit_plan_start(self, plan: MacroPlan) -> None:
        if self.on_plan_start:
            try:
                self.on_plan_start(plan)
            except Exception as e:
                logger.warning("Error in on_plan_start callback: %s", e)

    def _emit_step_start(self, step: PlanStep) -> None:
        if self.on_step_start:
            try:
                self.on_step_start(step)
            except Exception as e:
                logger.warning("Error in on_step_start callback: %s", e)

    def _emit_step_progress(self, step: PlanStep, report: StepExecutionReport) -> None:
        if self.on_step_progress:
            try:
                self.on_step_progress(step, report)
            except Exception as e:
                logger.warning("Error in on_step_progress callback: %s", e)

    def _emit_plan_complete(self, summary: PlanExecutionSummary) -> None:
        if self.on_plan_complete:
            try:
                self.on_plan_complete(summary)
            except Exception as e:
                logger.warning("Error in on_plan_complete callback: %s", e)

    def execute_plan(
        self,
        plan: MacroPlan,
        app_launcher: Optional[Callable[[str], Tuple[Optional[int], str]]] = None,
    ) -> PlanExecutionSummary:
        """Progressively dispatch System 2 plan steps through the System 1 reflex arc."""
        t_start = time.perf_counter()
        reports: List[StepExecutionReport] = []
        overall_success = True
        plan_aborted = False

        self._emit_plan_start(plan)

        current_hwnd = self.target_hwnd
        current_title = (
            windows.get_window_title(current_hwnd)
            if (current_hwnd and windows.is_window(current_hwnd))
            else ""
        )

        for step in plan.steps:
            step_t0 = time.perf_counter()

            # 0. Check abort condition before starting step
            if self.should_abort and self.should_abort():
                logger.info("Plan execution aborted before step %s", step.step_index)
                plan_aborted = True
                overall_success = False
                rep = StepExecutionReport(
                    step_index=step.step_index,
                    action_type=step.tool_name,
                    target=step.arguments.get("target"),
                    description="Execution aborted by user or caller timeout.",
                    success=False,
                    latency_ms=int((time.perf_counter() - step_t0) * 1000),
                    error="Execution cancelled by user or caller timeout.",
                )
                reports.append(rep)
                self._emit_step_progress(step, rep)
                break

            self._emit_step_start(step)

            # Ensure we have a CanonicalAction for this step
            action = step.canonical_action
            if action is None:
                try:
                    action = AliasMapper.canonicalize_action(raw_action=step.tool_name, **step.arguments)
                    step.canonical_action = action
                except Exception as e:
                    rep = StepExecutionReport(
                        step_index=step.step_index,
                        action_type=step.tool_name,
                        target=step.arguments.get("target"),
                        description=f"Invalid action: {e}",
                        success=False,
                        latency_ms=int((time.perf_counter() - step_t0) * 1000),
                        error=str(e),
                    )
                    reports.append(rep)
                    overall_success = False
                    self._emit_step_progress(step, rep)
                    break

            # 1. Handle Application Launching / Switching
            if action.action_type == CanonicalActionType.LAUNCH:
                app_target = action.app_name or action.target or ""
                if app_launcher:
                    hwnd, title = app_launcher(app_target)
                else:
                    from servo.daemon.copilot import launch_or_focus_app
                    hwnd, title = launch_or_focus_app(app_target)

                step_latency = int((time.perf_counter() - step_t0) * 1000)
                own = set(self.exclude_hwnds() if self.exclude_hwnds else ())
                if hwnd and (hwnd in own or "servo copilot" in str(title).lower()):
                    # Never report "launched" for focusing the copilot itself.
                    title = (f"found only the Servo Copilot window, not '{app_target}'. "
                             f"'{app_target}' may not be installed under that name.")
                    hwnd = None
                if hwnd and windows.is_window(hwnd):
                    if _fresh_notepad_tab(hwnd):
                        title = windows.get_window_title(hwnd) or title
                    current_hwnd = hwnd
                    current_title = title
                    self.target_hwnd = hwnd
                    rep = StepExecutionReport(
                        step_index=step.step_index,
                        action_type=action.action_type.value,
                        target=app_target,
                        description=f"Launched/focused '{title}' (HWND {hwnd})",
                        success=True,
                        latency_ms=step_latency,
                    )
                else:
                    rep = StepExecutionReport(
                        step_index=step.step_index,
                        action_type=action.action_type.value,
                        target=app_target,
                        description=f"Failed to launch '{app_target}': {title}",
                        success=False,
                        latency_ms=step_latency,
                        error=f"Could not find or launch application '{app_target}'",
                    )
                    overall_success = False

                reports.append(rep)
                self._emit_step_progress(step, rep)
                if not rep.success:
                    break
                continue

            # 1b. System media keys (volume, play/pause): global, no target window needed.
            if action.action_type == CanonicalActionType.HOTKEY:
                from servo.actuation.needle_tools import NeedleToolSurface, media_key_vk
                if media_key_vk(list(action.keys or [])) is not None:
                    try:
                        NeedleToolSurface(current_hwnd or 0).hotkey(list(action.keys))
                        rep = StepExecutionReport(
                            step_index=step.step_index, action_type=action.action_type.value,
                            target="+".join(action.keys), description=f"Pressed {'+'.join(action.keys)} (system media key)",
                            success=True, latency_ms=int((time.perf_counter() - step_t0) * 1000),
                            perception_source="system",
                        )
                    except Exception as e:
                        rep = StepExecutionReport(
                            step_index=step.step_index, action_type=action.action_type.value,
                            target="+".join(action.keys), description="Media key failed", success=False,
                            latency_ms=int((time.perf_counter() - step_t0) * 1000), error=str(e),
                            verification="failed",
                        )
                    reports.append(rep)
                    self._emit_step_progress(step, rep)
                    if not rep.success:
                        overall_success = False
                        break
                    continue

            # 2. Handle Application Closing
            if action.action_type == CanonicalActionType.CLOSE:
                app_target = action.app_name or current_title
                close_hwnd = current_hwnd
                if action.app_name:
                    # Find, never launch: closing an app that isn't open must not open it.
                    from servo.daemon.copilot import find_app_window
                    close_hwnd = find_app_window(action.app_name)
                own = set(self.exclude_hwnds() if self.exclude_hwnds else ())
                close_title = windows.get_window_title(close_hwnd) if close_hwnd and windows.is_window(close_hwnd) else ""

                if close_hwnd and (close_hwnd in own or "servo copilot" in close_title.lower()):
                    rep = StepExecutionReport(
                        step_index=step.step_index, action_type=action.action_type.value, target=app_target,
                        description="Refused to close the Servo Copilot window.", success=False,
                        latency_ms=int((time.perf_counter() - step_t0) * 1000),
                        error="The window to close is Servo's own copilot.", verification="failed",
                    )
                elif close_hwnd and windows.is_window(close_hwnd):
                    if sys.platform == "win32":
                        import win32gui, win32con
                        win32gui.PostMessage(close_hwnd, win32con.WM_CLOSE, 0, 0)
                    gone = False
                    # Up to 4 s for the window to go away. Apps like WhatsApp and Teams hide
                    # to the tray instead of destroying the window, so invisible counts too.
                    for _ in range(80):
                        time.sleep(0.05)
                        if not windows.is_window(close_hwnd) or not windows.is_visible(close_hwnd) \
                                or not windows.get_window_title(close_hwnd):
                            gone = True
                            break
                    rep = StepExecutionReport(
                        step_index=step.step_index,
                        action_type=action.action_type.value,
                        target=app_target,
                        description=(f"Closed '{close_title}'" if gone else
                                     f"Asked '{close_title}' to close; it is still open (it may be asking to save)"),
                        success=True,
                        latency_ms=int((time.perf_counter() - step_t0) * 1000),
                        verification="confirmed" if gone else "unconfirmed",
                        note=None if gone else "Window still open after the close request; check for a save prompt.",
                    )
                    if gone and close_hwnd == current_hwnd:
                        current_hwnd = None
                        current_title = ""
                        self.target_hwnd = None
                else:
                    rep = StepExecutionReport(
                        step_index=step.step_index,
                        action_type=action.action_type.value,
                        target=app_target,
                        description=f"Nothing to close: no open window for '{app_target}'.",
                        success=True,
                        latency_ms=int((time.perf_counter() - step_t0) * 1000),
                        verification="unconfirmed",
                        note=f"No open window matched '{app_target}', so nothing was closed.",
                    )
                reports.append(rep)
                self._emit_step_progress(step, rep)
                continue

            # 3. Handle System 1 Control Loop (Clicks, Typing, Hotkeys, Invoke, Scroll, Wait)
            # Ensure target window exists
            if not current_hwnd or not windows.is_window(current_hwnd):
                fg = windows.get_foreground_window()
                excluded = self.exclude_hwnds() if self.exclude_hwnds else set()
                if fg and windows.is_window(fg) and fg not in excluded:
                    current_hwnd = fg
                    current_title = windows.get_window_title(fg)
                    self.target_hwnd = fg
                elif action.action_type == CanonicalActionType.WAIT:
                    # WAIT fallback when no window is active
                    wait_sec = action.duration_seconds or 0.5
                    slice_sec = 0.05
                    elapsed = 0.0
                    while elapsed < wait_sec:
                        if self.should_abort and self.should_abort():
                            break
                        chunk = min(slice_sec, wait_sec - elapsed)
                        time.sleep(chunk)
                        elapsed += chunk

                    aborted = bool(self.should_abort and self.should_abort())
                    rep = StepExecutionReport(
                        step_index=step.step_index,
                        action_type=action.action_type.value,
                        target=None,
                        description="Execution aborted during wait" if aborted else f"Paused for {wait_sec}s",
                        success=not aborted,
                        latency_ms=int((time.perf_counter() - step_t0) * 1000),
                        error="Execution cancelled by user or caller timeout." if aborted else None,
                    )
                    reports.append(rep)
                    self._emit_step_progress(step, rep)
                    if aborted:
                        plan_aborted = True
                        overall_success = False
                        break
                    continue
                else:
                    rep = StepExecutionReport(
                        step_index=step.step_index,
                        action_type=action.action_type.value,
                        target=action.target,
                        description="No interactive target window active.",
                        success=False,
                        latency_ms=int((time.perf_counter() - step_t0) * 1000),
                        error="No active target window available for actuation.",
                    )
                    reports.append(rep)
                    overall_success = False
                    self._emit_step_progress(step, rep)
                    break

            # Bring target window to foreground
            if sys.platform == "win32" and current_hwnd:
                import ctypes
                ctypes.windll.user32.SetForegroundWindow(current_hwnd)
                time.sleep(0.1)

            loop = ServoControlLoop(
                hwnd=current_hwnd,
                fast_mode=self.fast_mode,
                confirmation_callback=self.confirmation_callback,
                should_abort=self.should_abort,
                mode=self.mode,
                tracer=self.tracer,
            )

            # Action routing through loop.execute_subgoal
            if action.action_type in (
                CanonicalActionType.CLICK,
                CanonicalActionType.DOUBLE_CLICK,
                CanonicalActionType.RIGHT_CLICK,
            ):
                btn = action.button
                if action.action_type == CanonicalActionType.DOUBLE_CLICK:
                    btn = "double"
                elif action.action_type == CanonicalActionType.RIGHT_CLICK:
                    btn = "right"

                act_verb = action.action_type.value.replace("_", " ").capitalize()
                res = loop.execute_subgoal(
                    step_index=step.step_index,
                    sub_goal=step.rationale or f"{act_verb} '{action.target}'",
                    target_label_hint=action.target or "",
                    action_type=action.action_type.value,
                    button=btn,
                    expected_outcome=f"{act_verb} {action.target}",
                )

            elif action.action_type == CanonicalActionType.TYPE:
                res = loop.execute_subgoal(
                    step_index=step.step_index,
                    sub_goal=step.rationale or f"Type '{action.text}' into '{action.target}'",
                    target_label_hint=action.target or "editor",
                    action_type="type",
                    text_to_type=action.text,
                    clear_first=action.clear_first,
                    press_enter=action.press_enter,
                    expected_outcome="Typed text",
                )

            elif action.action_type == CanonicalActionType.HOTKEY:
                keys_str = "+".join(action.keys)
                res = loop.execute_subgoal(
                    step_index=step.step_index,
                    sub_goal=step.rationale or f"Hotkey {keys_str}",
                    target_label_hint=action.target or "",
                    action_type="hotkey",
                    keys=action.keys,
                    expected_outcome=f"Dispatched hotkey {keys_str}",
                )

            elif action.action_type == CanonicalActionType.INVOKE:
                res = loop.execute_subgoal(
                    step_index=step.step_index,
                    sub_goal=step.rationale or f"Invoke '{action.target}'",
                    target_label_hint=action.target or "",
                    action_type="invoke",
                    expected_outcome=f"Invoked {action.target}",
                )

            elif action.action_type == CanonicalActionType.SCROLL:
                direction = step.arguments.get("direction", "down")
                amount = int(step.arguments.get("amount", 3))
                res = loop.execute_subgoal(
                    step_index=step.step_index,
                    sub_goal=step.rationale or f"Scroll {direction} on '{action.target}'",
                    target_label_hint=action.target or "",
                    action_type="scroll",
                    direction=direction,
                    amount=amount,
                    scroll_amount=amount,
                    expected_outcome=f"Scrolled {direction}",
                )

            elif action.action_type == CanonicalActionType.WAIT:
                wait_sec = action.duration_seconds or 0.5
                res = loop.execute_subgoal(
                    step_index=step.step_index,
                    sub_goal=step.rationale or f"Wait {wait_sec}s",
                    target_label_hint="",
                    action_type="wait",
                    duration_seconds=wait_sec,
                    expected_outcome=f"Waited {wait_sec}s",
                )

            else:
                res = loop._fail(
                    loop.uia_sensor.sense(),
                    step.step_index,
                    "unknown",
                    "none",
                    step.tool_name,
                    f"Unsupported action type: {action.action_type}",
                )

            step_log = getattr(res, "step_log", None)
            log_latency = getattr(step_log, "latency_ms", None) if step_log else None
            step_latency = log_latency if isinstance(log_latency, int) else int((time.perf_counter() - step_t0) * 1000)

            raw_err = getattr(step_log, "error", None) if step_log else None
            err_str = _to_str_or_none(raw_err)
            is_aborted = (
                bool(err_str and "cancelled" in err_str.lower())
                or bool(self.should_abort and self.should_abort())
            )
            if is_aborted:
                plan_aborted = True

            attempts = getattr(res, "attempts_used", 1)
            attempts_int = attempts if isinstance(attempts, int) else 1

            rep = StepExecutionReport(
                step_index=step.step_index,
                action_type=action.action_type.value,
                target=action.target,
                description=step.rationale or action.description,
                success=bool(res.success),
                latency_ms=step_latency,
                error=err_str if not res.success else None,
                attempts_used=attempts_int,
                perception_source=_to_str_or_none(getattr(step_log, "perception_source", None)),
                jev_risk=_to_str_or_none(getattr(step_log, "jev_pre_risk", None)),
                confirmation=_to_str_or_none(getattr(step_log, "confirmation", None)),
                verification=(_to_str_or_none(getattr(step_log, "verification", None)) or
                              ("confirmed" if res.success else "failed")),
                note=_to_str_or_none(getattr(step_log, "note", None)),
            )
            reports.append(rep)
            self._emit_step_progress(step, rep)

            if not res.success:
                overall_success = False
                break

        total_latency = int((time.perf_counter() - t_start) * 1000)
        summary = PlanExecutionSummary(
            goal=plan.goal,
            success=overall_success,
            total_steps=len(plan.steps),
            completed_steps=sum(1 for r in reports if r.success),
            total_latency_ms=total_latency,
            step_reports=reports,
            active_hwnd=current_hwnd,
            active_title=current_title,
            aborted=plan_aborted,
        )

        self._emit_plan_complete(summary)
        return summary
