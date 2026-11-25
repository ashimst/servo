"""Tier 1: Cactus Needle turns one step into a tool call.

One Needle agent per tool (servo/cognition/specialists.py): the step's form picks the
tool, and Needle fills in the arguments. check_call then compares the call with the
step's own words (control number, quoted text, path, keys, seconds) before it runs.

There is no rule-based stand-in: when Needle is not installed, proposes nothing, or its
call doesn't match the step, the proposal says so and the caller treats the step as not
understood.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field

from servo.actuation.alias_mapping import AliasMapper
from servo.actuation.system_tools import SYSTEM_TOOLS
from servo.cognition import cactus_runtime
from servo.cognition.specialists import form_hint, SPECIALISTS, TOOLS, StepForm, all_tool_schemas, step_form
from servo.cognition.trace import NullTracer, Tracer
from servo.config import settings
from servo.cognition.plan import MacroPlan, PlanStep

BROWSER_TOOLS = ("open_url", "web_search")


# Needle's tools live with the specialists that use them (servo/cognition/specialists.py).
NEEDLE_TOOLS: List[Dict[str, Any]] = all_tool_schemas()

_URLISH = re.compile(r"^(?:https?://\S+|(?:www\.)?[a-z0-9-]+(?:\.[a-z0-9-]+)+(?:/\S*)?)$", re.I)


class Tier1Proposal(BaseModel):
    steps: List[PlanStep]
    confidence: float
    source: str = "needle"
    reasoning: str = ""
    raw: Dict[str, Any] = Field(default_factory=dict)


class NeedleError(RuntimeError):
    """Needle could not turn the instruction into a confident call."""


def _step(i: int, tool: str, args: Dict[str, Any], why: str) -> PlanStep:
    canon = None
    if tool not in BROWSER_TOOLS and tool not in SYSTEM_TOOLS:
        try:
            canon = AliasMapper.canonicalize_action(raw_action=tool, **args)
        except Exception:
            canon = None
    return PlanStep(step_index=i, tool_name=tool, arguments=args, rationale=why, canonical_action=canon)


def _keys(s: str) -> List[str]:
    """'ctrl+shift+t' -> ['ctrl', 'shift', 't']; a '+' that is itself the key stays ('ctrl++')."""
    s = " ".join(str(s or "").split()).lower()
    out, cur = [], ""
    for ch in s.replace(" + ", "+"):
        if ch == "+" and cur.strip():
            out.append(cur.strip()); cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur.strip())
    return out


def to_steps(name: str, args: Dict[str, Any], start: int = 1) -> List[PlanStep]:
    """One specialist's tool call -> Servo executor step(s). Raises NeedleError for an unusable call."""
    a = {k: v for k, v in (args or {}).items() if v not in (None, "")}
    why = f"Needle: {name}"

    def need(key: str) -> str:
        v = str(a.get(key, "")).strip()
        if not v:
            raise NeedleError(f"{name}() without {key}")
        return v

    if name in ("click", "double_click", "right_click"):
        tool = {"click": "click_element"}.get(name, name)
        return [_step(start, tool, {"target": need("control")}, why)]
    if name in ("scroll_up", "scroll_down"):
        out = {"direction": name[7:], "amount": 5}
        if a.get("control") is not None:
            out["target"] = str(a["control"])
        return [_step(start, "scroll", out, why)]
    if name in ("type_into", "type_text"):
        out = {"text": need("text"), "target": need("control") if name == "type_into" else "editor"}
        if name == "type_into":
            out["clear_first"] = True   # "type X into [n]" leaves the field reading X
        if a.get("press_enter"):
            out["press_enter"] = True
        return [_step(start, "type_text", out, why)]
    if name == "press":
        keys = _keys(str(a.get("keys", "")))
        if not keys:
            raise NeedleError("press() without keys")
        return [_step(start, "send_hotkey", {"keys": keys}, why)]
    if name == "clear":
        return [_step(start, "set_value", {"target": need("control"), "value": ""}, why)]
    if name in ("turn_on", "turn_off"):
        return [_step(start, "set_toggle", {"target": need("control"), "state": name[5:]}, why)]
    if name == "set_value":
        return [_step(start, "set_value", {"target": need("control"), "value": need("value")}, why)]
    if name == "choose":
        return [_step(start, "select_option", {"target": need("control"), "option": need("option")}, why)]
    if name in ("expand", "collapse"):
        return [_step(start, "set_expanded", {"target": need("control"), "state": name}, why)]
    if name == "open_app":
        return [_step(start, "launch_app", {"app_name": need("name")}, why)]
    if name == "open_settings":
        page = need("page")
        page = page if page.lower().startswith("ms-settings:") else f"ms-settings:{page}"
        return [_step(start, "launch_app", {"app_name": page}, why)]
    if name == "close_window":
        return [_step(start, "close_window", {"target": str(a.get("window", ""))}, why)]
    if name == "switch_to":
        return [_step(start, "focus_window", {"title": need("window")}, why)]
    if name == "wait":
        try:
            secs = float(a.get("seconds"))
        except (TypeError, ValueError):
            raise NeedleError("wait() without seconds") from None
        return [_step(start, "wait", {"duration_seconds": secs}, why)]
    if name == "open_web":
        return [_step(start, "open_url", {"url": normalize_url(need("address"))}, why)]
    if name == "search_web":
        return [_step(start, "web_search", {"query": need("query")}, why)]
    if name == "read_file":
        return [_step(start, "read_file", {"path": need("path")}, why)]
    if name == "list_folder":
        return [_step(start, "list_folder", {"path": need("path")}, why)]
    if name in ("write_file", "append_file"):
        return [_step(start, "write_file", {"path": need("path"), "text": str(a.get("text", "")),
                                            "append": name == "append_file"}, why)]
    if name == "run_command":
        return [_step(start, "run_command", {"command": need("command"), "shell": "powershell"}, why)]
    if name in ("describe_screen", "read_text"):
        return [_step(start, name, {}, why)]
    raise NeedleError(f"Needle called an unknown tool '{name}'")


