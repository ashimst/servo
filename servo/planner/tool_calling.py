"""Structured Tool Calling and System 2 Macro Plan Generation.

See ADR-10 in SERVO_ARCHITECTURE_V1.md and SERVO_ARCHITECTURE_V3.md.
Provides JSON schema tool declarations, structured plan decomposition,
small-model few-shot prompt optimization, and error recovery parsing.
"""

from __future__ import annotations

import ast
import json
import logging
import re
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Set, Tuple, Union
from pydantic import BaseModel, Field

from servo.actuation.alias_mapping import AliasMapper, CanonicalAction, CanonicalActionType
from servo.actuation.sandboxing import normalize_keys
from servo.config import settings
from servo.perception.scene_index import SceneIndex

if TYPE_CHECKING:
    from servo.planner.base import SubGoal

logger = logging.getLogger("servo.planner.tool_calling")


# =============================================================================
# Tool Schema Declarations (OpenAI / Anthropic / Gemini Compatible)
# =============================================================================

SERVO_TOOL_DEFINITIONS: List[Dict[str, Any]] = [
    {
        "name": "launch_app",
        "description": "Launch a desktop application or bring it to the foreground.",
        "parameters": {
            "type": "object",
            "properties": {
                "app_name": {
                    "type": "string",
                    "description": "Name or executable of the application (e.g. 'notepad', 'calc', 'chrome').",
                }
            },
            "required": ["app_name"],
        },
    },
    {
        "name": "click_element",
        "description": "Click on a user interface element using its label or alias.",
        "parameters": {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "The exact or partial label of the UI control to click (e.g. 'File', 'Save', '7').",
                },
                "button": {
                    "type": "string",
                    "enum": ["left", "right", "double"],
                    "default": "left",
                    "description": "Mouse button to click with.",
                },
            },
            "required": ["target"],
        },
    },
    {
        "name": "type_text",
        "description": "Focus an input field or text editor and type text into it.",
        "parameters": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "The exact string to enter.",
                },
                "target": {
                    "type": "string",
                    "default": "editor",
                    "description": "Label or hint for the target input area (e.g. 'editor', 'Search', 'Filename').",
                },
                "clear_first": {
                    "type": "boolean",
                    "default": False,
                    "description": "Whether to select all existing text and delete it before typing.",
                },
                "press_enter": {
                    "type": "boolean",
                    "default": False,
                    "description": "Whether to press Enter immediately after typing.",
                },
            },
            "required": ["text"],
        },
    },
    {
        "name": "send_hotkey",
        "description": "Press a keyboard shortcut combination.",
        "parameters": {
            "type": "object",
            "properties": {
                "keys": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of key names (e.g. ['ctrl', 's'] or ['enter']).",
                }
            },
            "required": ["keys"],
        },
    },
    {
        "name": "wait",
        "description": "Pause execution for a brief duration to allow the UI to settle or animations to complete.",
        "parameters": {
            "type": "object",
            "properties": {
                "duration_seconds": {
                    "type": "number",
                    "description": "Seconds to wait (typically 0.2 to 2.0).",
                }
            },
            "required": ["duration_seconds"],
        },
    },
    {
        "name": "close_window",
        "description": "Close the active application window.",
        "parameters": {
            "type": "object",
            "properties": {
                "app_name": {
                    "type": "string",
                    "description": "Optional name of the application to close; closes active window if omitted.",
                }
            },
        },
    },
]


