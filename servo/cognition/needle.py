"""Tier 1 intent: map one command to tool calls, with a confidence signal.

Order (cheapest first):
  1. Command grammar: deterministic parse of the canonical command forms (also the
     language System 2 is told to write its subtasks in). Exact, so confidence is high.
  2. Cactus Needle 3: on-device tool-calling model with a calibrated ``confidence``.
     Acted on only at or above ``settings.needle_confidence_threshold``.

Tier 1 never reasons about multi-step goals. If neither path is confident, that is a
*reasoning miss* and the router escalates up to System 2.
"""

from __future__ import annotations

import os
import re
import sys
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from servo.actuation.alias_mapping import AliasMapper
from servo.actuation.system_tools import SYSTEM_GRAMMAR, SYSTEM_TOOLS, parse_system_intent
from servo.cognition import cactus_runtime
from servo.cognition.trace import NullTracer, Tracer
from servo.config import settings
from servo.planner.tool_calling import SERVO_TOOL_DEFINITIONS, MacroPlan, PlanStep, parse_deterministic_intent

BROWSER_TOOLS = ("open_url", "web_search")

BROWSER_TOOL_DEFINITIONS: List[Dict[str, Any]] = [
    {
        "name": "open_url",
        "description": "Open a web page in the browser.",
        "parameters": {
            "type": "object",
            "properties": {"url": {"type": "string", "description": "Address, e.g. 'github.com' or 'https://example.com'."}},
            "required": ["url"],
        },
    },
    {
        "name": "web_search",
        "description": "Search the web in the browser.",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "What to search for."}},
            "required": ["query"],
        },
    },
]

NEEDLE_TOOLS = [*SERVO_TOOL_DEFINITIONS, *BROWSER_TOOL_DEFINITIONS]

# The grammar System 2 must use for subtasks (kept in sync with parse below).
COMMAND_GRAMMAR = """\
open <app>                     e.g. open notepad
close <app>                    e.g. close notepad   (or: close window)
go to <web address>            e.g. go to github.com
search the web for <query>     e.g. search the web for weather in Kathmandu
click <label>                  e.g. click Save   (also: double click <label>, right click <label>)
type "<text>" into <label>     e.g. type "hello" into Search
type "<text>"                  types into the focused editor
press <keys>                   e.g. press ctrl+s, press enter
save as <file name>            e.g. save as notes.txt on the desktop
wait <seconds>                 e.g. wait 1
""" + SYSTEM_GRAMMAR

_BROWSER_APPS = r"(?:chrome|google chrome|edge|microsoft edge|msedge|firefox|brave|the browser|browser|a browser)"
_URLISH = re.compile(r"^(?:https?://\S+|(?:www\.)?[a-z0-9-]+(?:\.[a-z0-9-]+)+(?:/\S*)?)$", re.I)


class Tier1Proposal(BaseModel):
    steps: List[PlanStep]
    confidence: float
    source: str  # "grammar" | "needle"
    reasoning: str = ""
    raw: Dict[str, Any] = Field(default_factory=dict)


def _step(i: int, tool: str, args: Dict[str, Any], why: str) -> PlanStep:
    canon = None
    if tool not in BROWSER_TOOLS and tool not in SYSTEM_TOOLS:
        try:
            canon = AliasMapper.canonicalize_action(raw_action=tool, **args)
        except Exception:
            canon = None
    return PlanStep(step_index=i, tool_name=tool, arguments=args, rationale=why, canonical_action=canon)


def normalize_url(u: str) -> str:
    u = u.strip().strip("'\"").rstrip(".")
    if not re.match(r"^https?://", u, re.I):
        u = "https://" + u
    return u


def _site_or_url(x: str) -> Optional[str]:
    from servo.cognition.user_browser import site_url
    x = x.strip().strip("'\"")
    if _URLISH.match(x) and "." in x:
        return normalize_url(x)
    return site_url(x)