_STEP_NUMBER = re.compile(r"\[(\d{1,3})\]")
_ARG_NUMBER = re.compile(r"^\s*\[?#?\s*(\d{1,3})\s*\]?\s*$")
_QUOTED = re.compile(r"[\"“”]([^\"“”]+)[\"“”]")
_CONTROL_TOOLS = {"click", "double_click", "right_click", "scroll_up", "scroll_down", "type_into", "clear",
                  "turn_on", "turn_off", "set_value", "choose", "expand", "collapse"}
_QUOTED_FIELD = {"type_into": "text", "type_text": "text", "choose": "option", "set_value": "value",
                 "write_file": "text", "append_file": "text", "run_command": "command"}
_PATH_TOOLS = {"read_file", "list_folder", "write_file", "append_file"}


def check_call(step_text: str, name: str, args: Dict[str, Any], form: Optional[StepForm] = None) -> Optional[str]:
    """Does Needle's call do what the planner's step says? None if it does, else the mismatch.

    Needle's own confidence is not calibrated for this input (measured: correct calls at
    0.18-0.50, a wrong "wait 2" -> close() at 0.28), so a step is accepted when the call
    matches it. Deterministic, and only about the step's own words: the step's form fixes
    the tool, a control number in the step must be the call's control (and the call may not
    name a number the step doesn't), quoted text must be what is typed, set, chosen, written
    or run, a file step's path must be the step's, and numbers like wait seconds must agree."""
    text = " ".join((step_text or "").split())
    low = text.lower()
    form = form or step_form(text)
    args = {k: v for k, v in (args or {}).items() if v not in (None, "")}
    if form is not None and name != form.tool:
        return f"the step says “{' '.join(text.split()[:2])}” ({form.tool}) but Needle called {name}()"
    numbers = _STEP_NUMBER.findall(text)
    if name in _CONTROL_TOOLS:
        control = str(args.get("control", ""))
        m = _ARG_NUMBER.match(control)
        if numbers:
            if not m or str(int(m.group(1))) not in [str(int(n)) for n in numbers]:
                return f"the step acts on [{numbers[0]}] but Needle's control is {args.get('control')!r}"
        elif m:
            return f"Needle chose control [{m.group(1)}], which the step doesn't mention"
    quoted = _QUOTED.findall(text)
    field = _QUOTED_FIELD.get(name)
    if field:
        if not quoted:
            return f"the step has no quoted text for {name}'s {field}"
        if str(args.get(field, "")).strip() != quoted[0].strip():
            return f"the step's text is “{quoted[0]}” but Needle's {field} is {args.get(field)!r}"
    if name in ("switch_to", "close_window") and quoted:
        if str(args.get("window", "")).strip() != quoted[0].strip():
            return f"the step's window is “{quoted[0]}” but Needle's is {args.get('window')!r}"
    if name in _PATH_TOOLS:
        # The step's own path: after "to file" for write/append, after the verb otherwise.
        m = re.search(r"\bto file\s+(.+)$", text, re.I) if name in ("write_file", "append_file") else \
            re.match(r"^(?:read file|list folder)\s+(.+)$", text, re.I)
        want = m.group(1).strip().strip("\"“”") if m else ""
        path = str(args.get("path", "")).strip().strip("\"“”")
        if not path or path.lower() != want.lower():
            return f"Needle's path {args.get('path')!r} is not the step's path {want!r}"
    if name == "press":
        m = re.match(r"^press\s+(.+)$", text, re.I)
        want = _keys(m.group(1)) if m else []
        if want and _keys(str(args.get("keys", ""))) != want:
            return f"the step presses {'+'.join(want)} but Needle's keys are {args.get('keys')!r}"
    if name == "wait":
        n = re.search(r"\d+(?:\.\d+)?", low)
        try:
            got = float(args.get("seconds"))
        except (TypeError, ValueError):
            got = None
        if n and got != float(n.group(0)):
            return f"the step waits {n.group(0)} s but Needle gave {args.get('seconds')!r}"
    return None