class PlanStep(BaseModel):
    """An individual atomic step inside a macro plan."""
    step_index: int
    tool_name: str
    arguments: Dict[str, Any] = Field(default_factory=dict)
    rationale: str = ""
    canonical_action: Optional[CanonicalAction] = None

    def to_subgoal(self) -> SubGoal:
        """Convert this PlanStep into a SubGoal for control loop execution."""
        from servo.planner.base import SubGoal
        return SubGoal.from_plan_step(self)

    def __getitem__(self, key: str) -> Any:
        if hasattr(self, key) and key not in ("to_subgoal",):
            return getattr(self, key)
        if key in self.arguments:
            return self.arguments[key]
        if self.canonical_action and hasattr(self.canonical_action, key):
            val = getattr(self.canonical_action, key)
            if val is not None:
                return val
        if key in ("type", "action_type", "action"):
            if self.canonical_action:
                return self.canonical_action.action_type.value
            return self.tool_name
        if key in ("app", "app_name"):
            if self.canonical_action and self.canonical_action.app_name:
                return self.canonical_action.app_name
            return self.arguments.get("app_name") or self.arguments.get("app")
        raise KeyError(key)

    def __contains__(self, key: str) -> bool:
        try:
            self[key]
            return True
        except KeyError:
            return False

    def get(self, key: str, default: Any = None) -> Any:
        try:
            return self[key]
        except KeyError:
            return default


class MacroPlan(BaseModel):
    """Structured execution plan decomposed by System 2."""
    goal: str
    steps: List[PlanStep] = Field(default_factory=list)
    estimated_latency_ms: int = 0
    raw_response: str = ""

    def to_subgoals(self) -> List[SubGoal]:
        """Convert all steps in this MacroPlan into SubGoals."""
        return [step.to_subgoal() for step in self.steps]

    def __getitem__(self, key: str) -> Any:
        """Dictionary access backwards compatibility for legacy assertions."""
        if hasattr(self, key) and key not in ("to_subgoals", "to_subgoal"):
            return getattr(self, key)
        if key == "actions":
            return self.steps
        if key == "sub_goals":
            return self.to_subgoals()
        if self.steps:
            first_step = self.steps[0]
            if key in ("type", "action_type", "action"):
                if first_step.canonical_action:
                    return first_step.canonical_action.action_type.value
                if "type" in first_step.arguments:
                    return first_step.arguments["type"]
                tn = first_step.tool_name.lower()
                if "launch" in tn or "open" in tn:
                    return "launch"
                elif "click" in tn:
                    return "click"
                elif "type" in tn or "write" in tn:
                    return "type"
                elif "hotkey" in tn:
                    return "hotkey"
                elif "close" in tn or "quit" in tn:
                    return "close"
                elif "wait" in tn:
                    return "wait"
                return tn
            if key in ("app", "app_name"):
                if first_step.canonical_action and first_step.canonical_action.app_name:
                    return first_step.canonical_action.app_name
                return first_step.arguments.get("app_name") or first_step.arguments.get("app")
            if key == "text":
                if first_step.canonical_action and first_step.canonical_action.text:
                    return first_step.canonical_action.text
                return first_step.arguments.get("text")
            if key == "target":
                if first_step.canonical_action and first_step.canonical_action.target:
                    return first_step.canonical_action.target
                return first_step.arguments.get("target")
            if key == "keys":
                if first_step.canonical_action and first_step.canonical_action.keys:
                    return first_step.canonical_action.keys
                return first_step.arguments.get("keys")
            if key == "button":
                if first_step.canonical_action and first_step.canonical_action.button:
                    return first_step.canonical_action.button
                return first_step.arguments.get("button", "left")
            if key in first_step.arguments:
                return first_step.arguments[key]
            if key == "arguments":
                return first_step.arguments
        raise KeyError(key)

    def __contains__(self, key: str) -> bool:
        try:
            self[key]
            return True
        except KeyError:
            return False

    def get(self, key: str, default: Any = None) -> Any:
        try:
            return self[key]
        except KeyError:
            return default


# =============================================================================
# Small-Model & Free-Tier Optimized System Prompt
# =============================================================================