def parse_site_intent(text: str) -> Optional[MacroPlan]:
    """'open gmail in chrome', 'search youtube for kanye', 'find X on amazon in dia'."""
    from servo.cognition.user_browser import _BROWSER_NAME_RE, site_search_url

    t = text.strip().rstrip(".")
    browser = ""
    m = re.match(rf"^(.+?)\s+(?:in|on|using|with)\s+({_BROWSER_NAME_RE})$", t, re.I)
    if m:
        t, browser = m.group(1).strip(), m.group(2).lower()

    def step(url: str, why: str) -> MacroPlan:
        args = {"url": url, **({"browser": browser} if browser else {})}
        return MacroPlan(goal=text.strip(), steps=[_step(1, "open_url", args, why + (f" in {browser}" if browser else ""))])

    m = (re.match(r"^(?:search|look\s+up|find)\s+(?:on\s+)?(\w[\w ]*?)\s+for\s+(.+)$", t, re.I)
         or None)
    if m:
        url = site_search_url(m.group(1), m.group(2).strip("'\""))
        if url:
            return step(url, f"Search {m.group(1)} for '{m.group(2)}'")
    m = re.match(r"^(?:search(?:\s+for)?|look\s+up|find)\s+(.+?)\s+(?:on|in)\s+(\w[\w ]*?)$", t, re.I)
    if m:
        url = site_search_url(m.group(2), m.group(1).strip("'\""))
        if url:
            return step(url, f"Search {m.group(2)} for '{m.group(1)}'")
    # "go to youtube": web verbs are unambiguous ("open spotify" may mean the desktop app).
    m = re.match(r"^(?:go\s+to|visit|navigate\s+to|browse\s+to)\s+(\w[\w ]*?)$", t, re.I)
    if m and not browser:
        from servo.cognition.user_browser import site_url
        url = site_url(m.group(1))
        if url:
            return step(url, f"Open {url}")
    if browser:
        m = re.match(r"^(?:open|go\s+to|visit|navigate\s+to|browse\s+to|load|show)\s+(.+)$", t, re.I)
        if m:
            url = _site_or_url(m.group(1))
            if url:
                return step(url, f"Open {url}")
            if re.fullmatch(r"(?:it|a\s+new\s+tab|new\s+tab|the\s+browser)", m.group(1).strip(), re.I):
                return None
    return None


def parse_web_intent(text: str) -> Optional[MacroPlan]:
    """Grammar for browser commands (your browser, or the Playwright tier in that mode)."""
    plan = parse_site_intent(text)
    if plan:
        return plan
    t = text.strip().rstrip(".")
    m = re.match(rf"^(?:open|launch|start)\s+{_BROWSER_APPS}\s+and\s+(?:search(?:\s+the\s+web)?\s+for|google|look\s+up)\s+(.+)$", t, re.I)
    if m:
        q = m.group(1).strip().strip("'\"")
        return MacroPlan(goal=t, steps=[_step(1, "web_search", {"query": q}, f"Search the web for '{q}'")])
    m = re.match(rf"^(?:open|launch|start)\s+{_BROWSER_APPS}\s+and\s+(?:go\s+to|open|visit|navigate\s+to)\s+(\S+)$", t, re.I)
    if m and _URLISH.match(m.group(1)):
        url = normalize_url(m.group(1))
        return MacroPlan(goal=t, steps=[_step(1, "open_url", {"url": url}, f"Open {url}")])
    m = re.match(r"^(?:go\s+to|open|visit|navigate\s+to|browse\s+to|load)\s+(\S+)$", t, re.I)
    if m and _URLISH.match(m.group(1)) and "." in m.group(1):
        url = normalize_url(m.group(1))
        return MacroPlan(goal=t, steps=[_step(1, "open_url", {"url": url}, f"Open {url}")])
    m = re.match(r"^(?:search\s+(?:the\s+web|online|the\s+internet|google|bing)\s+for|google|web\s+search(?:\s+for)?)\s+(.+)$", t, re.I)
    if m:
        q = m.group(1).strip().strip("'\"")
        return MacroPlan(goal=t, steps=[_step(1, "web_search", {"query": q}, f"Search the web for '{q}'")])
    return None


_MODIFIED_CLICK = re.compile(r"^(double|right)[\s-]click\s+(?:on\s+)?(.+)$", re.I)


