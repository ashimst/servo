"""The cognitive cascade router (see docs/COGNITIVE_CASCADE.md).

    goal ─► Laya: desktop task? complexity? area?
              │ not a desktop task ─► System 2 answers as chat
              │ single + Needle confident on the request itself ─► execute
              ▼
            planner agent for the area writes steps in the step forms
              ─► each step: the Needle agent for that tool ─► check_call ─► execute
    execute (servo/control/loop.py): UIA ─► Windows OCR ─► vision locator ─► fail;
              Laya pre-check (your confirmation if medium/high) ─► act once ─► verify
    failure ─► policy/abort: stop and report
               perception / verification / reasoning: the planner re-plans with what was
               tried, why it failed and the current screen (settings.max_replans per tier,
               then the local planner steps up to the cloud)

Every decision is emitted to the trace bus, so the copilot shows the full reasoning.
"""

from __future__ import annotations

import re
import sys
import threading
import time
import uuid
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union

from pydantic import BaseModel, Field

from servo.actuation import system_tools
from servo.cognition.needle import BROWSER_TOOLS, Tier1Intent, describe_step
from servo.cognition.system2 import System2, System2Unavailable
from servo.cognition.planners import AGENTS as PLANNER_AGENTS, DEFAULT_AGENT, agent as planner_agent
from servo.cognition.trace import Tracer
from servo.config import settings
from servo.control.confirmation import ConfirmationCallback, ConfirmationRequest
from servo.control.handoff import StepExecutionReport, SystemHandoffCoordinator
from servo.control.jev_gate import JevGate, LayaUnavailable
from servo.perception import app_identity, windows
from servo.cognition.plan import MacroPlan, PlanStep
from servo.actuation.alias_mapping import AliasMapper

# Request actions without a target control (servo/cognition/intents.py request_steps).
_DIRECT_TOOLS = ("audio", "media_key", "window_state", "browser_hotkey", "open_file")

AskFn = Callable[[str, Dict[str, Any], List[str], Optional[float]], str]


def _window_class(hwnd: int) -> str:
    import ctypes
    buf = ctypes.create_unicode_buffer(256)
    ctypes.windll.user32.GetClassNameW(hwnd, buf, 256)
    return buf.value




class StepRecord(BaseModel):
    command: str
    tool: str
    call: str = ""  # the exact tool call, with arguments
    tier: str
    success: bool
    error: Optional[str] = None
    latency_ms: int = 0
    output: str = ""  # what a system tool returned (file text, command output)
    verification: str = "confirmed"  # confirmed | unconfirmed | failed
    note: Optional[str] = None
    # Where it happened, for the desktop knowledge graph.
    target: str = ""
    app: str = ""
    page: str = ""


class StepOutcome(BaseModel):
    success: bool
    kind: Optional[str] = None  # perception | verify | reasoning | policy | abort | error
    error: Optional[str] = None
    tier: str = "uia"
    detail: Dict[str, Any] = Field(default_factory=dict)
    reports: List[StepExecutionReport] = Field(default_factory=list)
    output: str = ""  # system tools: shown to you and fed back to System 2
    verification: str = "confirmed"  # confirmed | unconfirmed | failed (see PostTransitionResult)
    note: Optional[str] = None       # why an unconfirmed step could not be confirmed


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


_RISK_ORDER = {"low": 0, "medium": 1, "high": 2}


def _indent(text: str, prefix: str = "    ") -> str:
    return "\n".join(prefix + line for line in text.splitlines())


def describe_scene(scene: Any, process: str = "", limit: int = 150,
                   numbered: Optional[Dict[int, Dict[str, Any]]] = None) -> str:
    """Screen description for System 2: window, address bar / focused text, then controls.

    Every control gets a number ("[12] Button: Save"); the planner refers to controls by
    that number and ``numbered`` records what each number stands for, so the step is
    grounded to that exact control instead of a label the planner retyped.
    Duplicate labels are collapsed (YouTube lists each channel link twice) and long
    labels are trimmed, so the entries cover more of the page.
    """
    lines = [f"Active window: {scene.window_title}" + (f"  ({process})" if process else "")]
    edits = [e for e in scene.elements.values() if e.role == "Edit" and (e.value or "").strip()]
    if edits:
        lines.append(f"Address bar / input: {(edits[0].value or '')[:200]}")
    seen = set()
    controls: List[str] = []
    texts: List[str] = []
    for e in scene.elements.values():
        label = " ".join((e.label or "").split())[:140]
        key = (e.role, label, getattr(e, "context", None))
        if not label or key in seen:
            continue
        seen.add(key)
        if not getattr(e, "interactive", True):
            if len(texts) < 40:
                texts.append(label)
            continue
        # State in brackets ("[on]", "[collapsed]", "[selected]") and the current value, so
        # the planner can tell whether a switch is already on or which option is chosen.
        tags = e.state_tags() if hasattr(e, "state_tags") else []
        value = (e.value or "").strip()
        shown = f"{e.role}: {label}" + (f" ({e.context})" if getattr(e, "context", None) else "")
        if value and value != label and e.role in ("ComboBox", "Slider", "Spinner", "Edit", "Custom"):
            shown += f" = {value[:80]}"
        if tags:
            shown += f" [{', '.join(tags)}]"
        n = len(controls) + 1
        controls.append(f"[{n}] {shown}")
        if numbered is not None:
            numbered[n] = {"role": e.role, "label": label, "context": getattr(e, "context", None),
                           "bbox": tuple(e.bbox_screen)}
        if len(controls) >= limit:
            break
    lines.append("Controls:\n" + "\n".join(controls))
    if texts:
        lines.append("Text on screen:\n" + "\n".join(texts))
    return "\n".join(lines)