SYSTEM_PROMPT_TEMPLATE = """You are Servo System 2 Planner, an expert Windows desktop automation engine.
Your mission is to decompose the user's high-level goal into a concise, ordered list of atomic tool calls.

Available Tools:
1. launch_app(app_name="notepad") - open or focus application
2. click_element(target="Save", button="left") - click UI control
3. type_text(text="...", target="editor", clear_first=false, press_enter=false) - type text
4. send_hotkey(keys=["ctrl", "s"]) - trigger keyboard shortcut
5. wait(duration_seconds=0.5) - pause for UI animation
6. close_window(app_name="notepad") - close application window

Rules:
- Output ONLY valid JSON: an array of step objects with keys: "step_index", "tool_name", "arguments", "rationale".
- Do NOT output extra explanatory prose outside the JSON.
- Never invent pixel coordinates; use element labels or semantic targets.
- If the goal is a casual conversation question (not a desktop task), return an empty array [].

Example 1:
Goal: "open notepad and write hello world"
Response:
[
  {"step_index": 1, "tool_name": "launch_app", "arguments": {"app_name": "notepad"}, "rationale": "Open Notepad window"},
  {"step_index": 2, "tool_name": "type_text", "arguments": {"text": "hello world", "target": "editor"}, "rationale": "Write text into Notepad editor"}
]

Example 2:
Goal: "open calculator and click 7"
Response:
[
  {"step_index": 1, "tool_name": "launch_app", "arguments": {"app_name": "calculator"}, "rationale": "Open Calculator"},
  {"step_index": 2, "tool_name": "click_element", "arguments": {"target": "7"}, "rationale": "Click the 7 button"}
]
"""


def build_tool_planning_prompt(
    goal: str,
    scene: Optional[SceneIndex] = None,
    user_name: str = "User",
) -> str:
    """Construct an optimized prompt for System 2 planning with current window context."""
    context_lines = [f"User Name: {user_name}"]
    if scene and not scene.sense_error:
        context_lines.append(f"Active Window: '{scene.window_title}' (HWND {scene.hwnd})")
        labels = [el.label for el in scene.elements.values() if el.label.strip()]
        if labels:
            sample_labels = labels[:40]
            context_lines.append(f"Visible Element Labels: {json.dumps(sample_labels)}")

    return f"Context:\n" + "\n".join(context_lines) + f"\n\nGoal: {goal}\nJSON Plan:"


STANDARD_HOTKEY_KEYS: Set[str] = {
    "ctrl", "control", "ctl", "lctrl", "rctrl",
    "alt", "lalt", "ralt", "altgr", "option", "menu",
    "shift", "lshift", "rshift",
    "win", "windows", "lwin", "rwin", "cmd", "command", "meta", "super",
    "enter", "return", "tab", "esc", "escape", "backspace", "delete", "del", "space", "spacebar",
    "up", "down", "left", "right", "home", "end", "pageup", "pagedown", "pgup", "pgdn",
    "insert", "ins", "capslock", "numlock", "scrolllock", "printscreen", "prtsc", "pause",
} | {f"f{i}" for i in range(1, 25)}


def _is_hotkey_argument(arg_str: str) -> bool:
    """Return True if argument appears to be a hotkey combination or standard key."""
    clean = arg_str.strip().strip("'\"").lower()
    clean = re.sub(r"^the\s+", "", clean)
    clean = re.sub(r"\s+keys?$", "", clean).strip()
    if not clean:
        return False

    # Check for combo delimiters '+' or '-'
    if "+" in clean or "-" in clean:
        delimiter = "+" if "+" in clean else "-"
        parts = [p.strip() for p in clean.split(delimiter) if p.strip()]
        if parts:
            has_std = any(p in STANDARD_HOTKEY_KEYS for p in parts)
            all_key_like = all(p in STANDARD_HOTKEY_KEYS or (len(p) <= 2 and p.isalnum()) for p in parts)
            if has_std or all_key_like:
                return True

    # Single key check
    if clean in STANDARD_HOTKEY_KEYS:
        return True

    return False


