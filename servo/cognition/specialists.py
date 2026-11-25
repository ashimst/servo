"""Needle specialists: one Needle agent per tool, grouped by kind of interaction.

Measured on Needle 3 with planner steps:
  * one Needle with 14 general tools: 15/21 steps right; "wait 2" became close(), "right
    click [13]" lost its button, "set [2] to 03" put 03 in the target;
  * one Needle per interaction type (2-6 tools each): 19/31 — siblings still confused
    (click → double_click, wait → close_window, go to → search_web);
  * one Needle per tool, value-first schemas with an integer ``control``, quoted value
    before the bracket ("set "03" in [2]"): every tool and argument right on the cases that
    failed before.

So the step's form picks the tool (the verb decides it, deterministically), the tool's own
Needle agent fills in the arguments, and ``check_call`` in needle.py verifies them against
the step's words. Interaction types (pointer, keyboard, form, ...) group the tools; planner
agents (servo/cognition/planners.py) are each allowed some interaction types.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

_S = {"type": "string"}
_CONTROL = {"type": "integer", "description": "the number in square brackets"}
_OPT_CONTROL = {"type": "integer", "description": "the number in square brackets, if the step has one"}


def _tool(name: str, description: str, props: Dict[str, Dict[str, Any]], required: List[str]) -> Dict[str, Any]:
    return {"name": name, "description": description,
            "parameters": {"type": "object", "properties": props, "required": required}}


# Two-argument tools list the quoted value first and the control second: Needle 3 placed the
# arguments right in that order (5/6 vs 1/6 the other way round).
TOOLS: Dict[str, Dict[str, Any]] = {t["name"]: t for t in [
    # pointer
    _tool("click", "Left-click a control.", {"control": _CONTROL}, ["control"]),
    _tool("double_click", "Double-click a control.", {"control": _CONTROL}, ["control"]),
    _tool("right_click", "Right-click a control to open its context menu.", {"control": _CONTROL}, ["control"]),
    _tool("scroll_up", "Scroll up, on the page or inside a control.", {"control": _OPT_CONTROL}, []),
    _tool("scroll_down", "Scroll down, on the page or inside a control.", {"control": _OPT_CONTROL}, []),
    # keyboard
    _tool("type_into", "Type text into a control.",
          {"text": {**_S, "description": "the quoted text"}, "control": _CONTROL,
           "press_enter": {"type": "boolean", "description": "true if the step says to press enter"}},
          ["text", "control"]),
    _tool("type_text", "Type text into the focused field.",
          {"text": {**_S, "description": "the quoted text"},
           "press_enter": {"type": "boolean", "description": "true if the step says to press enter"}}, ["text"]),
    _tool("press", "Press a key or keyboard shortcut.",
          {"keys": {**_S, "description": "keys joined with +, e.g. ctrl+s, enter, alt+f4"}}, ["keys"]),
    _tool("clear", "Empty a text field.", {"control": _CONTROL}, ["control"]),
    # form
    _tool("turn_on", "Turn a switch or checkbox on.", {"control": _CONTROL}, ["control"]),
    _tool("turn_off", "Turn a switch or checkbox off.", {"control": _CONTROL}, ["control"]),
    _tool("set_value", "Put a value into a field, slider, spinner or picker.",
          {"value": {**_S, "description": "the quoted value"}, "control": _CONTROL}, ["value", "control"]),
    _tool("choose", "Choose an option in a dropdown or list.",
          {"option": {**_S, "description": "the quoted option"}, "control": _CONTROL}, ["option", "control"]),
    _tool("expand", "Expand a collapsed section or tree item.", {"control": _CONTROL}, ["control"]),
    _tool("collapse", "Collapse an expanded section or tree item.", {"control": _CONTROL}, ["control"]),
    # window
    _tool("open_app", "Open an app by name.", {"name": {**_S, "description": "the app name"}}, ["name"]),
    _tool("open_settings", "Open a Windows Settings page.",
          {"page": {**_S, "description": "the page id, e.g. dateandtime, display, bluetooth"}}, ["page"]),
    _tool("close_window", "Close a window or app.",
          {"window": {**_S, "description": "the quoted window or app name; empty for the current one"}}, []),
    _tool("switch_to", "Bring an open window to the front.",
          {"window": {**_S, "description": "the quoted window title"}}, ["window"]),
    _tool("wait", "Wait some seconds.", {"seconds": {"type": "number", "description": "seconds"}}, ["seconds"]),
    # web
    _tool("open_web", "Open a web address in the user's browser.",
          {"address": {**_S, "description": "the web address"}}, ["address"]),
    _tool("search_web", "Search the web.", {"query": {**_S, "description": "what to search for"}}, ["query"]),
    # files
    _tool("read_file", "Read a text file.", {"path": {**_S, "description": "the file path"}}, ["path"]),
    _tool("list_folder", "List a folder's contents.", {"path": {**_S, "description": "folder path or name"}}, ["path"]),
    _tool("write_file", "Write text to a file, replacing it.",
          {"text": {**_S, "description": "the quoted text"}, "path": {**_S, "description": "the file path"}},
          ["text", "path"]),
    _tool("append_file", "Append text to the end of a file.",
          {"text": {**_S, "description": "the quoted text"}, "path": {**_S, "description": "the file path"}},
          ["text", "path"]),
    # system
    _tool("run_command", "Run one PowerShell command and return its output.",
          {"command": {**_S, "description": "the quoted command"}}, ["command"]),
    # observe
    _tool("describe_screen", "Describe what is visible in the window (objects, layout, text).", {}, []),
    _tool("read_text", "Read all the text visible in the window.", {}, []),
]}


@dataclass(frozen=True)
class Specialist:
    """One kind of interaction: its tools and the instructions each tool's agent gets."""
    name: str
    instructions: str
    tools: Tuple[str, ...]


