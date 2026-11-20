"""System 1 and System 2 Handoff Architecture for Servo.

See Section 5.4 in SERVO_ARCHITECTURE_V1.md and SERVO_ARCHITECTURE_V3.md.
Coordinates the high-frequency reflex arc (System 1) with the macro setpoint
planner (System 2), managing alias resolution, app lifecycle, safety gating,
settling verification, and real-time execution telemetry.
"""

from __future__ import annotations

import logging
import sys
import time
from typing import Any, Callable, Dict, List, Optional, Tuple
from pydantic import BaseModel

from servo.actuation.alias_mapping import (
    AliasMapper,
    CanonicalAction,
    CanonicalActionType,
)
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


class StepExecutionReport(BaseModel):
    step_index: int
    action_type: str
    target: Optional[str]
    description: str
    success: bool
    latency_ms: int
    error: Optional[str] = None
    attempts_used: int = 1


class PlanExecutionSummary(BaseModel):
    goal: str
    success: bool
    total_steps: int
    completed_steps: int
    total_latency_ms: int
    step_reports: List[StepExecutionReport]
    active_hwnd: Optional[int] = None
    active_title: str = ""


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
        on_plan_start: Optional[OnPlanStartFn] = None,
        on_step_start: Optional[OnStepStartFn] = None,
        on_step_progress: Optional[OnStepProgressFn] = None,
        on_plan_complete: Optional[OnPlanCompleteFn] = None,
    ) -> None:
        self.target_hwnd = target_hwnd
        self.fast_mode = fast_mode
        self.on_plan_start = on_plan_start
        self.on_step_start = on_step_start
        self.on_step_progress = on_step_progress
        self.on_plan_complete = on_plan_complete

    def execute_plan(self, plan: MacroPlan, app_launcher: Optional[Callable[[str], Tuple[Optional[int], str]]] = None) -> PlanExecutionSummary:
        """Progressively dispatch System 2 plan steps through the System 1 reflex arc."""
        t_start = time.perf_counter()
        reports: List[StepExecutionReport] = []
        overall_success = True

        if self.on_plan_start:
            try:
                self.on_plan_start(plan)
            except Exception as e:
                logger.warning("Error in on_plan_start callback: %s", e)

        current_hwnd = self.target_hwnd
        current_title = windows.get_window_title(current_hwnd) if (current_hwnd and windows.is_window(current_hwnd)) else ""

        for step in plan.steps:
            step_t0 = time.perf_counter()
            if self.on_step_start:
                try:
                    self.on_step_start(step)
                except Exception as e:
                    logger.warning("Error in on_step_start callback: %s", e)

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
                    if self.on_step_progress:
                        self.on_step_progress(step, rep)
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
                if hwnd and windows.is_window(hwnd):
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
                if self.on_step_progress:
                    self.on_step_progress(step, rep)
                if not rep.success:
                    break
                continue

            # 2. Handle Application Closing
            if action.action_type == CanonicalActionType.CLOSE:
                app_target = action.app_name or current_title
                close_hwnd = current_hwnd
                if action.app_name:
                    from servo.daemon.copilot import launch_or_focus_app
                    h, _ = launch_or_focus_app(action.app_name)
                    if h:
                        close_hwnd = h

                step_latency = int((time.perf_counter() - step_t0) * 1000)
                if close_hwnd and windows.is_window(close_hwnd):
                    if sys.platform == "win32":
                        import win32gui, win32con
                        win32gui.PostMessage(close_hwnd, win32con.WM_CLOSE, 0, 0)
                    rep = StepExecutionReport(
                        step_index=step.step_index,
                        action_type=action.action_type.value,
                        target=app_target,
                        description=f"Closed window '{current_title}'",
                        success=True,
                        latency_ms=step_latency,
                    )
                    if close_hwnd == current_hwnd:
                        current_hwnd = None
                        current_title = ""
                        self.target_hwnd = None
                else:
                    rep = StepExecutionReport(
                        step_index=step.step_index,
                        action_type=action.action_type.value,
                        target=app_target,
                        description="No window found to close.",
                        success=True,
                        latency_ms=step_latency,
                    )
                reports.append(rep)
                if self.on_step_progress:
                    self.on_step_progress(step, rep)
                continue

            # 3. Handle Explicit Wait
            if action.action_type == CanonicalActionType.WAIT:
                wait_sec = action.duration_seconds or 0.5
                time.sleep(wait_sec)
                step_latency = int((time.perf_counter() - step_t0) * 1000)
                rep = StepExecutionReport(
                    step_index=step.step_index,
                    action_type=action.action_type.value,
                    target=None,
                    description=f"Paused for {wait_sec}s",
                    success=True,
                    latency_ms=step_latency,
                )
                reports.append(rep)
                if self.on_step_progress:
                    self.on_step_progress(step, rep)
                continue

            # 4. Handle System 1 Control Loop (Clicks, Typing, Hotkeys)
            # Ensure target window exists
            if not current_hwnd or not windows.is_window(current_hwnd):
                # Fallback to foreground window
                fg = windows.get_foreground_window()
                if fg and windows.is_window(fg):
                    current_hwnd = fg
                    current_title = windows.get_window_title(fg)
                    self.target_hwnd = fg
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
                    if self.on_step_progress:
                        self.on_step_progress(step, rep)
                    break

            # Bring target window to foreground
            if sys.platform == "win32" and current_hwnd:
                import ctypes
                ctypes.windll.user32.SetForegroundWindow(current_hwnd)
                time.sleep(0.1)

            loop = ServoControlLoop(hwnd=current_hwnd, fast_mode=self.fast_mode)

            if action.action_type in (CanonicalActionType.CLICK, CanonicalActionType.DOUBLE_CLICK, CanonicalActionType.RIGHT_CLICK):
                res = loop.execute_subgoal(
                    step_index=step.step_index,
                    sub_goal=step.rationale or f"Click '{action.target}'",
                    target_label_hint=action.target or "",
                    action_type="click",
                    expected_outcome=f"Clicked {action.target}",
                )
            elif action.action_type == CanonicalActionType.TYPE:
                res = loop.execute_subgoal(
                    step_index=step.step_index,
                    sub_goal=step.rationale or f"Type '{action.text}' into '{action.target}'",
                    target_label_hint=action.target or "editor",
                    action_type="type",
                    text_to_type=action.text,
                    expected_outcome=f"Typed text",
                )
            elif action.action_type == CanonicalActionType.HOTKEY:
                # Hotkey execution via needle tools
                try:
                    tool_res = loop.tools.hotkey(action.keys)
                    res = ExecutionResult(
                        success=True,
                        attempts_used=1,
                        final_scene=loop.uia_sensor.sense(),
                        step_log=loop._fail(
                            loop.uia_sensor.sense(), step.step_index, "hotkey", "uia",
                            f"hotkey({action.keys})", ""
                        ).step_log,
                    )
                    res.step_log.error = None
                    res.step_log.latency_ms = int((time.perf_counter() - step_t0) * 1000)
                except Exception as e:
                    res = loop._fail(
                        loop.uia_sensor.sense(), step.step_index, "hotkey", "uia",
                        f"hotkey({action.keys})", str(e)
                    )
            else:
                res = loop._fail(
                    loop.uia_sensor.sense(), step.step_index, "unknown", "none",
                    step.tool_name, f"Unsupported action type: {action.action_type}"
                )

            step_latency = res.step_log.latency_ms or int((time.perf_counter() - step_t0) * 1000)
            rep = StepExecutionReport(
                step_index=step.step_index,
                action_type=action.action_type.value,
                target=action.target,
                description=step.rationale or action.description,
                success=res.success,
                latency_ms=step_latency,
                error=res.step_log.error if not res.success else None,
                attempts_used=res.attempts_used,
            )
            reports.append(rep)

            if self.on_step_progress:
                self.on_step_progress(step, rep)

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
        )

        if self.on_plan_complete:
            try:
                self.on_plan_complete(summary)
            except Exception as e:
                logger.warning("Error in on_plan_complete callback: %s", e)

        return summary