def parse_deterministic_intent(goal: str, user_name: str = "User") -> Optional[MacroPlan]:
    """Deterministic intent parsing for common Windows desktop commands.

    Used as an offline fallback when LLM endpoints are unreachable, rate-limited (HTTP 429),
    or return unparsable responses.
    """
    if not goal or not goal.strip():
        return None

    p_strip = goal.strip()
    steps: List[PlanStep] = []

    # 1. "open <app> and (write|type|enter) <text>"
    m_open_type = re.search(
        r"^(?:open|launch|start)\s+([a-zA-Z0-9_\s\.\-]+?)\s+and\s+(?:write|type|enter)\s+(.+)$",
        p_strip,
        re.I,
    )
    if m_open_type:
        app = m_open_type.group(1).strip()
        raw_text = m_open_type.group(2).strip().strip('"\'')
        press_enter = False
        m_trailing_enter = re.search(r"\s+and\s+(?:press|hit|send)\s+(?:the\s+)?enter(?:\s+key)?$", raw_text, re.I)
        if m_trailing_enter:
            press_enter = True
            raw_text = raw_text[:m_trailing_enter.start()].strip().strip("'\"")
        text = user_name if raw_text.lower() in ("my name", "my username") else raw_text
        steps.append(
            PlanStep(
                step_index=1,
                tool_name="launch_app",
                arguments={"app_name": app},
                rationale=f"Launch application '{app}'",
                canonical_action=AliasMapper.canonicalize_action("launch_app", app_name=app),
            )
        )
        steps.append(
            PlanStep(
                step_index=2,
                tool_name="type_text",
                arguments={"text": text, "target": "editor", "press_enter": press_enter},
                rationale=f"Type '{text}' into editor",
                canonical_action=AliasMapper.canonicalize_action(
                    "type_text", text=text, target="editor", press_enter=press_enter
                ),
            )
        )
        return MacroPlan(goal=p_strip, steps=steps)

    # 2. "open <app> and (click|press|select) <target>"
    m_open_click = re.search(
        r"^(?:open|launch|start)\s+([a-zA-Z0-9_\s\.\-]+?)\s+and\s+(?:click|press|select)\s+(.+)$",
        p_strip,
        re.I,
    )
    if m_open_click:
        app = m_open_click.group(1).strip()
        target = m_open_click.group(2).strip().strip('"\'')
        steps.append(
            PlanStep(
                step_index=1,
                tool_name="launch_app",
                arguments={"app_name": app},
                rationale=f"Launch application '{app}'",
                canonical_action=AliasMapper.canonicalize_action("launch_app", app_name=app),
            )
        )
        if _is_hotkey_argument(target):
            clean_keys = re.sub(r"^the\s+", "", target, flags=re.I)
            clean_keys = re.sub(r"\s+keys?$", "", clean_keys, flags=re.I).strip()
            raw_keys = [k.strip() for k in re.split(r"[\+\s,\-]+", clean_keys) if k.strip()]
            keys_list = normalize_keys(raw_keys)
            steps.append(
                PlanStep(
                    step_index=2,
                    tool_name="send_hotkey",
                    arguments={"keys": keys_list},
                    rationale=f"Press hotkey '{'+'.join(keys_list)}'",
                    canonical_action=AliasMapper.canonicalize_action("send_hotkey", keys=keys_list),
                )
            )
        else:
            steps.append(
                PlanStep(
                    step_index=2,
                    tool_name="click_element",
                    arguments={"target": target},
                    rationale=f"Click '{target}'",
                    canonical_action=AliasMapper.canonicalize_action("click_element", target=target),
                )
            )
        return MacroPlan(goal=p_strip, steps=steps)

    # 3. "close <app>" or "close window" or "quit <app>" or "exit <app>"
    m_close = re.search(r"^(?:close|quit|exit)\s*([a-zA-Z0-9_\s\.\-]*)$", p_strip, re.I)
    if m_close:
        raw_app = m_close.group(1).strip()
        app = None if (not raw_app or raw_app.lower() in ("window", "the window", "active window", "current window")) else raw_app
        steps.append(
            PlanStep(
                step_index=1,
                tool_name="close_window",
                arguments={"app_name": app} if app else {},
                rationale=f"Close application '{app or 'active window'}'",
                canonical_action=AliasMapper.canonicalize_action("close_window", app_name=app),
            )
        )
        return MacroPlan(goal=p_strip, steps=steps)

    # 4. "open <app>" or "launch <app>" or "start <app>" or "switch to <app>"
    m_open_only = re.search(r"^(?:open|launch|start|switch to)\s+([a-zA-Z0-9_\s\.\-]+)$", p_strip, re.I)
    if m_open_only:
        app = m_open_only.group(1).strip()
        if not any(w in app.lower() for w in ["what", "how", "why", "when", "who"]):
            steps.append(
                PlanStep(
                    step_index=1,
                    tool_name="launch_app",
                    arguments={"app_name": app},
                    rationale=f"Launch application '{app}'",
                    canonical_action=AliasMapper.canonicalize_action("launch_app", app_name=app),
                )
            )
            return MacroPlan(goal=p_strip, steps=steps)

    # 5. "write|type|enter <text> (in|into|on) <target>"
    m_type_into = re.search(r"^(?:write|type|enter)\s+(.+?)\s+(?:in|into|on)\s+(.+)$", p_strip, re.I)
    if m_type_into:
        raw_text = m_type_into.group(1).strip().strip('"\'')
        target = m_type_into.group(2).strip().strip('"\'')
        press_enter = False
        m_trailing_enter = re.search(r"\s+and\s+(?:press|hit|send)\s+(?:the\s+)?enter(?:\s+key)?$", target, re.I)
        if m_trailing_enter:
            press_enter = True
            target = target[:m_trailing_enter.start()].strip().strip("'\"")
        text = user_name if raw_text.lower() in ("my name", "my username") else raw_text
        steps.append(
            PlanStep(
                step_index=1,
                tool_name="type_text",
                arguments={"text": text, "target": target, "press_enter": press_enter},
                rationale=f"Type '{text}' into '{target}'",
                canonical_action=AliasMapper.canonicalize_action(
                    "type_text", text=text, target=target, press_enter=press_enter
                ),
            )
        )
        return MacroPlan(goal=p_strip, steps=steps)

    # 6. "write|type|enter <text>"
    m_type_only = re.search(r"^(?:write|type|enter)\s+(.+)$", p_strip, re.I)
    if m_type_only:
        raw_text = m_type_only.group(1).strip().strip('"\'')
        press_enter = False
        m_trailing_enter = re.search(r"\s+and\s+(?:press|hit|send)\s+(?:the\s+)?enter(?:\s+key)?$", raw_text, re.I)
        if m_trailing_enter:
            press_enter = True
            raw_text = raw_text[:m_trailing_enter.start()].strip().strip("'\"")
        text = user_name if raw_text.lower() in ("my name", "my username") else raw_text
        steps.append(
            PlanStep(
                step_index=1,
                tool_name="type_text",
                arguments={"text": text, "target": "editor", "press_enter": press_enter},
                rationale=f"Type '{text}'",
                canonical_action=AliasMapper.canonicalize_action(
                    "type_text", text=text, target="editor", press_enter=press_enter
                ),
            )
        )
        return MacroPlan(goal=p_strip, steps=steps)

    # 7. Hotkey combinations:
    # Explicit: "hotkey <keys>", "shortcut <keys>", "send hotkey|shortcut|keys <keys>"
    # Or "press|send <keys>" where keys match hotkey pattern (contains '+' or standard key name)
    m_explicit_hotkey = re.search(r"^(?:hotkey|shortcut)\s+(?:keys?\s+)?(.+)$", p_strip, re.I)
    m_send_hotkey = re.search(r"^send\s+(?:hotkey|shortcut|keys?)\s+(.+)$", p_strip, re.I)
    m_press_send = re.search(r"^(?:press|send)\s+(.+)$", p_strip, re.I)

    hotkey_arg: Optional[str] = None
    if m_explicit_hotkey:
        hotkey_arg = m_explicit_hotkey.group(1).strip().strip('"\'')
    elif m_send_hotkey:
        hotkey_arg = m_send_hotkey.group(1).strip().strip('"\'')
    elif m_press_send:
        cand = m_press_send.group(1).strip().strip('"\'')
        if _is_hotkey_argument(cand):
            hotkey_arg = cand

    if hotkey_arg:
        clean_keys = re.sub(r"^the\s+", "", hotkey_arg, flags=re.I)
        clean_keys = re.sub(r"\s+keys?$", "", clean_keys, flags=re.I).strip()
        raw_keys = [k.strip() for k in re.split(r"[\+\s,\-]+", clean_keys) if k.strip()]
        keys_list = normalize_keys(raw_keys)
        if keys_list:
            steps.append(
                PlanStep(
                    step_index=1,
                    tool_name="send_hotkey",
                    arguments={"keys": keys_list},
                    rationale=f"Press hotkey '{'+'.join(keys_list)}'",
                    canonical_action=AliasMapper.canonicalize_action("send_hotkey", keys=keys_list),
                )
            )
            return MacroPlan(goal=p_strip, steps=steps)

    # 8. "click|press|select <target>"
    m_click = re.search(r"^(?:click|press|select)\s+(.+)$", p_strip, re.I)
    if m_click:
        target = m_click.group(1).strip().strip('"\'')
        steps.append(
            PlanStep(
                step_index=1,
                tool_name="click_element",
                arguments={"target": target},
                rationale=f"Click '{target}'",
                canonical_action=AliasMapper.canonicalize_action("click_element", target=target),
            )
        )
        return MacroPlan(goal=p_strip, steps=steps)

    # 9. "wait <duration> [seconds]"
    m_wait = re.search(r"^wait\s+([0-9.]+)\s*(?:s|sec|seconds?)?$", p_strip, re.I)
    if m_wait:
        dur = float(m_wait.group(1))
        steps.append(
            PlanStep(
                step_index=1,
                tool_name="wait",
                arguments={"duration_seconds": dur},
                rationale=f"Wait for {dur}s",
                canonical_action=AliasMapper.canonicalize_action("wait", duration_seconds=dur),
            )
        )
        return MacroPlan(goal=p_strip, steps=steps)

    return None