_COMMON = ("Fill in the arguments of the one tool from the step. Copy numbers and quoted text exactly; "
           "never invent a number or text that is not in the step.")

SPECIALISTS: Dict[str, Specialist] = {s.name: s for s in [
    Specialist("pointer", _COMMON + " The control is the number in square brackets.",
               ("click", "double_click", "right_click", "scroll_up", "scroll_down")),
    Specialist("keyboard", _COMMON + " The text is between the quotes.", ("type_into", "type_text", "press", "clear")),
    Specialist("form", _COMMON + " The value or option is between the quotes; the control is the number in "
               "square brackets.", ("turn_on", "turn_off", "set_value", "choose", "expand", "collapse")),
    Specialist("window", _COMMON, ("open_app", "open_settings", "close_window", "switch_to", "wait")),
    Specialist("web", _COMMON, ("open_web", "search_web")),
    Specialist("files", _COMMON + " The text is between the quotes.", ("read_file", "list_folder", "write_file", "append_file")),
    Specialist("system", _COMMON + " The command is between the quotes.", ("run_command",)),
    Specialist("observe", _COMMON, ("describe_screen", "read_text")),
]}
SPECIALIST_OF: Dict[str, str] = {t: s.name for s in SPECIALISTS.values() for t in s.tools}


@dataclass(frozen=True)
class StepForm:
    pattern: "re.Pattern[str]"
    tool: str
    example: str

    @property
    def specialist(self) -> str:
        return SPECIALIST_OF[self.tool]


def _f(rx: str, tool: str, example: str) -> StepForm:
    return StepForm(re.compile(rx, re.I), tool, example)