def _plausible(plan: MacroPlan) -> bool:
    """Reject grammar matches that swallowed a second action.

    "search the web for X and store the links in notepad" must not become one search for
    the whole sentence; it is two clauses, and the second needs reasoning.
    """
    second_action = re.compile(rf"\band\s+(?:{_CLAUSE_VERBS})\b|\b(?:and then|then)\b", re.I)
    for s in plan.steps:
        for key in ("app_name", "target", "query", "text", "url"):
            val = str(s.arguments.get(key) or "")
            if second_action.search(val):
                return False
            # App names are short; a long one swallowed a clause ("open notepad after lunch").
            # Click targets can legitimately be long link/video titles ("Kanye West - Runaway
            # (Video Version) ft. Pusha T 4 minutes, 38 seconds", "If I Ruled the World").
            if key == "app_name" and (len(val.split()) > 6 or re.search(r"\b(after|before|if)\b", val, re.I)):
                return False
            if key == "target" and len(val.split()) > 30:
                return False
        if s.tool_name == "launch_app" and re.search(r"\band\b", str(s.arguments.get("app_name", "")), re.I):
            return False
    return True


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


_SAVE_AS = re.compile(
    r"^save\s+(?:it|this|the\s+file|the\s+document|the\s+note|that)?\s*as\s+[\"“']?(.+?)[\"”']?"
    r"(?:\s+(?:on|in|to|into)\s+(?:the\s+|my\s+)?(desktop|documents|downloads)(?:\s+folder)?)?$",
    re.I,
)
_SAVE = re.compile(r"^save(?:\s+(?:it|this|the\s+file|the\s+document|changes|that))?$", re.I)


def parse_save(t: str) -> Optional[MacroPlan]:
    m = _SAVE_AS.match(t)
    if m:
        name, folder = m.group(1).strip(), (m.group(2) or "").lower()
        path = os.path.join(known_folder(folder), name) if folder else name
        return MacroPlan(goal=t, steps=[
            _step(1, "send_hotkey", {"keys": ["ctrl", "shift", "s"]}, "Open Save As"),
            _step(2, "wait", {"duration_seconds": 1.0}, "Let the Save dialog open"),
            _step(3, "type_text", {"text": path, "target": "File name", "clear_first": True, "press_enter": True},
                  f"Save as {path}"),
        ])
    if _SAVE.match(t):
        return MacroPlan(goal=t, steps=[_step(1, "send_hotkey", {"keys": ["ctrl", "s"]}, "Save")])
    return None


def _unescape_typed(text: str) -> str:
    """Models often double-escape line breaks inside JSON ('\\\\n'); type real newlines/tabs."""
    text = text.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\t", "\t")
    # ...and unicode escapes: an em dash arrived as the six characters "—".
    return re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), text)


_MAX_REPEAT = 25


def _repeat_key(goal: str, key: str, n: int, why: str) -> MacroPlan:
    n = max(1, min(int(n), _MAX_REPEAT))
    return MacroPlan(goal=goal, steps=[_step(i, "send_hotkey", {"keys": [key]}, f"{why} ({i}/{n})" if n > 1 else why)
                                      for i in range(1, n + 1)])


def parse_media_intent(text: str) -> Optional[MacroPlan]:
    """Volume and playback by system media keys (each volume key press is about 2%).

    'increase the volume a bit', 'turn the volume down by 20%', 'mute', 'pause the music',
    'press volumeup 3 times', 'next track'.
    """
    t = text.strip().rstrip(".!").lower()
    m = re.match(r"^press\s+(\w[\w+\-]*)\s+(\d{1,2})\s+times$", t)
    if m:
        from servo.planner.tool_calling import _is_hotkey_argument
        if _is_hotkey_argument(m.group(1)):
            return _repeat_key(text.strip(), m.group(1), int(m.group(2)), f"Press {m.group(1)}")
    up = re.match(r"^(?:(?:increase|raise|turn\s+up|bump\s+up|boost)\s+(?:the\s+|my\s+)?(?:volume|sound)|"
                  r"(?:turn|make)\s+(?:the\s+|my\s+)?(?:volume|sound|it)\s+up|louder|volume\s+up)(.*)$", t)
    down = re.match(r"^(?:(?:decrease|lower|reduce|turn\s+down)\s+(?:the\s+|my\s+)?(?:volume|sound)|"
                    r"(?:turn|make)\s+(?:the\s+|my\s+)?(?:volume|sound|it)\s+down|quieter|softer|volume\s+down)(.*)$", t)
    for mm, key in ((up, "volumeup"), (down, "volumedown")):
        if mm:
            rest = mm.group(1)
            pct = re.search(r"(\d{1,3})\s*(?:%|percent)", rest)
            steps_n = re.search(r"(\d{1,2})\s*(?:steps?|notches?|times)", rest)
            if pct:
                n = round(int(pct.group(1)) / 2)
            elif steps_n:
                n = int(steps_n.group(1))
            elif re.search(r"\b(?:a\s+lot|much|way)\b", rest):
                n = 12
            elif rest.strip() in ("", "a bit", "a little", "a little bit", "slightly", "a tad") or "bit" in rest:
                n = 5
            else:
                return None
            return _repeat_key(text.strip(), key, n, f"Volume {'up' if key == 'volumeup' else 'down'}")
    if re.match(r"^(?:mute|unmute)(?:\s+(?:the\s+)?(?:volume|sound|audio|computer|pc|laptop))?$", t):
        return _repeat_key(text.strip(), "volumemute", 1, "Toggle mute")
    if re.match(r"^(?:pause|resume|play|unpause)\s+(?:the\s+)?(?:music|song|audio|media|playback)$", t):
        return _repeat_key(text.strip(), "playpause", 1, "Play/pause media")
    if re.match(r"^(?:next|skip)\s+(?:the\s+)?(?:track|song)$|^skip\s+(?:this\s+)?song$", t):
        return _repeat_key(text.strip(), "nexttrack", 1, "Next track")
    if re.match(r"^(?:previous|prev|last)\s+(?:track|song)$", t):
        return _repeat_key(text.strip(), "prevtrack", 1, "Previous track")
    return None