def _quote_bare_keys(s: str) -> str:
    """Safely quote bare/unquoted identifier keys in JSON-like text without corrupting string literals."""
    pattern = r'("(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\')|([{,]\s*)([a-zA-Z_][a-zA-Z0-9_]*)\s*:'

    def repl(m: re.Match) -> str:
        if m.group(1):
            return m.group(1)
        return f'{m.group(2)}"{m.group(3)}":'

    return re.sub(pattern, repl, s)


def parse_llm_tool_plan(raw_text: str, goal: str = "") -> MacroPlan:
    """Robustly extract and validate a MacroPlan from diverse LLM response formats.

    Implements a 7-tier resilient parsing pipeline:
    - Stage 1: Strip markdown code fences (```(?:json)? ... ```) and conversational preamble/postamble.
    - Stage 2: Bracket boundary extraction (slice from first [ or { to matching ] or }).
    - Stage 3: JSON syntax repair (trailing commas, single quotes to double quotes, clean unescaped newlines).
    - Stage 4: Root structure normalization (extract inner list from steps, sub_goals, plan, actions, tools; or wrap single step).
    - Stage 5: Argument unflattening: package flat keys (text, target, app_name, keys, button, duration_seconds) into arguments.
    - Stage 6: Action alias canonicalization via AliasMapper.canonicalize_action() assigned to step.canonical_action.
    - Stage 7: Deterministic fallback: if JSON parsing fails or produces no steps, invoke fallback regex extraction or deterministic intent parsing.
    """
    text = (raw_text or "").strip()
    if not text and not goal:
        return MacroPlan(goal=goal, steps=[], raw_response=raw_text)

    data: Any = None

    if text:
        # Stage 1: Strip Markdown Code Fences
        # Look specifically for ```json ... ``` code blocks first so thinking/text fences do not shadow the plan
        json_fences = list(re.finditer(r"```+json\s*([\s\S]*?)\s*```+", text, re.I))
        if json_fences:
            chosen = json_fences[0].group(1).strip()
            for jf in json_fences:
                c = jf.group(1).strip()
                if ("[" in c or "{" in c) and any(kw in c for kw in ("tool_name", "action", "step_index", "target", "app_name")):
                    chosen = c
                    break
            text = chosen
        else:
            # Check all fences, preferring one with JSON structure over plain text
            fences = list(re.finditer(r"```+\w*\s*([\s\S]*?)\s*```+", text, re.I))
            if fences:
                chosen = fences[0].group(1).strip()
                for f in fences:
                    f_content = f.group(1).strip()
                    if ("[" in f_content or "{" in f_content) and any(kw in f_content for kw in ("tool_name", "action", "step_index", "target", "app_name")):
                        chosen = f_content
                        break
                text = chosen

        # Stage 2: Bracket boundary extraction
        first_bracket = text.find("[")
        first_brace = text.find("{")

        start_idx = -1
        end_char = ""
        if first_bracket != -1 and (first_brace == -1 or first_bracket < first_brace):
            start_idx = first_bracket
            end_char = "]"
        elif first_brace != -1:
            start_idx = first_brace
            end_char = "}"

        if start_idx != -1:
            open_char = text[start_idx]
            depth = 0
            in_string = False
            escape = False
            end_idx = -1
            for i in range(start_idx, len(text)):
                ch = text[i]
                if escape:
                    escape = False
                    continue
                if ch == "\\":
                    if in_string:
                        escape = True
                    continue
                if ch == '"':
                    in_string = not in_string
                    continue
                if not in_string:
                    if ch == open_char:
                        depth += 1
                    elif ch == end_char:
                        depth -= 1
                        if depth == 0:
                            end_idx = i
                            break
            if end_idx != -1:
                text = text[start_idx:end_idx + 1]
            else:
                last_match = text.rfind(end_char)
                if last_match > start_idx:
                    text = text[start_idx:last_match + 1]

        # Stage 3: JSON parsing & syntax repair
        try:
            data = json.loads(text)
        except Exception:
            # 3a: Trailing commas removal
            repaired = re.sub(r",\s*([\]}])", r"\1", text)
            # 3b: Python booleans & None to JSON
            repaired = re.sub(r"\bTrue\b", "true", repaired)
            repaired = re.sub(r"\bFalse\b", "false", repaired)
            repaired = re.sub(r"\bNone\b", "null", repaired)
            # 3c: Support unquoted JSON identifier keys
            repaired = _quote_bare_keys(repaired)

            try:
                data = json.loads(repaired)
            except Exception:
                # 3d: Single quotes to double quotes for keys and strings
                sq_repaired = re.sub(r"(?<=[{\[,:\s])'([^'\\]*(?:\\.[^'\\]*)*)'(?=[\s,:\]}])", r'"\1"', repaired)
                sq_repaired = _quote_bare_keys(sq_repaired)
                try:
                    data = json.loads(sq_repaired)
                except Exception:
                    # 3e: Python ast.literal_eval fallback
                    try:
                        data = ast.literal_eval(repaired)
                    except Exception:
                        try:
                            data = ast.literal_eval(sq_repaired)
                        except Exception:
                            data = None

    # Stage 4: Root structure normalization
    raw_steps: List[Any] = []
    if isinstance(data, list):
        raw_steps = data
    elif isinstance(data, dict):
        found = False
        for k in ("steps", "sub_goals", "plan", "actions", "tools"):
            if k in data:
                val = data[k]
                if isinstance(val, list):
                    raw_steps = val
                    found = True
                    break
                elif isinstance(val, dict):
                    for inner_k in ("steps", "sub_goals", "actions", "tools"):
                        if inner_k in val and isinstance(val[inner_k], list):
                            raw_steps = val[inner_k]
                            found = True
                            break
            if found:
                break
        if not found:
            # Single step dictionary
            if any(key in data for key in ("tool_name", "action", "name", "action_type", "type", "target", "app_name", "text", "keys")):
                raw_steps = [data]

    # Stage 5 & 6: Argument unflattening & Action alias canonicalization
    steps: List[PlanStep] = []
    for idx, item in enumerate(raw_steps, start=1):
        if not isinstance(item, dict):
            continue
        tool_name = item.get("tool_name") or item.get("action") or item.get("name") or item.get("type") or "click_element"
        args = item.get("arguments") or item.get("args") or item.get("parameters") or {}
        if not isinstance(args, dict):
            args = {}
        else:
            args = dict(args)

        # Stage 5: Argument unflattening
        for key in ("target", "text", "app_name", "app", "keys", "button", "duration_seconds", "clear_first", "press_enter"):
            if key in item and key not in args:
                if key == "app" and "app_name" not in args:
                    args["app_name"] = item[key]
                else:
                    args[key] = item[key]

        # Stage 6: Action alias canonicalization
        canon = None
        try:
            canon = AliasMapper.canonicalize_action(raw_action=str(tool_name), **args)
        except Exception:
            canon = None

        step = PlanStep(
            step_index=item.get("step_index", idx),
            tool_name=str(tool_name),
            arguments=args,
            rationale=item.get("rationale", ""),
            canonical_action=canon,
        )
        steps.append(step)

    # Stage 7: Deterministic fallback
    if not steps:
        # 7a: Heuristic regex extraction from raw text
        if text:
            heuristic_plan = _extract_heuristic_steps(raw_text, goal)
            if heuristic_plan.steps:
                return heuristic_plan

        # 7b: Deterministic intent parsing
        if goal:
            fallback = parse_deterministic_intent(goal)
            if fallback and fallback.steps:
                return fallback

    return MacroPlan(goal=goal, steps=steps, raw_response=raw_text)


