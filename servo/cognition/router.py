"""The cognitive cascade router.

One explicit function decides which tier handles each piece of work:

    goal ─► Tier 1 (grammar → Cactus Needle) ─► confident AND Kev says "single"? ─► System 1 executes
                │                                           │ no (reasoning miss)
                │                                           ▼
                │                                  System 2 writes plain-language subtasks
                │                                           │ each subtask goes back DOWN to Tier 1
                ▼                                           ▼
    execution per step:  UIA (desktop) / Playwright DOM (web)
                         ── perception miss ──► Mirage (sideways, same step)
                         ── Kev pre-check (risk/scope) → your confirmation if medium/high
                         ── actuation (once) → Kev post-check
    failure ─► classify:  policy/abort → stop and report
                          perception / verification / reasoning → System 2 re-plans with
                          what was tried, why it failed and what the screen shows now
                          (bounded by settings.max_replans)

Every decision is emitted to the trace bus, so the copilot shows the full reasoning.
"""

from __future__ import annotations

import collections
import sys
import threading
import time
import uuid
from typing import Any, Callable, Deque, Dict, List, Optional, Set, Tuple, Union

from pydantic import BaseModel, Field

from servo.cognition import browser as browser_mod
from servo.cognition.needle import BROWSER_TOOLS, Tier1Intent, describe_step
from servo.cognition.system2 import System2, System2Unavailable
from servo.cognition.trace import Tracer
from servo.config import settings
from servo.control.confirmation import ConfirmationCallback, ConfirmationRequest
from servo.control.handoff import PlanExecutionSummary, StepExecutionReport, SystemHandoffCoordinator
from servo.control.jev_gate import JevGate
from servo.perception import windows
from servo.perception.scene_index import SceneDelta
from servo.planner.tool_calling import MacroPlan, PlanStep

AskFn = Callable[[str, Dict[str, Any], List[str], Optional[float]], str]


def _window_class(hwnd: int) -> str:
    import ctypes
    buf = ctypes.create_unicode_buffer(256)
    ctypes.windll.user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


BROWSER_CAPABLE = {"click_element", "click", "double_click", "right_click", "type_text", "send_hotkey"}


class StepRecord(BaseModel):
    command: str
    tool: str
    tier: str
    success: bool
    error: Optional[str] = None
    latency_ms: int = 0


class StepOutcome(BaseModel):
    success: bool
    kind: Optional[str] = None  # perception | verify | reasoning | policy | abort | error
    error: Optional[str] = None
    tier: str = "uia"
    detail: Dict[str, Any] = Field(default_factory=dict)
    reports: List[StepExecutionReport] = Field(default_factory=list)


class RunOutcome(BaseModel):
    run_id: str
    goal: str
    success: bool
    reply: str
    route: str
    steps: List[StepRecord] = Field(default_factory=list)
    replans: int = 0
    aborted: bool = False
    latency_ms: int = 0


def classify_error(err: str) -> str:
    e = (err or "").lower()
    if "cancel" in e:
        return "abort"
    if any(k in e for k in ("rejected by user", "requires confirmation", "out of scope", "sandbox", "not allowed", "whitelist")):
        return "policy"
    if "ambiguous" in e:
        return "reasoning"
    if any(k in e for k in ("not observed", "no ui state mutation", "expected change", "could not be matched")):
        return "verify"
    if any(k in e for k in ("no ui element", "no unique element", "mirage", "uia failed", "not found",
                            "could not find or launch", "no interactive target")):
        return "perception"
    return "error"