def parse_grammar(text: str, user_name: str = "User") -> Optional[MacroPlan]:
    # System tools first: grammar-only (never Needle), and their quoted text is literal.
    sys_intent = parse_system_intent(text)
    if sys_intent:
        tool, args = sys_intent
        return MacroPlan(goal=text.strip(), steps=[_step(1, tool, args, f"{tool.replace('_', ' ').capitalize()}")])
    media = parse_media_intent(text)
    if media:
        return media
    t = text.strip().rstrip(".")
    plan = parse_web_intent(t)
    if plan and _plausible(plan):
        return plan
    if plan:
        return None
    plan = parse_save(t)
    if plan:
        return plan
    m = _MODIFIED_CLICK.match(t)
    if m:
        kind, target = m.group(1).lower(), m.group(2).strip().strip("'\"")
        tool = "double_click" if kind == "double" else "right_click"
        return MacroPlan(goal=t, steps=[_step(1, tool, {"target": target}, f"{kind.capitalize()}-click '{target}'")])
    # `type "x" into y` with explicit quotes keeps text containing " into " intact.
    m = re.match(r'^(?:type|write|enter)\s+"([^"]+)"\s+(?:in|into|on)\s+(.+)$', t, re.I | re.S)
    if m:
        txt, target = _unescape_typed(m.group(1)), m.group(2).strip().strip("'\"")
        return MacroPlan(goal=t, steps=[_step(1, "type_text", {"text": txt, "target": target}, f"Type '{_clip(txt)}' into '{target}'")])
    # `type "x"` (quoted, no target): the WHOLE quoted text goes to the focused editor.
    # Without this, a composed poem fell through to a rule that split it at the word "in".
    m = re.match(r'^(?:type|write|enter)\s+"(.*)"$', text.strip(), re.I | re.S)
    if m and m.group(1).strip():
        txt = _unescape_typed(m.group(1))
        return MacroPlan(goal=t, steps=[_step(1, "type_text", {"text": txt, "target": "editor"}, f"Type '{_clip(txt)}'")])
    # Any clause that describes text to compose ("write a short poem about rain") needs
    # System 2. Checking only the whole sentence missed "open notepad and write a poem…",
    # which then typed the literal words "a short poem about rain".
    if any(_describes_text_to_write(c) for c in split_clauses(t)):
        return None
    plan = parse_deterministic_intent(t, user_name=user_name)
    if plan and plan.steps and _plausible(plan):
        # Validate that referenced applications are installed before blind Tier 1 launch
        for s in plan.steps:
            if s.tool_name == "launch_app":
                app = str(s.arguments.get("app_name") or "")
                try:
                    from servo.cognition.windows_context import windows_context
                    installed, _ = windows_context.is_installed(app)
                    if not installed:
                        web_fallback = windows_context.get_web_fallback(app)
                        if web_fallback:
                            return MacroPlan(
                                goal=t,
                                steps=[_step(1, "open_url", {"url": web_fallback}, f"Open {app.capitalize()} Web ({web_fallback})")],
                            )
                        # Not installed: let System 2 handle with Windows context
                        return None
                except Exception:
                    pass
        return plan
    return None


