"""Structured Tool Calling and System 2 Macro Plan Generation.

See ADR-10 in SERVO_ARCHITECTURE_V1.md and SERVO_ARCHITECTURE_V3.md.
Provides JSON schema tool declarations, structured plan decomposition,
small-model few-shot prompt optimization, and error recovery parsing.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple, Union
from pydantic import BaseModel, Field

from servo.actuation.alias_mapping import AliasMapper, CanonicalAction, CanonicalActionType
from servo.config import settings
from servo.perception.scene_index import SceneIndex

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


class MacroPlan(BaseModel):
    """Structured execution plan decomposed by System 2."""
    goal: str
    steps: List[PlanStep] = Field(default_factory=list)
    estimated_latency_ms: int = 0
    raw_response: str = ""


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


def parse_llm_tool_plan(raw_text: str, goal: str = "") -> MacroPlan:
    """Robustly extract and validate a MacroPlan from diverse LLM response formats."""
    text = raw_text.strip()
    if not text:
        return MacroPlan(goal=goal, steps=[], raw_response=raw_text)

    # 1. Strip Markdown Code Fences (```json ... ```)
    m_fence = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text, re.I)
    if m_fence:
        text = m_fence.group(1).strip()

    # 2. Extract JSON Array or Object
    json_match = re.search(r"(\[[\s\S]*\]|\{[\s\S]*\})", text)
    if json_match:
        text = json_match.group(1)

    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        logger.warning("Failed to decode JSON from LLM: %s. Raw: %s", e, raw_text[:200])
        # Attempt heuristic regex extraction for small models
        return _extract_heuristic_steps(raw_text, goal)

    raw_steps = []
    if isinstance(data, list):
        raw_steps = data
    elif isinstance(data, dict):
        if "steps" in data and isinstance(data["steps"], list):
            raw_steps = data["steps"]
        elif "tool_name" in data:
            raw_steps = [data]
        else:
            raw_steps = []

    steps: List[PlanStep] = []
    for idx, item in enumerate(raw_steps, start=1):
        if not isinstance(item, dict):
            continue
        tool_name = item.get("tool_name") or item.get("action") or item.get("name") or "click_element"
        args = item.get("arguments") or item.get("args") or {}
        if not isinstance(args, dict):
            args = {}
        # Pull flat fields if nested arguments weren't provided
        for key in ("target", "text", "app_name", "keys", "button", "duration_seconds"):
            if key in item and key not in args:
                args[key] = item[key]

        try:
            canon = AliasMapper.canonicalize_action(raw_action=tool_name, **args)
        except Exception:
            canon = None

        step = PlanStep(
            step_index=item.get("step_index", idx),
            tool_name=tool_name,
            arguments=args,
            rationale=item.get("rationale", ""),
            canonical_action=canon,
        )
        steps.append(step)

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