def _summary_reply(records: List["StepRecord"]) -> str:
    """Final reply that says what was confirmed and what only ran."""
    n = len(records)
    unconf = [r for r in records if r.success and r.verification == "unconfirmed"]
    base = f"Done — {n} step{'s' if n != 1 else ''} completed."
    if not unconf:
        return base
    lines = [f"Ran {n} step{'s' if n != 1 else ''}, but I couldn't confirm "
             f"{'this one' if len(unconf) == 1 else f'{len(unconf)} of them'} worked:"]
    lines += [f"• {r.command}: {r.note or 'no visible change'}" for r in unconf]
    lines.append("Please check the screen.")
    return "\n".join(lines)


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
        laya: Optional[JevGate] = None,
        tier1: Optional[Tier1Intent] = None,
        system2: Optional[System2] = None,
        should_abort: Optional[threading.Event] = None,
        confirmation_callback: Optional[ConfirmationCallback] = None,
        app_launcher: Optional[Callable[[str], Tuple[Optional[int], str]]] = None,
        target_hwnd: Optional[int] = None,
        exclude_hwnds: Optional[Callable[[], Set[int]]] = None,
        history: Optional[List[Dict[str, str]]] = None,
        context: str = "desktop",
        ask: Optional[AskFn] = None,
        step_mode: bool = False,
        memory: Optional[Any] = None,
    ) -> None:
        self.tracer = tracer
        self.user_name = user_name
        self.brain = brain
        self.laya = laya or JevGate()
        self.tier1 = tier1 or Tier1Intent(user_name=user_name, tracer=tracer)
        self.tier1.tracer = tracer
        self.system2 = system2 or System2(user_name=user_name, tracer=tracer)
        self.system2.tracer = tracer
        self.abort_event = should_abort or threading.Event()
        self.confirmation_callback = confirmation_callback
        # ask(kind, payload, options) -> chosen option; drives the copilot's buttons.
        self.ask = ask
        self.step_mode = step_mode
        self.app_launcher = app_launcher
        self.hwnd = target_hwnd
        self.title = windows.get_window_title(target_hwnd) if target_hwnd and windows.is_window(target_hwnd) else ""
        # Servo's own windows are never a target. Without a list from the copilot
        # (CLI, task suite, another process) fall back to finding them by title.
        if exclude_hwnds is None:
            from servo.actuation.overlay import own_windows as exclude_hwnds
        self.exclude_hwnds = exclude_hwnds
        self._commands_declined = False
        # What each control number in the last screen description stands for.
        self._numbered: Dict[int, Dict[str, Any]] = {}
        self.agent = DEFAULT_AGENT   # planner agent for this task (Laya picks it; planners.py)
        self._handoffs = 0
        self._outside_planner = False   # run_step: an MCP caller plans; every specialist is allowed
        self.history = history or []
        if memory is not None:
            self.memory = memory
        else:
            from servo.cognition.memory import chat_memory
            self.memory = chat_memory
        self.context = context  # "desktop" (web pages are windows driven like any app)
        self.recent_browser = ""  # browser app this request launched (user browser mode)
        self.goal = ""
        self._skip_requested = False

    def _try_intent_request(self, complexity: str) -> Tuple[Optional[List[PlanStep]], float, str]:
        """Try the single-step intent fast path; return (None, 0.0, reason) to hand off to System 2."""
        if complexity != "single" or settings.intent_backend == "none":
            return None, 0.0, ""
        from servo.cognition import intents

        if settings.intent_backend == "needle":
            if not settings.needle_intent_weights:
                return None, 0.0, ""
            reading = intents.read(self.goal, "request", tracer=self.tracer)
        else:
            reading = intents.read_local_planner(self.goal, "request", tracer=self.tracer)

        if not reading:
            return None, 0.0, reading.why or "No single direct tool fits; handing off to planner"
        steps: List[PlanStep] = []
        try:
            for call in reading.calls:
                steps.extend(intents.request_steps(call["name"], call.get("arguments") or {}, start=len(steps) + 1))
        except intents.IntentError as e:
            return None, 0.0, str(e)
        return steps, float(reading.confidence or 0.0), ""

    # ================================================================== run

    def run(self, goal: str) -> RunOutcome:
        t0 = time.perf_counter()
        from servo.cognition.redact import redact_secrets
        self.goal, secrets_found = redact_secrets(goal.strip())
        tr = self.tracer
        tr.emit("goal", self.goal, tier="copilot")
        if secrets_found:
            # Redacted before memory, traces or any model sees it; Servo never types secrets.
            tr.emit("decision", "Secret removed from your request",
                    f"{', '.join(sorted(set(secrets_found)))} replaced with [redacted]. Servo never types "
                    "passwords, codes or card numbers; enter those yourself.", tier="laya", status="warn")
        records: List[StepRecord] = []
        counters = {"replans": 0, "rounds": 0, "still": 0}
        self._commands_declined = False
        self.system2.reset_tier()
        self.agent, self._handoffs = DEFAULT_AGENT, 0

        def finish(success: bool, reply: str, route: str, aborted: bool = False) -> RunOutcome:
            if self.memory is not None:
                try:
                    self.memory.add_turn("assistant", reply, run_id=tr.run_id, route=route)
                except Exception as e:  # noqa: BLE001
                    tr.emit("error", "Chat memory not saved", f"{e.__class__.__name__}: {e}", tier="copilot",
                            status="warn")
            out = RunOutcome(
                run_id=tr.run_id, goal=self.goal, success=success, reply=reply, route=route, steps=records,
                replans=counters["replans"], aborted=aborted, latency_ms=int((time.perf_counter() - t0) * 1000),
            )
            tr.emit("result", reply, tier="copilot", status="ok" if success else ("warn" if aborted else "fail"),
                    duration_ms=out.latency_ms, route=route, replans=counters["replans"],
                    rounds=counters["rounds"], steps=len(records))
            try:
                from servo.telemetry.run_record import build_record, save_run
                save_run(build_record(out, tr.events, {"secrets_redacted": bool(secrets_found),
                                                        "browser": self.recent_browser or None}))
            except Exception as e:  # noqa: BLE001
                tr.emit("error", "Run record not saved (diagnosis won't find this run)",
                        f"{e.__class__.__name__}: {e}", tier="copilot", status="warn")
            self._remember_run(out)
            return out

        if self.memory is not None:
            try:
                self.memory.add_turn("user", self.goal, run_id=tr.run_id)
            except Exception as e:  # noqa: BLE001
                tr.emit("error", "Chat memory not saved", f"{e.__class__.__name__}: {e}", tier="copilot",
                        status="warn")

        # 0. A stated preference ("dia is my default browser") is remembered, not executed.
        from servo.cognition.user_browser import parse_browser_preference
        pref = parse_browser_preference(self.goal)
        if pref:
            from servo import user_settings
            try:
                user_settings.save(settings, {"preferred_browser": pref}, mark_done=False)
            except Exception as e:  # noqa: BLE001
                tr.emit("error", "Preference not saved", f"{e.__class__.__name__}: {e}", tier="router", status="fail")
                return finish(False, f"I couldn't save that preference: {e}", "preference")
            tr.emit("route", "Preference saved", f"preferred_browser = {pref}", tier="router", status="ok")
            return finish(True, f"Got it. I'll open web pages in {pref.capitalize()} from now on.", "preference")

        # 0c. "why did that fail?": diagnose the last failed run from its record and the code graph.
        from servo.cognition.diagnose import diagnose, parse_diagnose_request
        target_run = parse_diagnose_request(self.goal)
        if target_run is not None:
            with tr.span("tier", "Self-diagnosis · run record + code graph", tier="router") as sp:
                d = diagnose(target_run, explain=self.system2.available())
                sp.done("ok" if d.found else "warn", d.cause or "no run to diagnose", run=d.run_id,
                        code=[c.model_dump() for c in d.code], failing_step=d.failing_step)
            return finish(d.found, d.to_text(), "diagnose")

        # 1. Laya decides what kind of request this is; it has no rule-based stand-in.
        with tr.span("decision", "Laya · route the request", tier="laya") as sp:
            try:
                assess = self.laya.assess_goal(self.goal)
            except LayaUnavailable as e:
                sp.done("fail", str(e))
                return finish(False, f"I can't decide how to handle that: {e}", "laya-unavailable")
            sp.done("ok", assess.reason, confidence=assess.confidence, desktop_task=assess.desktop_task,
                    complexity=assess.complexity, probabilities=assess.complexity_probabilities, source=assess.source,
                    domain=assess.domain, domain_probabilities=assess.domain_probabilities)
        self.agent = planner_agent(assess.domain).name

        if not assess.desktop_task:
            tr.emit("route", "Conversation → System 2", "Laya: not a desktop task; answering as chat.", tier="router")
            if not self.system2.available():
                return finish(False, self._no_system2_message(), "system2-unavailable")
            try:
                return finish(True, self._chat(), "chat")
            except System2Unavailable as e:
                tr.emit("error", "System 2 chat failed", str(e), tier="system2", status="fail")
                return finish(False, f"I couldn't answer that: {e}", "chat")

        # 2. A single direct action goes straight to Needle; anything else is planned.
        thr = settings.needle_confidence_threshold
        intent_steps, intent_conf, intent_why = self._try_intent_request(assess.complexity)
        proposal = None
        if intent_steps is None:
            proposal = self.tier1.propose(self.goal) if assess.complexity == "single" else None
        agentic = False

        if intent_steps is not None:
            tr.emit("route", "Needle handles this directly",
                    f"Laya rates the request 'single' and tuned Needle is confident ({intent_conf:.2f} ≥ {settings.needle_intent_threshold:.2f}).",
                    tier="router", status="ok")
            items: List[Union[str, PlanStep]] = list(intent_steps)
            route = "system1"
        elif proposal is not None and proposal.confidence >= thr:
            tr.emit("route", "Needle handles this directly",
                    f"Laya rates the request 'single' and Needle is confident ({proposal.confidence:.2f} ≥ {thr:.2f}).",
                    tier="router", status="ok")
            items = list(proposal.steps)
            route = "system1"
        else:
            why = [f"Laya rates it '{assess.complexity}'"] if assess.complexity != "single" else \
                [intent_why or self.tier1.last_error or "Needle could not map the request to tool calls"]
            tr.emit("escalate", "→ System 2", "; ".join(why) + ".", tier="system2", status="warn")
            route = "system2"
            if not self.system2.available():
                return finish(False, self._no_system2_message(), "system2-unavailable")
            else:
                try:
                    tr.emit("route", f"Planner agent · {self.agent}",
                            f"Laya: this is a {planner_agent(self.agent).title} task.", tier="system2", status="ok",
                            probabilities=assess.domain_probabilities)
                    plan = self.system2.plan(self.goal, screen=self._screen_summary(), round_no=1, agent=self.agent)
                    if plan.next_agent and self._hand_off(plan) and not plan.subtasks:
                        plan = self.system2.plan(self.goal, screen=self._screen_summary(), round_no=1, agent=self.agent)
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
        last_screen: Optional[str] = None
        repeated: Optional[Dict[str, Any]] = None
        while True:
            if repeated is not None:
                status, failure, repeated = "failed", repeated, None
            else:
                status, failure = self._run_batch(items, completed, records, counters["rounds"] or 1)
            if status == "abort":
                return finish(False, "Stopped. Nothing else will run.", route, aborted=True)
            if status == "policy" and self._declined_command_can_replan(failure, counters):
                # The user said no to a shell command, not to the goal: plan again through
                # the app's own interface. (Before, every task stopped dead at the first
                # declined "Start-Process ms-settings:…" or "Rename-Item".)
                tr.emit("decision", "Command declined → plan through the interface instead",
                        failure.get("command", ""), tier="system2", status="warn")
                status = "failed"
                failure = {**failure, "kind": "declined",
                           "error": "The user declined this command. Do not run commands for this goal: "
                                    "do it through the apps' own windows (open, click, type, set), or stop and "
                                    "explain what you would need."}
            if status == "policy":
                return finish(False, f"I didn't do “{failure['command']}”: {failure.get('error')}", route)

            if status == "failed":
                kind = failure.get("kind") or "error"
                if counters["replans"] >= settings.max_replans and self.system2.step_up(
                        f"The local planner used all {settings.max_replans} re-plans; last failure: "
                        f"{failure.get('error')}"):
                    counters["replans"] = 0
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
                    shown = [r.output for r in records if r.output]
                    reply = _summary_reply(records)
                    if shown:
                        reply += "\n\n" + "\n\n".join(shown)
                    return finish(True, reply, route)
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
            screen_now = self._screen_summary(screen_kind)
            try:
                plan = self.system2.plan(self.goal, screen=screen_now, completed=completed,
                                         failure=failure, round_no=counters["rounds"] + 1, agent=self.agent)
            except System2Unavailable as e:
                return finish(False, f"I couldn't decide the next step: {e}", route)
            counters["rounds"] += 1
            if plan.next_agent and self._hand_off(plan) and not plan.subtasks and not plan.done:
                items = []
                last_batch, last_screen = (), None
                continue
            if failure and plan.subtasks and " ".join(plan.subtasks[0].split()).lower() ==                     " ".join(str(failure.get("command") or "").split()).lower():
                # The planner sent the step that just failed, unchanged: it would fail the same way.
                tr.emit("decision", "Planner repeated the failed step · not run", plan.subtasks[0],
                        tier="system2", status="warn")
                repeated = {**failure, "kind": "reasoning",
                            "error": f"You sent “{plan.subtasks[0]}” again unchanged after it failed with: "
                                     f"{failure.get('error')}. Use a different control, step form or route."}
                continue
            if plan.done:
                n = len(records)
                return finish(True, plan.reply or f"Done — {n} step{'s' if n != 1 else ''} completed.", route)
            if not plan.subtasks:
                return finish(False, plan.reply or "I'm not sure what to do next.", route)
            batch = tuple(plan.subtasks)
            # Repeating a step is fine while it makes progress ("scroll down" twice on a long
            # page). Stop only when the same steps come back AND the screen didn't change.
            counters["still"] = counters["still"] + 1 if screen_now == last_screen else 0
            stuck = (batch == last_batch and failure is None and screen_now == last_screen) or counters["still"] >= 3
            if stuck:
                why = ("it proposed the same steps again and the screen did not change"
                       if batch == last_batch else "the screen has not changed for 3 rounds")
                if self.system2.step_up(f"The local planner is stuck: {why}"):
                    counters["still"] = 0
                    last_batch, last_screen = (), None
                    items = []  # nothing to run: go straight to a fresh plan from the cloud tier
                    continue
                return finish(False, f"I stopped: {why}. " + (plan.reply or ""), route)
            last_batch, last_screen = batch, screen_now
            items = list(plan.subtasks)

    # ============================================================ for MCP callers

    def screen(self) -> Tuple[str, Dict[int, Dict[str, Any]]]:
        """The numbered screen description planners see, and what each number stands for
        (pass it back to run_step: control numbers only mean something for this screen)."""
        text = self._screen_summary()
        return text, dict(self._numbered)

    def run_step(self, step_text: str, numbered: Optional[Dict[int, Dict[str, Any]]] = None) -> RunOutcome:
        """Run ONE step form written by an outside planner (an MCP caller): the Needle agent
        for its tool, check_call, then the same execution, gate and verification as any
        planner step. Every specialist is allowed; the Laya risk gate and the confirmation
        callback still apply to each action."""
        t0 = time.perf_counter()
        tr = self.tracer
        self.goal = step_text.strip()
        tr.emit("goal", self.goal, tier="mcp")
        self._numbered = dict(numbered or {})
        self._outside_planner = True
        records: List[StepRecord] = []
        status, failure = self._run_batch([self.goal], [], records, round_no=1)
        if status == "ok":
            outs = [r.output for r in records if r.output]
            reply = "Done." + ("\n" + "\n".join(outs) if outs else "")
        else:
            reply = f"{failure.get('kind') or status}: {failure.get('error') or status}"
        out = RunOutcome(run_id=tr.run_id, goal=self.goal, success=status == "ok", reply=reply, route="mcp-step",
                         steps=records, aborted=status == "abort",
                         latency_ms=int((time.perf_counter() - t0) * 1000))
        tr.emit("result", reply, tier="mcp", status="ok" if out.success else "fail", duration_ms=out.latency_ms,
                route="mcp-step", steps=len(records))
        try:
            from servo.telemetry.run_record import build_record, save_run
            save_run(build_record(out, tr.events, {"source": "mcp"}))
        except Exception as e:  # noqa: BLE001
            tr.emit("error", "Run record not saved", f"{e.__class__.__name__}: {e}", tier="mcp", status="warn")
        return out

    def _hand_off(self, plan: Any) -> bool:
        """The planner asked to hand the task to another agent. Returns whether it switched."""
        nxt = plan.next_agent
        if nxt not in PLANNER_AGENTS or nxt == self.agent or self._handoffs >= 3:
            return False
        self._handoffs += 1
        self.tracer.emit("route", f"Planner agent · {self.agent} → {nxt}",
                         plan.thought or f"The task moved to {PLANNER_AGENTS[nxt].title}.", tier="system2", status="ok")
        self.agent = nxt
        return True

    def _run_batch(
        self, items: List[Union[str, PlanStep]], completed: List[str], records: List["StepRecord"], round_no: int,
    ) -> Tuple[str, Dict[str, Any]]:
        """Run one batch of subtasks. Returns ("ok"|"failed"|"policy"|"abort", failure)."""
        tr = self.tracer
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
                allowed = None if self._outside_planner else planner_agent(self.agent).specialists
                prop = self.tier1.propose(item, planner_step=True, allowed=allowed)
                if prop is None:
                    why = self.tier1.last_error or "Needle could not interpret this step"
                    progress(i, "fail", why)
                    return "failed", {"command": item, "kind": "reasoning",
                                      "error": f"{why}. Write the step as one plain action on a numbered control, "
                                               "e.g. “click [12]”, “type \"hello\" into [4]”, “set \"03\" in [7]”."}
                steps = [self._ground(s) for s in prop.steps]
            else:
                steps = [item]

            skipped = False
            outputs: List[str] = []
            unconfirmed: List[str] = []
            for step in steps:
                if self.abort_event.is_set():
                    progress(i, "fail", "stopped")
                    return "abort", {"command": command}
                t_step = time.perf_counter()
                with tr.span("tier", f"Step · {describe_step(step)}", tier="router", command=command) as sp:
                    out = self._execute(step)
                    sp.done("ok" if out.success else "fail", out.error or "", tier=out.tier)
                records.append(StepRecord(command=command, tool=step.tool_name, call=describe_step(step),
                                          tier=out.tier, success=out.success,
                                          error=out.error, latency_ms=int((time.perf_counter() - t_step) * 1000),
                                          output=out.output,
                                          verification=out.verification if out.success else "failed", note=out.note,
                                          target=str(step.arguments.get("target") or ""), app=self._app_name(),
                                          page=self.title or ""))
                if out.success:
                    if out.output:
                        outputs.append(out.output)
                    if out.verification == "unconfirmed":
                        unconfirmed.append(out.note or "effect not visible")
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
                           "error": out.error, "tool": step.tool_name, **out.detail}
                return ("abort" if kind == "abort" else "policy" if kind == "policy" else "failed"), failure

            if skipped:
                progress(i, "skipped", "skipped by you")
                completed.append(f"(skipped by the user) {command}")
            elif unconfirmed:
                progress(i, "unconfirmed", "; ".join(unconfirmed))
                # System 2 must check the screen rather than assume this worked.
                completed.append(command + f" [ran, NOT confirmed: {'; '.join(unconfirmed)}]"
                                 + "".join(f"\n  output:\n{_indent(o)}" for o in outputs))
            else:
                progress(i, "ok")
                # System 2 sees what a command printed or a file contains, so it can act on it.
                completed.append(command + "".join(f"\n  output:\n{_indent(o)}" for o in outputs))
        return "ok", {}

    # ------------------------------------------------------------ asking you

    def _ask(self, kind: str, payload: Dict[str, Any], options: List[str], default: str,
             timeout: Optional[float] = -1) -> str:
        """Ask the user through the copilot's buttons; ``default`` when nobody can answer."""
        if self.ask is None:
            return default
        try:
            return self.ask(kind, payload, options, timeout) or default
        except Exception as e:  # noqa: BLE001
            self.tracer.emit("error", f"Couldn't ask you ({kind})", f"{e.__class__.__name__}: {e}; using “{default}”",
                             tier="copilot", status="warn")
            return default

    def _confirm(self, req: ConfirmationRequest) -> bool:
        """Risky action: Proceed / Skip this step / Stop."""
        if self.confirmation_callback is not None:
            try:
                approved = bool(self.confirmation_callback(req))
            except Exception as e:  # noqa: BLE001
                self.tracer.emit("error", "Confirmation failed · not running the step", f"{e.__class__.__name__}: {e}",
                                 tier="laya", status="fail")
                return False
            if not approved and self._skip_requested:
                self.tracer.emit("decision", "You skipped this step", req.tool_call, tier="laya", status="warn")
            return approved
        if self.ask is not None:
            choice = self._ask("confirm", req.model_dump(), ["proceed", "skip", "stop"], default="stop")
            if choice == "skip":
                self._skip_requested = True
                self.tracer.emit("decision", "You skipped this step", req.tool_call, tier="laya", status="warn")
                return False
            if choice == "stop":
                self.abort_event.set()
                return False
            return choice == "proceed"
        return False

    # ============================================================ execution

    def _other_window_titled(self, title: str, exact: bool = False) -> Optional[int]:
        """An open window (not Servo's, not the current one) with this title."""
        t = " ".join(title.lower().split())
        if len(t) < 3:
            return None
        own = self.exclude_hwnds() if self.exclude_hwnds else set()
        best = None
        for w in windows.list_windows():
            h, wt = int(w["hwnd"]), " ".join(str(w.get("title") or "").lower().split())
            if h in own or h == self.hwnd or "servo copilot" in wt or wt in ("program manager", ""):
                continue
            if wt == t:
                return h
            if not exact and best is None and t in wt:
                best = h
        return best

    def _focus_window(self, title: str, step: PlanStep) -> StepOutcome:
        """Bring an already-open window to the front and work in it from now on."""
        call = describe_step(step)
        h = self._other_window_titled(title)
        if h is None and self.hwnd and title.lower() in windows.get_window_title(self.hwnd).lower():
            h = self.hwnd
        if h is None:
            titles = [w.get("title") for w in windows.list_windows()[:12] if w.get("title")]
            return StepOutcome(success=False, kind="perception", tier="uia",
                               error=f"No open window titled '{title}'. Open windows: {', '.join(map(str, titles))}")
        from servo.actuation.sandboxing import activate_window
        ok = activate_window(h)
        self.hwnd, self.title, self.context = h, windows.get_window_title(h), "desktop"
        self.tracer.emit("action", call, f"Now working in “{self.title}”", tier="uia", status="ok" if ok else "warn")
        return StepOutcome(success=True, tier="uia", verification="confirmed" if ok else "unconfirmed",
                           note=None if ok else f"“{self.title}” was found but Windows kept it in the background")

    def _refresh_target(self) -> None:
        """If the window we were working in closed, continue in whatever is now in front.

        Before, a closed Explorer window kept being sensed ("Invalid handle passed to
        connect()") for the rest of the run, and the planner saw no screen at all.
        """
        if sys.platform != "win32" or not self.hwnd or windows.is_window(self.hwnd):
            return
        try:
            fg = windows.get_foreground_window()
            own = self.exclude_hwnds() if self.exclude_hwnds else set()
            if fg and windows.is_window(fg) and fg not in own:
                old = self.title
                self.hwnd, self.title = fg, windows.get_window_title(fg)
                self.tracer.emit("perception", f"Window closed · now in “{self.title}”",
                                 f"“{old}” is gone; continuing in the window in front.", tier="uia", status="warn")
            else:
                self.hwnd = None
        except Exception:
            pass

    def _execute(self, step: PlanStep) -> StepOutcome:
        self._refresh_target()
        tool = step.tool_name
        if tool == "focus_window":
            return self._focus_window(str(step.arguments.get("title") or ""), step)
        if tool == "describe_screen":
            return self._describe_screen(step)
        if tool == "read_text":
            return self._read_text(step)
        if tool in system_tools.SYSTEM_TOOLS:
            return self._system(step)
        if tool in BROWSER_TOOLS:
            return self._user_browser_nav(step)
        if tool in _DIRECT_TOOLS:
            return self._direct(step)
        return self._desktop(step)

    # ---------------------------------------------------------- direct (Needle requests)

    def _direct(self, step: PlanStep) -> StepOutcome:
        """Actions without a target control (servo/cognition/intents.py request_steps): the
        speaker, media keys, a window's size, a browser tab shortcut, opening a file. Each is
        gated like any step and reports what it read back."""
        tool, a = step.tool_name, step.arguments
        call = describe_step(step)
        stop = self._gate(step.rationale or call, call, str(a.get("name") or a.get("path") or a.get("op") or tool),
                          self.title or "", process_name="system")
        if stop:
            return stop
        self.tracer.emit("action", call, step.rationale, tier="system")
        try:
            ok, note = self._direct_run(tool, a)
        except Exception as e:  # noqa: BLE001
            self.tracer.emit("verify", f"{tool} failed", f"{e.__class__.__name__}: {e}", tier="system", status="fail")
            return StepOutcome(success=False, kind="error", tier="system", error=f"{tool} failed: {e}")
        self.tracer.emit("verify", note, "", tier="system", status="ok" if ok else "fail")
        if not ok:
            return StepOutcome(success=False, kind="verify", tier="system", error=note)
        return StepOutcome(success=True, tier="system", verification="confirmed", output=note)

    def _direct_run(self, tool: str, a: Dict[str, Any]) -> Tuple[bool, str]:
        if tool == "audio":
            from servo.actuation import audio
            if a["op"] in ("mute", "unmute"):
                want = a["op"] == "mute"
                got = audio.set_muted(want)
                return got == want, f"Sound is {'muted' if got else 'on'}."
            before = audio.get_volume()
            target = max(0, min(100, before + int(a["change"])))
            after = audio.set_volume(target)
            return after == target, f"Volume {before} → {after}."
        if tool == "media_key":
            from servo.actuation.needle_tools import MEDIA_KEYS
            import ctypes
            vk = MEDIA_KEYS[a["key"]]
            for _ in range(int(a.get("presses") or 1)):
                ctypes.windll.user32.keybd_event(vk, 0, 0, 0)
                ctypes.windll.user32.keybd_event(vk, 0, 2, 0)
            label = {"playpause": "play/pause (toggles whatever is playing)", "nexttrack": "next track",
                     "prevtrack": "previous track"}.get(a["key"], a["key"])
            return True, f"Pressed the {label} media key."
        if tool == "window_state":
            from servo.daemon.copilot import find_app_window
            hwnd = find_app_window(str(a["name"]))
            if not hwnd:
                return False, f"No open window for {a['name']}."
            import ctypes
            user32 = ctypes.windll.user32
            user32.ShowWindow(hwnd, 6 if a["state"] == "minimize" else 3)   # SW_MINIMIZE / SW_MAXIMIZE
            time.sleep(0.3)
            done = bool(user32.IsIconic(hwnd)) if a["state"] == "minimize" else bool(user32.IsZoomed(hwnd))
            title = windows.get_window_title(hwnd)
            return done, f"“{title}” is {a['state']}d." if done else f"“{title}” did not {a['state']}."
        if tool == "browser_hotkey":
            from servo.cognition import user_browser as ub
            from servo.actuation.needle_tools import build_hotkey_sequence
            name, _why = ub.choose_browser("", self.recent_browser)
            launcher = self.app_launcher
            if launcher is None:
                from servo.daemon.copilot import launch_or_focus_app as launcher
            hwnd, title = launcher(name)
            if not hwnd:
                return False, f"Couldn't bring up {name}: {title}"
            before = windows.get_window_title(hwnd)
            from pywinauto.keyboard import send_keys
            send_keys(build_hotkey_sequence(list(a["keys"])))
            time.sleep(0.5)
            self.hwnd, self.title, self.recent_browser = hwnd, windows.get_window_title(hwnd), name
            changed = self.title != before or not windows.is_window(hwnd)
            return True, (f"Pressed {'+'.join(a['keys'])} in {name}; the window now shows “{self.title}”."
                          if changed else f"Pressed {'+'.join(a['keys'])} in {name}; its title did not change.")
        if tool == "open_file":
            import os
            before = windows.get_foreground_window()
            os.startfile(str(a["path"]))  # type: ignore[attr-defined]
            for _ in range(40):
                time.sleep(0.2)
                fg = windows.get_foreground_window()
                if fg and fg != before:
                    self.hwnd, self.title = fg, windows.get_window_title(fg)
                    return True, f"Opened {a['path']} in “{self.title}”."
            return False, f"Asked Windows to open {a['path']}, but no new window came up."
        raise ValueError(f"unknown direct action {tool}")

    # ---------------------------------------------------------- desktop

    def _desktop(self, step: PlanStep) -> StepOutcome:
        if step.tool_name in ("launch_app", "close_window"):
            self.context = "desktop"
        if step.tool_name == "launch_app":
            from servo.cognition.user_browser import is_browser_name
            app = str(step.arguments.get("app_name") or "")
            if is_browser_name(app):
                self.recent_browser = app.lower()
        coord = SystemHandoffCoordinator(
            target_hwnd=self.hwnd,
            fast_mode=settings.fast_mode,
            confirmation_callback=self._confirm,
            should_abort=self.abort_event,
            tracer=self.tracer,
            exclude_hwnds=self.exclude_hwnds,
        )
        summary = coord.execute_plan(MacroPlan(goal=self.goal, steps=[step]), app_launcher=self.app_launcher)
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
                           reports=[rep], verification=rep.verification if rep.success else "failed", note=rep.note)

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

    def _declined_command_can_replan(self, failure: Dict[str, Any], counters: Dict[str, int]) -> bool:
        """Only a declined shell command, only once per run, and only with a planner."""
        if failure.get("tool") != "run_command" or "rejected by user" not in str(failure.get("error") or "").lower():
            return False
        self._commands_declined = True
        if counters.get("declined_commands", 0) >= 1 or not self.system2.available():
            return False
        counters["declined_commands"] = counters.get("declined_commands", 0) + 1
        return True

    def _gate(self, sub_goal: str, tool_call: str, label: str, window_title: str,
              floor: Optional[Tuple[str, str]] = None, process_name: str = "browser") -> Optional[StepOutcome]:
        """Laya pre-check + confirmation. Returns a failed outcome to stop, or None to proceed.

        ``floor`` is a deterministic (level, reason) minimum that Laya cannot lower.
        """
        try:
            pre = self.laya.evaluate_pre_risk(sub_goal, tool_call, label)
        except LayaUnavailable as e:
            self.tracer.emit("decision", "Laya pre-check failed", str(e), tier="laya", status="fail")
            return StepOutcome(success=False, kind="policy", tier="laya", error=f"No safety check possible: {e}")
        if floor and _RISK_ORDER[floor[0]] > _RISK_ORDER[pre.risk_level]:
            pre = pre.model_copy(update={"risk_level": floor[0], "reason": f"{floor[1]} (floor; was {pre.risk_level}: {pre.reason})"})
        self.tracer.emit("decision", f"Laya pre-check · {pre.risk_level} risk", pre.reason, tier="laya",
                         status="ok" if pre.risk_level == "low" and pre.scope_valid else "warn",
                         source=pre.source, scope_valid=pre.scope_valid, probabilities=pre.probabilities)
        if not pre.scope_valid:
            return StepOutcome(success=False, kind="policy", tier="laya",
                               error=f"Laya rejected the action as out of scope: {pre.reason}")
        if settings.mode == "assistant" and pre.risk_level in ("medium", "high"):
            approved = self._confirm(ConfirmationRequest(
                sub_goal=sub_goal, tool_call=tool_call, element_label=label, risk_level=pre.risk_level,
                reason=pre.reason, hwnd=0, window_title=window_title, process_name=process_name,
            ))
            self.tracer.emit("decision", f"Your confirmation · {'approved' if approved else 'rejected'}", tool_call,
                             tier="laya", status="ok" if approved else "fail")
            if not approved:
                if self.abort_event.is_set():
                    return StepOutcome(success=False, kind="abort", tier="laya", error="Execution cancelled by user.")
                return StepOutcome(success=False, kind="policy", tier="laya",
                                   error="Action rejected by user confirmation (declined or timed out).")
        if self.abort_event.is_set():
            return StepOutcome(success=False, kind="abort", tier="browser", error="Execution cancelled by user.")
        return None

    # ---------------------------------------------------------- system

    def _system(self, step: PlanStep) -> StepOutcome:
        """Files and shell, without the GUI. Gated like every other action, with a floor."""
        tool, args = step.tool_name, step.arguments
        if tool == "run_command":
            if self._commands_declined:
                return StepOutcome(success=False, kind="reasoning", tier="system",
                                   error="The user already declined a command for this goal. Use the apps' own "
                                         "windows (open, click, type, set) instead of commands.")
        call = system_tools.describe(tool, args)
        why = system_tools.availability()
        if why:
            return StepOutcome(success=False, kind="policy", tier="system", error=why)
        try:
            floor = system_tools.risk_floor(tool, args)
        except Exception as e:  # e.g. an unparsable path
            return StepOutcome(success=False, kind="error", tier="system", error=str(e))
        stop = self._gate(step.rationale or call, call, str(args.get("path") or args.get("command") or ""),
                          "system", floor=floor, process_name="system")
        if stop:
            return stop
        self.tracer.emit("action", call, f"Direct {tool.replace('_', ' ')} (no GUI)", tier="system")
        t0 = time.perf_counter()
        try:
            res = system_tools.execute(tool, args, abort=self.abort_event)
        except system_tools.SystemToolError as e:
            kind = "abort" if self.abort_event.is_set() else "error"
            self.tracer.emit("verify", f"{tool} failed", str(e), tier="system", status="fail")
            return StepOutcome(success=False, kind=kind, tier="system", error=str(e))
        except Exception as e:
            return StepOutcome(success=False, kind="error", tier="system", error=f"{tool} failed: {e}")
        self.tracer.emit("verify", res.summary, res.output[:2000], tier="system", status="ok",
                         duration_ms=int((time.perf_counter() - t0) * 1000))
        return StepOutcome(success=True, tier="system", output=res.output)

    def _user_browser_nav(self, step: PlanStep) -> StepOutcome:
        """open_url / web_search in the user's own browser (see servo/cognition/user_browser.py)."""
        from urllib.parse import quote_plus
        from servo.cognition import user_browser as ub

        args = step.arguments
        if step.tool_name == "open_url":
            from servo.cognition.needle import normalize_url
            url = normalize_url(str(args.get("url") or ""))
        else:
            url = settings.search_url.format(q=quote_plus(str(args.get("query") or "")))
        call = describe_step(step)
        problem = ub.url_problem(url, self.goal)
        if problem:
            self.tracer.emit("decision", "Address refused", problem, tier="laya", status="fail")
            return StepOutcome(success=False, kind="policy", tier="browser", error=problem)

        name, why = ub.choose_browser(str(args.get("browser") or ""), self.recent_browser)
        stop = self._gate(step.rationale or call, call, url, name, process_name=ub.browser_process(name) or name)
        if stop:
            return stop
        self.tracer.emit("route", f"Your browser · {name}", why, tier="browser")
        launcher = self.app_launcher
        if launcher is None:
            from servo.daemon.copilot import launch_or_focus_app as launcher
        hwnd, title = launcher(name)
        own = self.exclude_hwnds() if self.exclude_hwnds else set()
        if not hwnd or hwnd in own or "servo copilot" in str(title).lower():
            return StepOutcome(success=False, kind="perception", tier="browser",
                               error=f"Couldn't open {name}: {title}")
        self.tracer.emit("action", call, f"New tab in {name}, type the address, Enter", tier="browser")
        t0 = time.perf_counter()
        try:
            verdict, page_title, note = ub.navigate(hwnd, url)
        except Exception as e:
            return StepOutcome(success=False, kind="error", tier="browser", error=f"Navigation in {name} failed: {e}")
        fg = windows.get_foreground_window()
        self.hwnd = fg if fg and windows.get_process_name(fg) == windows.get_process_name(hwnd) else hwnd
        self.title = page_title
        self.context = "desktop"   # the page is now a window driven through UI Automation
        self.recent_browser = name
        status = {"confirmed": "ok", "unconfirmed": "warn", "failed": "fail"}[verdict]
        self.tracer.emit("verify", f"Page · {page_title}", note or f"Address bar shows {url}", tier="browser",
                         status=status, verdict=verdict, duration_ms=int((time.perf_counter() - t0) * 1000))
        if verdict == "failed":
            return StepOutcome(success=False, kind="verify", tier="browser", error=note)
        return StepOutcome(success=True, tier="browser", verification=verdict, note=note or None)

    # ---------------------------------------------------------- context

    def _screen_summary(self, failure_kind: Optional[str] = None) -> str:
        """What System 2 sees: the window's numbered controls (+ the OCR text after a perception miss)."""
        self._refresh_target()
        parts: List[str] = []
        try:
            if self.hwnd and windows.is_window(self.hwnd):
                from servo.perception.uia_sensor import UIASensor
                scene = UIASensor(self.hwnd).sense()
                self._numbered = {}
                parts.append(describe_scene(scene, app_identity.window_label(
                    self.hwnd, windows.get_process_name(self.hwnd)), numbered=self._numbered))
                self._observe_page(scene)
                if failure_kind == "perception":
                    parts.append(self._ocr_text())
            wins = [w.get("title") for w in windows.list_windows()[:12] if w.get("title")]
            if wins:
                parts.append("Open windows: " + "; ".join(str(w) for w in wins))
        except Exception as e:
            parts.append(f"(screen description unavailable: {e})")
        text = "\n\n".join(parts)
        self.tracer.emit("perception", "Screen description for System 2", text[:400] + ("…" if len(text) > 400 else ""),
                         tier="system2", full=text)
        return text

    def _ocr_text(self) -> str:
        """After a perception miss, the planner also gets the text on screen (Windows OCR):
        it shows labels the accessibility tree does not expose."""
        from servo.perception.mirage_sensor import MirageSensor
        from servo.perception.ocr_sensor import OcrUnavailable, read_lines
        img = MirageSensor(self.hwnd).capture_window_image()
        if img is None:
            return "Text on screen (OCR): could not capture the window"
        try:
            lines, _ = read_lines(img)
        except OcrUnavailable as e:
            return f"Text on screen (OCR) failed: {e}"
        return "Text on screen (OCR):\n" + "\n".join(d.label for d in lines[:80])

    def _no_target(self) -> str:
        """No window is targeted: say how to pick one, with the titles to pick from."""
        own = self.exclude_hwnds() if self.exclude_hwnds else set()
        titles = [str(w["title"]) for w in windows.list_windows() if w.get("title") and w["hwnd"] not in own][:10]
        return ('No window is targeted yet. Start with switch to "<exact title>"; open windows: '
                + "; ".join(f'"{t}"' for t in titles) + ".")

    def _read_text(self, step: PlanStep) -> StepOutcome:
        """All text visible in the window (Windows OCR), returned to the planner as output."""
        if not self.hwnd or not windows.is_window(self.hwnd):
            return StepOutcome(success=False, kind="reasoning", tier="ocr", error=self._no_target())
        text = self._ocr_text()
        failed = "failed" in text.split(":", 1)[0] or "could not capture" in text
        self.tracer.emit("perception", "OCR · text on screen", text[:400], tier="ocr",
                         status="fail" if failed else "ok")
        if failed:
            return StepOutcome(success=False, kind="perception", tier="ocr", error=text)
        body = text.split("\n", 1)[-1]
        return StepOutcome(success=True, tier="ocr", output=f"Text in “{self.title}”:\n{body}")

    def _describe_screen(self, step: PlanStep) -> StepOutcome:
        """Mirage's summary of the window, returned to the planner as output."""
        from servo.perception.mirage_sensor import MirageSensor, summarize
        if not self.hwnd or not windows.is_window(self.hwnd):
            return StepOutcome(success=False, kind="reasoning", tier="mirage", error=self._no_target())
        ms = MirageSensor(self.hwnd)
        scene = ms.sense(window_title=self.title)
        if scene.sense_error:
            self.tracer.emit("perception", "Mirage failed", scene.sense_error, tier="mirage", status="fail")
            return StepOutcome(success=False, kind="perception", tier="mirage", error=scene.sense_error)
        text = summarize(ms.last_detections)
        self.tracer.emit("perception", f"Mirage · {len(ms.last_detections)} detections", text[:400], tier="mirage")
        return StepOutcome(success=True, tier="mirage", output=f"Mirage sees in “{self.title}”:\n{text}")

    # ------------------------------------------------------- desktop knowledge graph

    def _app_name(self) -> str:
        try:
            if self.hwnd and windows.is_window(self.hwnd):
                return app_identity.window_label(self.hwnd, windows.get_process_name(self.hwnd)).split(" — ")[0]
        except Exception:
            pass
        return ""

    def _observe_page(self, scene: Any) -> None:
        """Every UIA snapshot the planner sees also teaches the graph that page's controls."""
        try:
            from servo.cognition.desktop_graph import desktop_graph
            b = None
            if sys.platform == "win32" and self.hwnd:
                import win32gui
                b = win32gui.GetWindowRect(self.hwnd)
            controls = [{"label": e.label, "role": e.role, "context": getattr(e, "context", None),
                         "bbox": e.bbox_screen, "toggle": getattr(e, "toggle", None),
                         "selected": getattr(e, "selected", None), "expanded": getattr(e, "expanded", None),
                         "value": e.value if e.role in ("ComboBox", "Slider", "Spinner", "Custom") else None}
                        for e in scene.elements.values() if getattr(e, "interactive", True)]
            desktop_graph().observe_page(self._app_name() or "unknown", scene.window_title, controls,
                                         window_box=tuple(b) if b else None)
        except Exception as e:  # noqa: BLE001 - learning must never break a run, but say so
            self.tracer.emit("error", "Desktop graph: could not record this page", str(e), tier="router", status="warn")

    def _remember_run(self, out: "RunOutcome") -> None:
        """Verified runs become procedures; Laya-failed steps become failures."""
        try:
            from servo.cognition.desktop_graph import desktop_graph
            steps = []
            for r in out.steps:
                m = re.match(r"^([A-Za-z]+):\s*(.+?)(?:\s+\((.+)\))?$", r.target or "")
                steps.append({"call": r.call, "tool": r.tool, "verification": r.verification, "error": r.error,
                              "app": r.app, "page": r.page,
                              "target_role": m.group(1) if m else "", "target_label": m.group(2) if m else r.target,
                              "target_context": (m.group(3) or "") if m else ""})
            desktop_graph().record_run(out.goal, steps, out.success)
        except Exception as e:  # noqa: BLE001
            self.tracer.emit("error", "Desktop graph: could not record this run", str(e), tier="router", status="warn")

    _NUMBER_REF = re.compile(r"^\s*\[?#?\s*(\d{1,3})\s*\]?\s*$")

    def _ground(self, step: PlanStep) -> PlanStep:
        """A target given as a control number ("17", "[17]") becomes that exact control from
        the screen description the planner saw: "Role: label (context)", which the resolver
        matches against the live screen. An unknown number is left as is and fails there."""
        target = step.arguments.get("target")
        m = self._NUMBER_REF.match(str(target)) if target is not None else None
        if not m or int(m.group(1)) not in self._numbered:
            return step
        ref = self._numbered[int(m.group(1))]
        label = f"{ref['role']}: {ref['label']}" + (f" ({ref['context']})" if ref.get("context") else "")
        args = {**step.arguments, "target": label}
        canon = None
        try:
            canon = AliasMapper.canonicalize_action(raw_action=step.tool_name, **args)
        except Exception:
            canon = step.canonical_action
        self.tracer.emit("perception", f"[{m.group(1)}] → {label}", "control number from the last screen description",
                         tier="router")
        return step.model_copy(update={"arguments": args, "canonical_action": canon})

    def _chat(self) -> str:
        """System 2's chat answer. Raises System2Unavailable; the caller reports the failure."""
        return self.system2.chat(self.goal, self.history).reply or "…"

    def _no_system2_message(self) -> str:
        return (f"The planner (System 2) is not available: {self.system2.unavailable_reason()}. "
                "Start the local planner server or configure the cloud planner.")


def new_run_id() -> str:
    return uuid.uuid4().hex[:12]