# The planner's step forms. Each pattern matches the WHOLE step (a step that only starts
# like a form, e.g. 'type "x" into the File name box', is not that form). Order matters:
# the first match wins.
_N = r"\[\d{1,3}\]"            # a control number
_Q = r"[\"“”].*[\"“”]"          # quoted text (may itself contain quotes)
_ENTER = r"(?: and press enter)?"
STEP_FORMS: List[StepForm] = [
    _f(rf"^double[- ]click {_N}$", "double_click", "double click [12]"),
    _f(rf"^right[- ]click {_N}$", "right_click", "right click [12]"),
    _f(rf"^click {_N}$", "click", "click [12]"),
    _f(rf"^scroll up(?: in {_N})?$", "scroll_up", "scroll up   /   scroll up in [3]"),
    _f(rf"^scroll(?: down)?(?: in {_N})?$", "scroll_down", "scroll down   /   scroll down in [3]"),
    _f(rf"^type {_Q} into {_N}{_ENTER}$", "type_into", 'type "hello" into [4]'),
    _f(rf"^type {_Q}{_ENTER}$", "type_text", 'type "hello"   (into the focused field; add "and press enter" if needed)'),
    _f(r"^press \S+$", "press", "press ctrl+s"),
    _f(rf"^clear {_N}$", "clear", "clear [4]"),
    _f(rf"^turn on {_N}$", "turn_on", "turn on [7]"),
    _f(rf"^turn off {_N}$", "turn_off", "turn off [7]"),
    _f(rf"^set {_Q} in {_N}$", "set_value", 'set "03" in [9]'),
    _f(rf"^(?:choose|select) {_Q} in {_N}$", "choose", 'choose "Large" in [9]'),
    _f(rf"^expand {_N}$", "expand", "expand [5]"),
    _f(rf"^collapse {_N}$", "collapse", "collapse [5]"),
    _f(r"^open settings [\w:-]+$", "open_settings", "open settings dateandtime"),
    _f(r"^open (?!settings\b)\S.*$", "open_app", "open notepad"),
    _f(rf"^close {_Q}$", "close_window", 'close "draft.txt - Notepad"'),
    _f(rf"^switch to {_Q}$", "switch_to", 'switch to "Downloads - File Explorer"'),
    _f(r"^wait \d+(?:\.\d+)?(?: seconds?)?$", "wait", "wait 2"),
    _f(r"^go to \S+$", "open_web", "go to github.com"),
    _f(r"^search the web for \S.*$", "search_web", "search the web for weather in Kathmandu"),
    _f(r"^read file \S.*$", "read_file", "read file C:/Users/name/notes.txt"),
    _f(r"^list folder \S.*$", "list_folder", "list folder downloads"),
    _f(rf"^write {_Q} to file \S.*$", "write_file", 'write "text" to file C:/Users/name/notes.txt'),
    _f(rf"^append {_Q} to file \S.*$", "append_file", 'append "text" to file C:/Users/name/notes.txt'),
    _f(rf"^run command {_Q}$", "run_command", 'run command "Get-Date"'),
    _f(r"^describe screen$", "describe_screen", "describe screen"),
    _f(r"^read text$", "read_text", "read text"),
]

# First words of each form, to tell a planner which form it got wrong.
_VERBS = [(re.compile(rx, re.I), ex) for rx, ex in (
    (r"^double[- ]click\b", "double click [12]"), (r"^right[- ]click\b", "right click [12]"),
    (r"^click\b", "click [12]"), (r"^scroll\b", "scroll down in [3]"),
    (r"^type\b", 'type "hello" into [4]   or   type "hello"'), (r"^press\b", "press ctrl+s"),
    (r"^clear\b", "clear [4]"), (r"^turn\b", "turn on [7]"), (r"^set\b", 'set "03" in [9]'),
    (r"^(?:choose|select)\b", 'choose "Large" in [9]'), (r"^expand\b", "expand [5]"),
    (r"^collapse\b", "collapse [5]"), (r"^close\b", 'close "draft.txt - Notepad"'),
    (r"^switch\b", 'switch to "Downloads - File Explorer"'), (r"^wait\b", "wait 2"),
    (r"^go to\b", "go to github.com"), (r"^write\b", 'write "text" to file C:/Users/name/notes.txt'),
    (r"^append\b", 'append "text" to file C:/Users/name/notes.txt'), (r"^run\b", 'run command "Get-Date"'),
)]


def form_hint(step_text: str) -> str:
    """For a step that is no form: the form its first word suggests, or ""."""
    text = " ".join((step_text or "").split())
    return next((ex for rx, ex in _VERBS if rx.match(text)), "")


def step_form(step_text: str) -> Optional[StepForm]:
    """The form a planner step uses, or None when it uses none of them."""
    text = " ".join((step_text or "").split())
    return next((f for f in STEP_FORMS if f.pattern.match(text)), None)


def all_tool_schemas() -> List[Dict[str, Any]]:
    return list(TOOLS.values())