def _extract_heuristic_steps(text: str, goal: str) -> MacroPlan:
    """Fallback parser for malformed text from smaller edge models."""
    steps: List[PlanStep] = []
    # Find tool-like invocations: tool_name(...)
    matches = re.finditer(r"([a-zA-Z_]+)\s*\((.*?)\)", text)
    idx = 1
    for m in matches:
        fn_name = m.group(1).lower()
        args_str = m.group(2)
        args: Dict[str, Any] = {}
        # Parse simple key="value" or positional strings
        kv_pairs = re.findall(r"([a-zA-Z_]+)\s*=\s*[\"']?([^\"',]*)[\"']?", args_str)
        for k, v in kv_pairs:
            args[k] = v
        if not args and args_str.strip():
            raw_val = args_str.strip().strip('"\'')
            if "type" in fn_name:
                args["text"] = raw_val
            elif "app" in fn_name:
                args["app_name"] = raw_val
            else:
                args["target"] = raw_val

        try:
            canon = AliasMapper.canonicalize_action(raw_action=fn_name, **args)
            steps.append(
                PlanStep(
                    step_index=idx,
                    tool_name=fn_name,
                    arguments=args,
                    canonical_action=canon,
                )
            )
            idx += 1
        except Exception:
            pass

    return MacroPlan(goal=goal, steps=steps, raw_response=text)