_GEN_TEXT = re.compile(
    r"^(?:type|write|enter|put|add|note)\s+(?:down\s+)?(?:(?:a|an|the|some|my|your)\s+\S+.*|.*\b(?:summary|summari[sz]e|"
    r"description|explanation|about|of\s+(?:it|them|this|that|the\s+\w+)|it|them|those|these|results?|links?|answer|"
    r"poem|haiku|story|email|letter|paragraph|list|bullet\s+points?|reply|response)\b.*)$",
    re.I,
)


def _describes_text_to_write(t: str) -> bool:
    """'write a one-line summary of it' describes text to compose; 'write hello' is literal.

    Quoted text is always literal, and 'write my name' is handled by the parser.
    """
    if re.match(r'^(?:type|write|enter)\s+["“]', t, re.I):
        return False
    if re.match(r"^(?:type|write|enter)\s+my\s+(?:user)?name$", t, re.I):
        return False
    body = re.sub(r"\s+(?:in|into|on)\s+(?:the\s+)?\S+$", "", t)  # drop a trailing "into Search"
    return bool(_GEN_TEXT.match(body)) and len(body.split()) >= 3


# Verbs that start a new clause in a compound request ("…, then open notepad and type …").
_CLAUSE_VERBS = (
    r"open|launch|start|close|quit|exit|click|double[\s-]click|right[\s-]click|press|hit|type|write|enter|"
    r"go\s+to|visit|navigate|browse|search|google|look\s+up|wait|save|run|store|put|paste|copy|note|record|"
    r"summari[sz]e|send|email|reply|compare|list|find|read|tell|show|make|create|add|remove|delete|"
    r"download|upload|select|scroll|switch|rename|move|minimi[sz]e|maximi[sz]e|check|fill|submit|sign|log"
)
_SPLIT_RE = re.compile(
    rf"\s*(?:,|;)\s*(?:and\s+)?(?:then\s+)?(?=(?:{_CLAUSE_VERBS})\b)"   # ", open …" / "; then click …"
    rf"|\s*,?\s*(?:and\s+)?then\s+"                                      # "… then …" / "… and then …"
    rf"|\s+and\s+(?=(?:{_CLAUSE_VERBS})\b)"                              # "… and type …"
    rf"|\s*\.\s+(?=[A-Z])",                                             # sentence break
    re.I,
)


def split_clauses(text: str) -> List[str]:
    """Split a compound request into single commands, never inside quotes."""
    masked, quotes = [], []

    def _mask(m: "re.Match[str]") -> str:
        quotes.append(m.group(0))
        return f"\x00{len(quotes) - 1}\x00"

    masked_text = re.sub(r'"[^"]*"|“[^”]*”', _mask, text.strip().rstrip("."))
    parts = [p for p in _SPLIT_RE.split(masked_text) if p and p.strip()]
    for p in parts:
        masked.append(re.sub(r"\x00(\d+)\x00", lambda m: quotes[int(m.group(1))], p.strip()))
    return masked or [text.strip()]