def normalize_url(u: str) -> str:
    u = u.strip().strip("'\"").rstrip(".")
    # Keep any address that already names its scheme (chrome://history, about:blank,
    # file:///C:/...); only a bare host like github.com gets https://.
    if not re.match(r"^([a-z][a-z0-9+.\-]*://|(about|chrome|edge|file|mailto):)", u, re.I):
        u = "https://" + u
    return u


def known_folder(name: str) -> str:
    """Real path of Desktop / Documents / Downloads (follows OneDrive redirection)."""
    name = name.lower()
    reg = {"desktop": "Desktop", "documents": "Personal", "downloads": "{374DE290-123F-4565-9164-39C4925E467B}"}[name]
    if sys.platform == "win32":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as k:
                val, _ = winreg.QueryValueEx(k, reg)
                path = os.path.expandvars(val)
                if os.path.isdir(path):
                    return path
        except OSError:
            pass
    return os.path.join(os.path.expanduser("~"), name.capitalize())


class Tier1Intent:
    """Cactus Needle specialists, and nothing else (see servo/cognition/specialists.py)."""

    def __init__(self, user_name: str = "User", tracer: Optional[Tracer] = None) -> None:
        self.user_name = user_name
        self.tracer = tracer or NullTracer()
        self.last_error: Optional[str] = None

    @staticmethod
    def _agent(tool: str, specialist: str) -> Any:
        """The Needle agent for one tool: only that tool, with its interaction type's instructions."""
        return cactus_runtime.get_needle([TOOLS[tool]], key=f"tool:{tool}", system=SPECIALISTS[specialist].instructions)

    def propose(self, text: str, planner_step: bool = False,
                allowed: Optional[Tuple[str, ...]] = None) -> Optional[Tier1Proposal]:
        """The calls for ``text`` from the specialist its form names; None (with ``last_error``)
        when there is no such form, the specialist is not ``allowed``, or the call is unusable.

        Every call must match the step (``check_call``). For a user's own request the
        confidence threshold also decides whether Needle may act alone (below it the request
        goes to the planner); for a planner step it is only traced: Needle's confidence is
        not calibrated for steps (correct calls measured at 0.18-0.50)."""
        self.last_error = None
        tr = self.tracer
        form = step_form(text)
        if form is None:
            hint = form_hint(text)
            self.last_error = ("the step is not one of the step forms"
                               + (f"; this one is written: {hint}" if hint else ""))
            return None
        if allowed is not None and form.specialist not in allowed:
            self.last_error = (f"“{form.example.split('   ')[0]}” steps ({form.specialist}) are not available "
                               "to this planner")
            return None
        spec = SPECIALISTS[form.specialist]
        with tr.span("tier", f"Needle {spec.name}.{form.tool} · “{_clip(text)}”", tier="needle",
                     specialist=spec.name, tool=form.tool) as sp:
            agent = self._agent(form.tool, spec.name)
            if agent is None:
                self.last_error = f"Cactus Needle is unavailable: {cactus_runtime.needle_error() or 'not installed'}"
                sp.done("fail", self.last_error)
                return None
            t0 = time.perf_counter()
            resp = cactus_runtime.needle_complete(agent, text)
            ms = int((time.perf_counter() - t0) * 1000)
            if not resp:
                self.last_error = "Needle returned nothing usable"
                sp.done("fail", self.last_error, latency_ms=ms)
                return None
            calls = resp.get("function_calls") or []
            conf = resp.get("confidence")
            conf_f = float(conf) if isinstance(conf, (int, float)) else 0.0
            tr.emit("model", f"Cactus Needle 3 · {spec.name}", resp.get("reasoning") or "", tier="needle",
                    confidence=conf_f, duration_ms=ms, response=resp, status="ok" if calls else "warn")
            if not calls:
                self.last_error = f"Needle ({spec.name}) found no tool for this step"
                sp.done("warn", self.last_error, confidence=conf_f)
                return None
            # A step is one action. Needle sometimes adds a stray second call (measured:
            # "clear [4]" → clear(4) and clear(44)); exactly one call must match the step,
            # and the others are discarded with their mismatch in the trace.
            checked = [(c, check_call(text, str(c.get("name")), dict(c.get("arguments") or {}), form)) for c in calls]
            matching = list({json.dumps(c, sort_keys=True, default=str): c
                             for c, problem in checked if problem is None}.values())
            if len(matching) != 1:
                problems = "; ".join(p for _, p in checked if p) or "every call matched"
                self.last_error = (f"Needle's call doesn't match the step: {problems}" if not matching else
                                   f"Needle ({spec.name}) made {len(matching)} different calls that fit one step")
                sp.done("fail", self.last_error, confidence=conf_f)
                return None
            call = matching[0]
            if len(calls) > 1:
                tr.emit("decision", f"Needle made {len(calls)} calls · kept the one that matches the step",
                        "; ".join(f"{c.get('name')}({c.get('arguments')}): {p}" for c, p in checked if p),
                        tier="needle", status="warn")
            try:
                steps = to_steps(str(call.get("name")), dict(call.get("arguments") or {}))
            except NeedleError as e:
                self.last_error = str(e)
                sp.done("fail", self.last_error, confidence=conf_f)
                return None
            calls_txt = ", ".join(describe_step(s) for s in steps)
            if planner_step:
                sp.done("ok", f"Needle {spec.name} → {calls_txt} (matches the step; confidence {conf_f:.2f})",
                        confidence=conf_f)
            else:
                thr = settings.needle_confidence_threshold
                if conf_f < thr:
                    self.last_error = f"Needle is not confident ({conf_f:.2f} < {thr:.2f}) about {calls_txt}"
                sp.done("ok" if conf_f >= thr else "warn",
                        f"Needle {spec.name} → {calls_txt} (confidence {conf_f:.2f}, threshold {thr:.2f})",
                        confidence=conf_f)
            return Tier1Proposal(steps=steps, confidence=conf_f, reasoning=str(resp.get("reasoning") or ""), raw=resp)


def _clip(s: str, n: int = 60) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def _describe(plan: MacroPlan) -> str:
    parts = []
    for s in plan.steps:
        args = ", ".join(f"{k}={v!r}" for k, v in s.arguments.items() if v not in (None, "", False))
        parts.append(f"{s.tool_name}({args})")
    return " ; ".join(parts)


def describe_step(step: PlanStep) -> str:
    return _describe(MacroPlan(goal="", steps=[step]))