class CascadeRouter:
    def __init__(
        self,
        *,
        tracer: Tracer,
        user_name: str = "User",
        brain: Any = None,
        kev: Optional[JevGate] = None,
        tier1: Optional[Tier1Intent] = None,
        system2: Optional[System2] = None,
        browser: Optional[browser_mod.BrowserTier] = None,
        should_abort: Optional[threading.Event] = None,
        confirmation_callback: Optional[ConfirmationCallback] = None,
        app_launcher: Optional[Callable[[str], Tuple[Optional[int], str]]] = None,
        target_hwnd: Optional[int] = None,
        exclude_hwnds: Optional[Callable[[], Set[int]]] = None,
        history: Optional[List[Dict[str, str]]] = None,
        context: str = "desktop",
        ask: Optional[AskFn] = None,
        step_mode: bool = False,
    ) -> None:
        self.tracer = tracer
        self.user_name = user_name
        self.brain = brain
        self.kev = kev or JevGate()
        self.tier1 = tier1 or Tier1Intent(user_name=user_name, tracer=tracer)
        self.tier1.tracer = tracer
        self.system2 = system2 or System2(user_name=user_name, tracer=tracer)
        self.system2.tracer = tracer
        self.browser = browser if browser is not None else browser_mod.browser_tier
        self.abort_event = should_abort or threading.Event()
        self.confirmation_callback = confirmation_callback
        # ask(kind, payload, options) -> chosen option; drives the copilot's buttons.
        self.ask = ask
        self.step_mode = step_mode
        self.app_launcher = app_launcher
        self.hwnd = target_hwnd
        self.title = windows.get_window_title(target_hwnd) if target_hwnd and windows.is_window(target_hwnd) else ""
        self.exclude_hwnds = exclude_hwnds
        self.history = history or []
        self.context = context  # "desktop" | "browser"
        self.goal = ""
        self._reports: List[StepExecutionReport] = []
        self._skip_requested = False

    # ================================================================== run

    def run(self, goal: str) -> RunOutcome:
        t0 = time.perf_counter()
        self.goal = goal.strip()
        tr = self.tracer
        tr.emit("goal", self.goal, tier="copilot")
        records: List[StepRecord] = []
        counters = {"replans": 0, "rounds": 0}

        def finish(success: bool, reply: str, route: str, aborted: bool = False) -> RunOutcome:
            out = RunOutcome(
                run_id=tr.run_id, goal=self.goal, success=success, reply=reply, route=route, steps=records,
                replans=counters["replans"], aborted=aborted, latency_ms=int((time.perf_counter() - t0) * 1000),
            )
            tr.emit("result", reply, tier="copilot", status="ok" if success else ("warn" if aborted else "fail"),
                    duration_ms=out.latency_ms, route=route, replans=counters["replans"],
                    rounds=counters["rounds"], steps=len(records))
            self._record_telemetry(out)
            return out

        # 0. Conversation and desktop introspection never need a model.
        if self.brain is not None:
            quick = self.brain.conversational_reply(self.goal)
            if quick:
                tr.emit("route", "Conversation", "Answered locally without touching the desktop.", tier="router")
                return finish(True, quick, "chat")

        # 1. Tier 1: a compound request whose every clause parses needs no LLM.
        compound = self.tier1.propose_compound(self.goal)
        proposal = compound or self.tier1.propose(self.goal)

        # 2. Kev routing decision.
        with tr.span("decision", "Kev · route the request", tier="kev") as sp:
            assess = self.kev.assess_goal(
                self.goal, tier1_parsed=proposal is not None and proposal.source == "grammar"
            )
            sp.done("ok", assess.reason, confidence=assess.confidence, desktop_task=assess.desktop_task,
                    complexity=assess.complexity, probabilities=assess.complexity_probabilities, source=assess.source)

        thr = settings.needle_confidence_threshold
        confident = proposal is not None and proposal.confidence >= thr
        agentic = False

        if compound is not None:
            tr.emit("route", f"System 1 runs all {len(compound.steps)} steps",
                    "Every clause is a direct command, so the sequence runs on-device with no LLM.",
                    tier="router", status="ok")
            items: List[Union[str, PlanStep]] = list(compound.steps)
            route = "system1"
        elif confident and assess.complexity != "multi":
            tr.emit("route", "System 1 handles this directly",
                    f"Tier 1 ({proposal.source}) is confident ({proposal.confidence:.2f} ≥ {thr:.2f}) and Kev rates the "
                    f"request '{assess.complexity}'. No LLM needed.", tier="router", status="ok")
            items = list(proposal.steps)
            route = "system1"
        elif proposal is None and not assess.desktop_task:
            tr.emit("route", "Conversation → System 2", "Not a desktop task; answering as chat.", tier="router")
            return finish(True, self._chat(), "chat")
        else:
            why = []
            if proposal is None:
                why.append("Tier 1 could not map the request to tool calls")
            elif not confident:
                why.append(f"Tier 1 confidence {proposal.confidence:.2f} is below {thr:.2f}")
            if assess.complexity != "single":
                why.append(f"Kev rates it '{assess.complexity}'")
            tr.emit("escalate", "Reasoning miss → System 2", "; ".join(why) + ".", tier="system2", status="warn")
            route = "system2"
            if not self.system2.available():
                if proposal is not None and proposal.source == "grammar":
                    tr.emit("route", "System 2 not ready — falling back to the direct parse",
                            self.system2.unavailable_reason(), tier="router", status="warn")
                    items = list(proposal.steps)
                    route = "system1-fallback"
                else:
                    return finish(False, self._no_system2_message(), "system2-unavailable")
            else:
                try:
                    plan = self.system2.plan(self.goal, screen=self._screen_summary(), round_no=1)
                except System2Unavailable as e:
                    tr.emit("error", "System 2 failed", str(e), tier="system2", status="fail")
                    return finish(False, f"I couldn't plan that: {e}", route)
                counters["rounds"] = 1
                if plan.done or not plan.subtasks:
                    return finish(True, plan.reply or "Nothing to do — that's already done.", route)
                items = list(plan.subtasks)
                agentic = True

        # 3. Act → observe → decide, until System 2 says the goal is done.
        completed: List[str] = []
        last_batch: Tuple[str, ...] = ()
        while True:
            status, failure = self._run_batch(items, completed, records, counters["rounds"] or 1)
            if status == "abort":
                return finish(False, "Stopped. Nothing else will run.", route, aborted=True)
            if status == "policy":
                return finish(False, f"I didn't do “{failure['command']}”: {failure.get('error')}", route)

            if status == "failed":
                kind = failure.get("kind") or "error"
                if counters["replans"] >= settings.max_replans or not self.system2.available():
                    hint = "" if self.system2.available() else f" ({self.system2.unavailable_reason()})"
                    return finish(False, f"I got stuck on “{failure['command']}”: {failure.get('error')}{hint}", route)
                counters["replans"] += 1
                label = {"perception": "Perception miss", "verify": "Verification failed",
                         "reasoning": "Reasoning miss", "error": "Step failed"}.get(kind, "Step failed")
                tr.emit("escalate", f"{label} → System 2 re-plan ({counters['replans']}/{settings.max_replans})",
                        str(failure.get("error") or ""), tier="system2", status="warn", failure=failure)
                screen_kind: Optional[str] = kind
            else:
                if not agentic:
                    n = len(records)
                    return finish(True, f"Done — {n} step{'s' if n != 1 else ''} completed.", route)
                failure = None
                screen_kind = None
                if counters["rounds"] >= settings.max_rounds:
                    choice = self._ask("continue", {"rounds": counters["rounds"], "steps": len(records),
                                                    "message": f"I've done {len(records)} steps over {counters['rounds']} rounds."},
                                       ["continue", "stop"], default="stop")
                    if choice != "continue":
                        return finish(False, f"Paused after {counters['rounds']} rounds and {len(records)} steps. "
                                             "Ask me to continue if there's more to do.", route)
                    counters["rounds"] = 0

            route = "system2"
            agentic = True
            try:
                plan = self.system2.plan(self.goal, screen=self._screen_summary(screen_kind), completed=completed,
                                         failure=failure, round_no=counters["rounds"] + 1)
            except System2Unavailable as e:
                return finish(False, f"I couldn't decide the next step: {e}", route)
            counters["rounds"] += 1
            if plan.done:
                n = len(records)
                return finish(True, plan.reply or f"Done — {n} step{'s' if n != 1 else ''} completed.", route)
            if not plan.subtasks:
                return finish(False, plan.reply or "I'm not sure what to do next.", route)
            batch = tuple(plan.subtasks)
            if batch == last_batch and failure is None:
                return finish(False, "I was about to repeat the same steps without progress, so I stopped. "
                                     + (plan.reply or ""), route)
            last_batch = batch
            items = list(plan.subtasks)

    def _run_batch(
        self, items: List[Union[str, PlanStep]], completed: List[str], records: List["StepRecord"], round_no: int,
    ) -> Tuple[str, Dict[str, Any]]:
        """Run one batch of subtasks. Returns ("ok"|"failed"|"policy"|"abort", failure)."""
        tr = self.tracer
        thr = settings.needle_confidence_threshold
        labels = [i if isinstance(i, str) else describe_step(i) for i in items]
        plan_ev = tr.emit("plan", f"Round {round_no} · {len(items)} step{'s' if len(items) != 1 else ''}",
                          "\n".join(labels), tier="system2" if any(isinstance(i, str) for i in items) else "needle",
                          subtasks=labels, round=round_no)

        def progress(i: int, status: str, note: str = "") -> None:
            tr.emit("progress", labels[i], note, tier="router", status=status, plan=plan_ev.seq, index=i)

        for i, item in enumerate(items):
            command = labels[i]
            if self.abort_event.is_set():
                return "abort", {"command": command}

            if self.step_mode:
                choice = self._ask("step", {"command": command, "index": i + 1, "total": len(items)},
                                   ["next", "run_all", "stop"], default="stop", timeout=None)
                if choice == "stop":
                    self.abort_event.set()
                    return "abort", {"command": command}
                if choice == "run_all":
                    self.step_mode = False

            progress(i, "running")
            if isinstance(item, str):
                prop = self.tier1.propose(item) or self.tier1.propose_compound(item)
                if prop is None or prop.confidence < thr:
                    progress(i, "fail", "not understood")
                    return "failed", {"command": item, "kind": "reasoning",
                                      "error": "System 1 could not interpret this command; rewrite it in the command grammar."}
                steps = list(prop.steps)
            else:
                steps = [item]

            skipped = False
            for step in steps:
                if self.abort_event.is_set():
                    progress(i, "fail", "stopped")
                    return "abort", {"command": command}
                t_step = time.perf_counter()
                with tr.span("tier", f"Step · {describe_step(step)}", tier="router", command=command) as sp:
                    out = self._execute(step)
                    sp.done("ok" if out.success else "fail", out.error or "", tier=out.tier)
                records.append(StepRecord(command=command, tool=step.tool_name, tier=out.tier, success=out.success,
                                          error=out.error, latency_ms=int((time.perf_counter() - t_step) * 1000)))
                if out.success:
                    continue
                if self._skip_requested:
                    self._skip_requested = False
                    skipped = True
                    break
                kind = out.kind or classify_error(out.error or "")
                if self.abort_event.is_set():
                    kind = "abort"
                progress(i, "fail", out.error or "")
                failure = {"command": command, "tool_call": describe_step(step), "kind": kind,
                           "error": out.error, **out.detail}
                return ("abort" if kind == "abort" else "policy" if kind == "policy" else "failed"), failure

            if skipped:
                progress(i, "skipped", "skipped by you")
                completed.append(f"(skipped by the user) {command}")
            else:
                progress(i, "ok")
                completed.append(command)
        return "ok", {}

    # ------------------------------------------------------------ asking you

    def _ask(self, kind: str, payload: Dict[str, Any], options: List[str], default: str,
             timeout: Optional[float] = -1) -> str:
        """Ask the user through the copilot's buttons; ``default`` when nobody can answer."""
        if self.ask is None:
            return default
        try:
            return self.ask(kind, payload, options, timeout) or default
        except Exception:
            return default

    def _confirm(self, req: ConfirmationRequest) -> bool:
        """Risky action: Proceed / Skip this step / Stop."""
        if self.ask is not None:
            choice = self._ask("confirm", req.model_dump(), ["proceed", "skip", "stop"], default="stop")
            if choice == "skip":
                self._skip_requested = True
                self.tracer.emit("decision", "You skipped this step", req.tool_call, tier="kev", status="warn")
                return False
            if choice == "stop":
                self.abort_event.set()
                return False
            return choice == "proceed"
        if self.confirmation_callback is not None:
            try:
                return bool(self.confirmation_callback(req))
            except Exception:
                return False
        return False

    # ============================================================ execution

    def _execute(self, step: PlanStep) -> StepOutcome:
        tool = step.tool_name
        if tool in BROWSER_TOOLS:
            return self._browser_nav(step)
        if tool in BROWSER_CAPABLE and self.context == "browser" and self._browser_open():
            return self._browser_action(step)
        return self._desktop(step)

    def _browser_open(self) -> bool:
        try:
            return self.browser.is_open()
        except Exception:
            return False

    # ---------------------------------------------------------- desktop

    def _desktop(self, step: PlanStep) -> StepOutcome:
        if step.tool_name in ("launch_app", "close_window"):
            self.context = "desktop"
        coord = SystemHandoffCoordinator(
            target_hwnd=self.hwnd,
            fast_mode=settings.fast_mode,
            confirmation_callback=self._confirm,
            should_abort=self.abort_event,
            tracer=self.tracer,
            exclude_hwnds=self.exclude_hwnds,
        )
        summary = coord.execute_plan(MacroPlan(goal=self.goal, steps=[step]), app_launcher=self.app_launcher)
        self._reports.extend(summary.step_reports)
        if summary.active_hwnd:
            self.hwnd, self.title = summary.active_hwnd, summary.active_title
        else:
            self.hwnd, self.title = None, ""
        rep = summary.step_reports[-1] if summary.step_reports else None
        if rep is None:
            return StepOutcome(success=False, kind="error", error="Step produced no report.")
        if step.tool_name == "launch_app" and rep.success:
            self.tracer.emit("perception", f"Window ready · {self.title}", f"HWND {self.hwnd}", tier="uia", status="ok")
        self._follow_dialog()
        kind = None if rep.success else ("abort" if summary.aborted else classify_error(rep.error or ""))
        return StepOutcome(success=rep.success, kind=kind, error=rep.error, tier=rep.perception_source or "uia",
                           reports=[rep])

    def _follow_dialog(self) -> None:
        """If the step opened a dialog (Save As, a prompt), make it the target window.

        Dialogs are separate top-level windows, so the next step's UIA scan of the main
        window would never see their controls.
        """
        if sys.platform != "win32":
            return
        try:
            fg = windows.get_foreground_window()
            excluded = self.exclude_hwnds() if self.exclude_hwnds else set()
            if not fg or fg == self.hwnd or fg in excluded or not windows.is_window(fg):
                return
            same_app = self.hwnd and windows.get_process_name(fg) == windows.get_process_name(self.hwnd)
            if same_app or _window_class(fg) == "#32770":
                self.hwnd, self.title = fg, windows.get_window_title(fg)
                self.tracer.emit("perception", f"Following dialog · {self.title}", f"HWND {fg}", tier="uia", status="ok")
        except Exception:
            pass

    # ---------------------------------------------------------- browser

    def _gate(self, sub_goal: str, tool_call: str, label: str, window_title: str) -> Optional[StepOutcome]:
        """Kev pre-check + confirmation. Returns a failed outcome to stop, or None to proceed."""
        pre = self.kev.evaluate_pre_risk(sub_goal, tool_call, label)
        self.tracer.emit("decision", f"Kev pre-check · {pre.risk_level} risk", pre.reason, tier="kev",
                         status="ok" if pre.risk_level == "low" and pre.scope_valid else "warn",
                         source=pre.source, scope_valid=pre.scope_valid, probabilities=pre.probabilities)
        if not pre.scope_valid:
            return StepOutcome(success=False, kind="policy", tier="kev",
                               error=f"Kev rejected the action as out of scope: {pre.reason}")
        if settings.mode == "assistant" and pre.risk_level in ("medium", "high"):
            approved = self._confirm(ConfirmationRequest(
                sub_goal=sub_goal, tool_call=tool_call, element_label=label, risk_level=pre.risk_level,
                reason=pre.reason, hwnd=0, window_title=window_title, process_name="browser",
            ))
            self.tracer.emit("decision", f"Your confirmation · {'approved' if approved else 'rejected'}", tool_call,
                             tier="kev", status="ok" if approved else "fail")
            if not approved:
                if self.abort_event.is_set():
                    return StepOutcome(success=False, kind="abort", tier="kev", error="Execution cancelled by user.")
                return StepOutcome(success=False, kind="policy", tier="kev",
                                   error="Action rejected by user confirmation (declined or timed out).")
        if self.abort_event.is_set():
            return StepOutcome(success=False, kind="abort", tier="browser", error="Execution cancelled by user.")
        return None

    def _browser_nav(self, step: PlanStep) -> StepOutcome:
        args = step.arguments
        call = describe_step(step)
        target = str(args.get("url") or args.get("query") or "")
        stop = self._gate(step.rationale or call, call, target, "browser")
        if stop:
            return stop
        self.tracer.emit("action", call, "Playwright drives a real browser window", tier="browser")
        try:
            t0 = time.perf_counter()
            state = self.browser.open_url(target) if step.tool_name == "open_url" else self.browser.web_search(target)
        except Exception as e:
            return StepOutcome(success=False, kind="error", tier="browser", error=f"Browser failed: {e}")
        self.context = "browser"
        self.tracer.emit("perception", f"Page loaded · {state.title or state.url}", state.url, tier="browser",
                         status="ok", duration_ms=int((time.perf_counter() - t0) * 1000),
                         aria_snapshot=state.snapshot[:6000])
        self._report(step, True, call)
        return StepOutcome(success=True, tier="browser")

    def _browser_action(self, step: PlanStep) -> StepOutcome:
        args = step.arguments
        tool = step.tool_name
        call = describe_step(step)
        if tool == "send_hotkey":
            keys = list(args.get("keys") or [])
            stop = self._gate(step.rationale or call, call, "+".join(keys), "browser")
            if stop:
                return stop
            self.tracer.emit("action", call, "Playwright keyboard", tier="browser")
            before, after = self.browser.press(keys)
            delta = browser_mod.snapshot_delta(before, after)
            self.tracer.emit("verify", "Page " + ("changed" if delta["has_changed"] else "unchanged (key accepted)"),
                             f"{after.title} <{after.url}>", tier="browser", status="ok")
            self._report(step, True, call)
            return StepOutcome(success=True, tier="browser")

        for_input = tool == "type_text"
        target = str(args.get("target") or ("" if for_input else ""))
        t0 = time.perf_counter()
        loc = self.browser.locate(target, for_input=for_input)
        self.tracer.emit(
            "perception",
            f"DOM · “{target or 'input'}” → " + (f"{loc.count} match" + ("es" if loc.count != 1 else "")),
            loc.strategy or "no locator strategy matched", tier="browser",
            status="ok" if loc.found else ("warn" if loc.count > 1 else "fail"),
            confidence=loc.confidence, duration_ms=int((time.perf_counter() - t0) * 1000),
            tried=loc.tried, candidates=loc.candidates,
        )

        if not loc.found and loc.count > 1:
            return StepOutcome(success=False, kind="reasoning", tier="browser",
                               error=f"Ambiguous target “{target}”: {loc.count} matches",
                               detail={"candidates": loc.candidates})
        if not loc.found:
            if for_input:
                return StepOutcome(success=False, kind="perception", tier="browser",
                                   error=f"No input field matching “{target}” on the page")
            return self._browser_mirage_click(step, target)

        stop = self._gate(step.rationale or call, call, target, "browser")
        if stop:
            return stop
        self.tracer.emit("action", call, f"Locator: {loc.strategy}", tier="browser")
        try:
            if for_input:
                text = str(args.get("text") or "")
                _, before, after, value = self.browser.fill(
                    target, text, press_enter=bool(args.get("press_enter")), clear_first=args.get("clear_first", True) is not False,
                )
                if text and text in (value or ""):
                    self.tracer.emit("verify", "Kev post-check · change confirmed", "Typed text is in the field.",
                                     tier="kev", status="ok", confidence=0.95)
                    self._report(step, True, call)
                    return StepOutcome(success=True, tier="browser")
            else:
                button = str(args.get("button") or ("double" if tool == "double_click" else "right" if tool == "right_click" else "left"))
                _, before, after = self.browser.click(target, button=button)
        except Exception as e:
            return StepOutcome(success=False, kind="error", tier="browser", error=f"Browser action failed: {e}")
        return self._browser_verify(step, call, before, after)

    def _browser_verify(self, step: PlanStep, call: str, before: Any, after: Any) -> StepOutcome:
        delta = SceneDelta(**browser_mod.snapshot_delta(before, after))
        post = self.kev.evaluate_post_transition(step.rationale or call, delta, expected_outcome=call,
                                                 typed_text=str(step.arguments.get("text") or "") if step.tool_name == "type_text" else "")
        self.tracer.emit("verify", "Kev post-check · " + ("change confirmed" if post.transition_ok else "expected change not seen"),
                         post.explanation, tier="kev", status="ok" if post.transition_ok else "fail",
                         confidence=post.confidence, delta=delta.model_dump())
        self._report(step, post.transition_ok, call, None if post.transition_ok else post.explanation)
        if post.transition_ok:
            return StepOutcome(success=True, tier="browser")
        return StepOutcome(success=False, kind="verify", tier="browser", error=post.explanation)

    def _browser_mirage_click(self, step: PlanStep, target: str) -> StepOutcome:
        from servo.perception.mirage_sensor import describe_image, match_target, parse_detections

        call = describe_step(step)
        self.tracer.emit("escalate", "Perception miss → Mirage", "Not in the DOM/accessibility tree (canvas, icon or "
                         "image button?) — looking at the page pixels.", tier="mirage", status="warn")
        t0 = time.perf_counter()
        img = self.browser.screenshot_image()
        raw = describe_image(img) if img is not None else None
        if raw is None:
            self.tracer.emit("perception", "Mirage unavailable", "Install cactus-compute (>= 3.3) for visual fallback.",
                             tier="mirage", status="fail")
            return StepOutcome(success=False, kind="perception", tier="mirage",
                               error=f"“{target}” is not on the page and Mirage is unavailable")
        dets = parse_detections(raw, img.width, img.height)
        best, score, ranked = match_target(dets, target)
        ok = best is not None and score >= settings.mirage_match_threshold
        self.tracer.emit("perception", f"Mirage · {len(dets)} detections",
                         (f"“{target}” → “{best.label}” at {best.center}" if ok else "no confident match"),
                         tier="mirage", status="ok" if ok else "fail", confidence=score,
                         duration_ms=int((time.perf_counter() - t0) * 1000), ranked=ranked)
        if not ok:
            return StepOutcome(success=False, kind="perception", tier="mirage",
                               error=f"Mirage found no confident match for “{target}”",
                               detail={"mirage_candidates": ranked})
        stop = self._gate(step.rationale or call, call, best.label, "browser")
        if stop:
            return stop
        self.tracer.emit("action", f"click({best.center[0]}, {best.center[1]})",
                         "Mouse moves along a path to Mirage's target, then clicks", tier="mirage")
        before, after = self.browser.click_point(*best.center)
        return self._browser_verify(step, call, before, after)

    # ---------------------------------------------------------- context

    def _screen_summary(self, failure_kind: Optional[str] = None) -> str:
        """What System 2 sees: page accessibility snapshot, or UIA labels (+ Mirage on perception misses)."""
        parts: List[str] = []
        try:
            if self.context == "browser" and self._browser_open():
                st = self.browser.state()
                parts.append(f"Browser page: {st.title} <{st.url}>\nAccessibility snapshot:\n{st.snapshot[:8000]}")
            elif self.hwnd and windows.is_window(self.hwnd):
                from servo.perception.uia_sensor import UIASensor
                scene = UIASensor(self.hwnd).sense()
                labels = [f"{e.role}: {e.label}" for e in list(scene.elements.values())[:120]]
                parts.append(f"Active window: {scene.window_title}\nControls:\n" + "\n".join(labels))
                if failure_kind == "perception":
                    from servo.perception.mirage_sensor import MirageSensor, summarize
                    ms = MirageSensor(self.hwnd)
                    mscene = ms.sense(window_title=scene.window_title)
                    if not mscene.sense_error:
                        parts.append("Mirage visual detections:\n" + summarize(ms.last_detections))
            wins = [w.get("title") for w in windows.list_windows()[:12] if w.get("title")]
            if wins:
                parts.append("Open windows: " + "; ".join(str(w) for w in wins))
        except Exception as e:
            parts.append(f"(screen description unavailable: {e})")
        text = "\n\n".join(parts)
        self.tracer.emit("perception", "Screen description for System 2", text[:400] + ("…" if len(text) > 400 else ""),
                         tier="system2", full=text)
        return text

    def _chat(self) -> str:
        if self.system2.available():
            try:
                return self.system2.chat(self.goal, self.history).reply or "…"
            except System2Unavailable as e:
                self.tracer.emit("error", "System 2 chat failed", str(e), tier="system2", status="fail")
        if self.brain is not None:
            return self.brain.offline_chat_reply(self.goal)
        return "I can help with things on your screen — try “open notepad and write hello”."

    def _no_system2_message(self) -> str:
        return ("That needs multi-step reasoning, and no System 2 model is configured. Set SERVO_PLANNER_PROVIDER "
                "(anthropic, openai for Ollama, gemini or cactus), or give me one direct command such as "
                "“click Save” or “search the web for …”.")

    def _report(self, step: PlanStep, success: bool, desc: str, error: Optional[str] = None) -> None:
        self._reports.append(StepExecutionReport(
            step_index=len(self._reports) + 1, action_type=step.tool_name,
            target=str(step.arguments.get("target") or step.arguments.get("url") or step.arguments.get("query") or ""),
            description=desc, success=success, latency_ms=0, error=error, perception_source="browser",
        ))

    def _record_telemetry(self, out: RunOutcome) -> None:
        if not self._reports:
            return
        try:
            from servo.daemon.tasks import record_plan_trace
            record_plan_trace(PlanExecutionSummary(
                goal=out.goal, success=out.success, total_steps=len(self._reports),
                completed_steps=sum(1 for r in self._reports if r.success), total_latency_ms=out.latency_ms,
                step_reports=self._reports, active_hwnd=self.hwnd, active_title=self.title, aborted=out.aborted,
            ), mode="assistant")
        except Exception:
            pass


def new_run_id() -> str:
    return uuid.uuid4().hex[:12]