class Tier1Intent:
    """Grammar first, then Cactus Needle; returns a proposal with a confidence."""

    def __init__(self, user_name: str = "User", tracer: Optional[Tracer] = None) -> None:
        self.user_name = user_name
        self.tracer = tracer or NullTracer()

    def needle_available(self) -> bool:
        return cactus_runtime.get_needle(NEEDLE_TOOLS) is not None

    def propose_compound(self, text: str) -> Optional[Tier1Proposal]:
        """Parse every clause of a compound request; None unless ALL of them parse confidently.

        "search the web for X, then open notepad and type Y" is three deterministic
        commands and needs no LLM. If any clause needs judgement ("…and summarize it"),
        the whole request goes to System 2 instead.
        """
        clauses = split_clauses(text)
        if len(clauses) < 2:
            return None
        steps: List[PlanStep] = []
        confs: List[float] = []
        with self.tracer.span("tier", f"Tier 1 · {len(clauses)} clauses", tier="needle", clauses=clauses) as sp:
            for clause in clauses:
                prop = self.propose(clause)
                if prop is None or prop.confidence < settings.needle_confidence_threshold:
                    sp.done("warn", f"“{clause}” needs reasoning — sending the whole request up")
                    return None
                steps.extend(prop.steps)
                confs.append(prop.confidence)
            for i, s in enumerate(steps, start=1):
                s.step_index = i
            sp.done("ok", f"All {len(clauses)} clauses parsed → {len(steps)} steps", confidence=min(confs))
        return Tier1Proposal(steps=steps, confidence=min(confs), source="grammar")

    def propose(self, text: str, use_needle: bool = True) -> Optional[Tier1Proposal]:
        tr = self.tracer
        with tr.span("tier", f"Tier 1 · parse “{_clip(text)}”", tier="needle") as sp:
            plan = parse_grammar(text, self.user_name)
            if plan:
                sp.done("ok", f"Command grammar matched → {_describe(plan)}", confidence=0.95,
                        source="grammar", steps=[s.model_dump(exclude={"canonical_action"}) for s in plan.steps])
                return Tier1Proposal(steps=plan.steps, confidence=0.95, source="grammar")

            if not use_needle:
                sp.done("warn", "No grammar match; Needle disabled for this call")
                return None
            agent = cactus_runtime.get_needle(NEEDLE_TOOLS)
            if agent is None:
                sp.done("warn", "No grammar match, and Cactus Needle is not installed — reasoning miss",
                        needle=cactus_runtime.status())
                return None

            t0 = time.perf_counter()
            resp = cactus_runtime.needle_complete(agent, text)
            ms = int((time.perf_counter() - t0) * 1000)
            if not resp:
                sp.done("fail", "Needle returned nothing usable", latency_ms=ms)
                return None
            calls = resp.get("function_calls") or []
            conf = resp.get("confidence")
            conf_f = float(conf) if isinstance(conf, (int, float)) else 0.0
            tr.emit("model", "Cactus Needle 3", resp.get("reasoning") or "", tier="needle",
                    confidence=conf_f, duration_ms=ms, response=resp,
                    status="ok" if calls else "warn")
            if not calls:
                sp.done("warn", "Needle proposed no tool call (out of scope for Tier 1)", confidence=conf_f)
                return None
            steps = [
                _step(i, str(c.get("name")), dict(c.get("arguments") or {}), f"Needle: {c.get('name')}")
                for i, c in enumerate(calls, start=1)
            ]
            mismatch = _verb_mismatch(text, steps)
            if mismatch:
                # e.g. "pause the video" -> close_window. A confident wrong action is worse
                # than escalating, so refuse it and let System 2 decide.
                sp.done("warn", f"Needle proposed {mismatch}, which the request never asks for — rejected",
                        confidence=conf_f)
                return None
            for s in steps:
                if s.tool_name == "open_url" and "url" in s.arguments:
                    s.arguments["url"] = normalize_url(str(s.arguments["url"]))
            thr = settings.needle_confidence_threshold
            sp.done(
                "ok" if conf_f >= thr else "warn",
                f"Needle → {', '.join(s.tool_name for s in steps)} (confidence {conf_f:.2f}, threshold {thr:.2f})",
                confidence=conf_f,
            )
            return Tier1Proposal(steps=steps, confidence=conf_f, source="needle",
                                 reasoning=str(resp.get("reasoning") or ""), raw=resp)


# Tools that change or destroy state must be literally asked for. Needle maps "pause the
# video" to close_window with high confidence; this guard makes that a reasoning miss.
_REQUIRED_VERBS = {
    "close_window": r"\b(close|quit|exit|kill|shut|end)\b",
    "launch_app": r"\b(open|launch|start|run|go\s+to|switch\s+to|bring\s+up|show)\b",
    "type_text": r"\b(type|write|enter|input|fill|put|note|search|say|paste|add)\b",
}


def _verb_mismatch(text: str, steps: List[PlanStep]) -> Optional[str]:
    for s in steps:
        pattern = _REQUIRED_VERBS.get(s.tool_name)
        if pattern and not re.search(pattern, text, re.I):
            return s.tool_name
    return None


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


def is_url(s: str) -> bool:
    try:
        return bool(urlparse(normalize_url(s)).netloc)
    except Exception:
        return False
