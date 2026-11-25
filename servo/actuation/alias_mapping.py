"""Action and Element Alias Mapping Engine for Servo.

Provides canonical translation of fuzzy natural language intents, LLM tool calls,
and user phrasings into concrete, deterministic invocations on NeedleToolSurface
and Desktop Control subsystems.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Any, Dict, Iterable, List, Optional, Tuple
from pydantic import BaseModel, Field

from servo.perception.scene_index import SceneElement, SceneIndex
from servo.actuation.sandboxing import normalize_keys, split_hotkeys


class CanonicalActionType(str, Enum):
    CLICK = "click"
    DOUBLE_CLICK = "double_click"
    RIGHT_CLICK = "right_click"
    INVOKE = "invoke"
    TYPE = "type"
    HOTKEY = "hotkey"
    SCROLL = "scroll"
    DRAG = "drag"
    LAUNCH = "launch"
    CLOSE = "close"
    WAIT = "wait"
    INSPECT = "inspect"
    # Set an outcome, verified by reading the control back (servo.actuation.patterns).
    SET_TOGGLE = "set_toggle"
    SET_EXPANDED = "set_expanded"
    SELECT_OPTION = "select_option"
    SET_VALUE = "set_value"


class CanonicalAction(BaseModel):
    """Normalized, validated desktop control action ready for System 1 dispatch."""
    action_type: CanonicalActionType
    target: Optional[str] = None
    text: str = ""
    keys: List[str] = Field(default_factory=list)
    app_name: Optional[str] = None
    button: str = "left"
    clear_first: bool = False
    press_enter: bool = False
    duration_seconds: float = 0.0
    description: str = ""
    state: str = ""   # set_toggle: on/off · set_expanded: expand/collapse


# Comprehensive dictionary of natural language & tool aliases mapping to CanonicalActionType
ACTION_ALIAS_MAP: Dict[str, CanonicalActionType] = {
    "set_toggle": CanonicalActionType.SET_TOGGLE,
    "set_expanded": CanonicalActionType.SET_EXPANDED,
    "select_option": CanonicalActionType.SELECT_OPTION,
    "set_value": CanonicalActionType.SET_VALUE,
    "clear": CanonicalActionType.SET_VALUE,
    "clear_field": CanonicalActionType.SET_VALUE,
    "clear_text": CanonicalActionType.SET_VALUE,
    # Click family
    "click": CanonicalActionType.CLICK,
    "press": CanonicalActionType.CLICK,
    "tap": CanonicalActionType.CLICK,
    "hit": CanonicalActionType.CLICK,
    "push": CanonicalActionType.CLICK,
    "select": CanonicalActionType.CLICK,
    "choose": CanonicalActionType.CLICK,
    "activate": CanonicalActionType.CLICK,
    "click_element": CanonicalActionType.CLICK,
    "button_click": CanonicalActionType.CLICK,

    # Invoke / Pattern activation family
    "invoke": CanonicalActionType.INVOKE,
    "uia_invoke": CanonicalActionType.INVOKE,
    "element_invoke": CanonicalActionType.INVOKE,
    "pattern_invoke": CanonicalActionType.INVOKE,
    "trigger": CanonicalActionType.INVOKE,
    "invoke_element": CanonicalActionType.INVOKE,
    "activate_element": CanonicalActionType.INVOKE,
    "pattern_activate": CanonicalActionType.INVOKE,
    "uia_activate": CanonicalActionType.INVOKE,

    # Double click family
    "double_click": CanonicalActionType.DOUBLE_CLICK,
    "double-click": CanonicalActionType.DOUBLE_CLICK,
    "dblclick": CanonicalActionType.DOUBLE_CLICK,
    "doubleclick": CanonicalActionType.DOUBLE_CLICK,
    "open_item": CanonicalActionType.DOUBLE_CLICK,
    "double_tap": CanonicalActionType.DOUBLE_CLICK,

    # Right click family
    "right_click": CanonicalActionType.RIGHT_CLICK,
    "right-click": CanonicalActionType.RIGHT_CLICK,
    "rightclick": CanonicalActionType.RIGHT_CLICK,
    "context_menu": CanonicalActionType.RIGHT_CLICK,
    "contextmenu": CanonicalActionType.RIGHT_CLICK,

    # Type / Text input family
    "type": CanonicalActionType.TYPE,
    "write": CanonicalActionType.TYPE,
    "enter": CanonicalActionType.TYPE,
    "input": CanonicalActionType.TYPE,
    "fill": CanonicalActionType.TYPE,
    "set_text": CanonicalActionType.TYPE,
    "type_text": CanonicalActionType.TYPE,
    "send_keys": CanonicalActionType.TYPE,
    "append": CanonicalActionType.TYPE,
    "populate": CanonicalActionType.TYPE,

    # Hotkey / Key combination family
    "hotkey": CanonicalActionType.HOTKEY,
    "shortcut": CanonicalActionType.HOTKEY,
    "key_combo": CanonicalActionType.HOTKEY,
    "send_hotkey": CanonicalActionType.HOTKEY,
    "press_keys": CanonicalActionType.HOTKEY,
    "key": CanonicalActionType.HOTKEY,

    # Scroll family
    "scroll": CanonicalActionType.SCROLL,
    "scroll_down": CanonicalActionType.SCROLL,
    "scroll_up": CanonicalActionType.SCROLL,
    "wheel": CanonicalActionType.SCROLL,
    "mouse_wheel": CanonicalActionType.SCROLL,

    # Drag family
    "drag": CanonicalActionType.DRAG,
    "drag_drop": CanonicalActionType.DRAG,
    "drag_and_drop": CanonicalActionType.DRAG,

    # App lifecycle family
    "launch": CanonicalActionType.LAUNCH,
    "open": CanonicalActionType.LAUNCH,
    "start": CanonicalActionType.LAUNCH,
    "run": CanonicalActionType.LAUNCH,
    "launch_app": CanonicalActionType.LAUNCH,
    "open_app": CanonicalActionType.LAUNCH,
    "switch_to": CanonicalActionType.LAUNCH,
    "focus_app": CanonicalActionType.LAUNCH,

    # Close family
    "close": CanonicalActionType.CLOSE,
    "quit": CanonicalActionType.CLOSE,
    "exit": CanonicalActionType.CLOSE,
    "kill": CanonicalActionType.CLOSE,
    "terminate": CanonicalActionType.CLOSE,
    "close_window": CanonicalActionType.CLOSE,
    "close_app": CanonicalActionType.CLOSE,

    # Wait family
    "wait": CanonicalActionType.WAIT,
    "sleep": CanonicalActionType.WAIT,
    "pause": CanonicalActionType.WAIT,
    "delay": CanonicalActionType.WAIT,

    # Inspect family
    "inspect": CanonicalActionType.INSPECT,
    "read": CanonicalActionType.INSPECT,
    "get_text": CanonicalActionType.INSPECT,
    "find_elements": CanonicalActionType.INSPECT,
    "sense": CanonicalActionType.INSPECT,
    "inspect_scene": CanonicalActionType.INSPECT,
}

_NUMBER_WORDS = ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine")
_SYMBOL_WORDS = {
    "+": ("plus", "add"), "-": ("minus", "subtract"), "*": ("multiply by", "multiply", "times"),
    "x": ("multiply by",), "/": ("divide by", "divide"), "=": ("equals",), ".": ("decimal separator",),
    "%": ("percent",),
}


def _norm_label(s: str) -> str:
    s = " ".join(str(s or "").lower().split())
    return re.sub(r"[\.\:'\"…]+$", "", s).strip()


_EDITABLE = ("Edit", "Document")
_GENERIC_EDITOR_HINTS = {"editor", "text editor", "the editor", "focused field", "text box", "input", "document",
                         "text area", "focused editor"}


def _area(e: SceneElement) -> int:
    return max(0, e.bbox_screen[2] - e.bbox_screen[0]) * max(0, e.bbox_screen[3] - e.bbox_screen[1])


_ROLES = {r.lower(): r for r in (
    "Button", "CheckBox", "ComboBox", "Custom", "DataItem", "Document", "Edit", "Group", "Hyperlink", "List",
    "ListItem", "Menu", "MenuItem", "Pane", "RadioButton", "Slider", "Spinner", "SplitButton", "Tab", "TabItem",
    "Text", "ToolBar", "TreeItem")}
_ROLE_PREFIX = re.compile(r"^([A-Za-z]+):\s*(\S.*)$")


def _contains(outer: SceneElement, inner: SceneElement) -> bool:
    a, b = outer.bbox_screen, inner.bbox_screen
    return a[0] <= b[0] and a[1] <= b[1] and b[2] <= a[2] and b[3] <= a[3]


def _fits(e: SceneElement, action_type: CanonicalActionType) -> bool:
    t = action_type
    if t in (CanonicalActionType.TYPE, CanonicalActionType.SET_VALUE):
        return e.role in _EDITABLE or e.role in ("ComboBox", "Spinner", "Slider") or \
            (t == CanonicalActionType.SET_VALUE and e.role == "Custom")
    if t == CanonicalActionType.SET_TOGGLE:
        return getattr(e, "toggle", None) is not None or e.role in ("CheckBox", "RadioButton")
    if t == CanonicalActionType.SET_EXPANDED:
        return getattr(e, "expanded", None) is not None
    if t == CanonicalActionType.SELECT_OPTION:
        return e.role in ("ComboBox", "List") or getattr(e, "expanded", None) is not None
    return getattr(e, "interactive", True)


def _prefer(cands: List[SceneElement], action_type: CanonicalActionType) -> Optional[SceneElement]:
    """One element from same-labelled candidates, or None if they are genuinely different."""
    pool = [e for e in cands if getattr(e, "enabled", True)] or list(cands)
    fit = [e for e in pool if _fits(e, action_type)]
    pool = fit or pool
    if len(pool) == 1:
        return pool[0]
    if action_type in (CanonicalActionType.TYPE, CanonicalActionType.SET_VALUE):
        edits = [e for e in pool if e.role in _EDITABLE]
        if len(edits) == 1:
            return edits[0]
    # Nested duplicates (a combo box and the edit inside it) are one control: typing goes
    # to the innermost, anything else to the outermost.
    if all(_contains(pool[0], e) or _contains(e, pool[0]) for e in pool[1:]):
        inner = action_type in (CanonicalActionType.TYPE, CanonicalActionType.SET_VALUE)
        return (min if inner else max)(pool, key=_area)
    return None


def _row_partner(texts: List[SceneElement], controls: List[SceneElement], q: str,
                 action_type: CanonicalActionType) -> Optional[SceneElement]:
    """The control that a text label on the same row names ("File name:" -> its box)."""
    labels = [t for t in texts if _norm_label(t.label) == q] or              [t for t in texts if _norm_label(t.label).startswith(q) and len(q) >= 4]
    if len(labels) != 1:
        return None
    lab = labels[0]
    cy = (lab.bbox_screen[1] + lab.bbox_screen[3]) / 2
    row = [c for c in controls
           if c.bbox_screen[1] - 8 <= cy <= c.bbox_screen[3] + 8 and c.bbox_screen[0] >= lab.bbox_screen[0] - 4]
    below = [c for c in controls
             if 0 <= c.bbox_screen[1] - lab.bbox_screen[3] <= 40
             and c.bbox_screen[0] <= lab.bbox_screen[2] and lab.bbox_screen[0] <= c.bbox_screen[2]]
    for group in (row, below):
        fit = [c for c in group if _fits(c, action_type)] or group
        if fit:
            return min(fit, key=lambda c: abs(c.bbox_screen[0] - lab.bbox_screen[2]) + abs(c.bbox_screen[1] - lab.bbox_screen[1]))
    return None


def _label_synonyms(q: str) -> Tuple[str, ...]:
    """Other spellings of a short target: '7' -> 'seven', 'seven' -> '7', '+' -> 'plus'."""
    q = q.strip().lower()
    if q.isdigit() and len(q) == 1:
        return (_NUMBER_WORDS[int(q)],)
    if q in _NUMBER_WORDS:
        return (str(_NUMBER_WORDS.index(q)),)
    return _SYMBOL_WORDS.get(q, ())


# Generic target categories that fall back to common control types if exact label is missing
ROLE_FALLBACK_ALIASES = {
    "editor": ("Document", "Edit"),
    "text editor": ("Document", "Edit"),
    "text": ("Document", "Edit"),
    "document": ("Document", "Edit"),
    "input": ("Edit", "Document"),
    "body": ("Document", "Edit"),
    "textarea": ("Document", "Edit"),
    "edit": ("Edit", "Document"),
    "search": ("Edit",),
    "search bar": ("Edit",),
    "search box": ("Edit",),
}


class AliasMapper:
    """Canonicalization engine for tool names, arguments, and UI control targets."""

    @staticmethod
    def canonicalize_action(
        raw_action: str,
        target: Optional[str] = None,
        text: str = "",
        keys: Optional[Any] = None,
        app_name: Optional[str] = None,
        button: str = "left",
        clear_first: bool = False,
        press_enter: bool = False,
        duration_seconds: float = 0.0,
        prefer_invoke: bool = False,
        **extra_kwargs: Any,
    ) -> CanonicalAction:
        """Translate a raw action name and kwargs into a CanonicalAction."""
        norm_name = re.sub(r"[\s\-_]+", "_", raw_action.strip().lower())
        canon_type = ACTION_ALIAS_MAP.get(norm_name)

        # Support "activate" routing to INVOKE if prefer_invoke or uia requested
        if norm_name == "activate":
            if prefer_invoke or extra_kwargs.get("prefer_invoke") or extra_kwargs.get("uia") or extra_kwargs.get("use_invoke"):
                canon_type = CanonicalActionType.INVOKE
            else:
                canon_type = CanonicalActionType.CLICK

        if canon_type is None:
            # Check prefixes
            if norm_name.startswith("click"):
                canon_type = CanonicalActionType.CLICK
            elif norm_name.startswith("invoke"):
                canon_type = CanonicalActionType.INVOKE
            elif norm_name.startswith("type") or norm_name.startswith("write"):
                canon_type = CanonicalActionType.TYPE
            elif norm_name.startswith("open") or norm_name.startswith("launch"):
                canon_type = CanonicalActionType.LAUNCH
            elif norm_name.startswith("close"):
                canon_type = CanonicalActionType.CLOSE
            elif norm_name.startswith("scroll"):
                canon_type = CanonicalActionType.SCROLL
            elif norm_name.startswith("drag"):
                canon_type = CanonicalActionType.DRAG
            else:
                raise ValueError(
                    f"Unknown action alias '{raw_action}'. Available types: "
                    f"{', '.join(sorted(set(t.value for t in ACTION_ALIAS_MAP.values())))}"
                )

        btn = button
        if canon_type == CanonicalActionType.DOUBLE_CLICK:
            btn = "double"
        elif canon_type == CanonicalActionType.RIGHT_CLICK:
            btn = "right"

        # Parameter synonym mapping
        # text: value, content, input
        if not text:
            for text_key in ("value", "content", "input"):
                if text_key in extra_kwargs and extra_kwargs[text_key] is not None:
                    text = str(extra_kwargs[text_key])
                    break

        # target: element, control, label, selector, target_hint, target_element
        if target is None or (isinstance(target, str) and not target.strip()):
            for target_key in ("element", "control", "label", "selector", "target_hint", "target_element"):
                if target_key in extra_kwargs and extra_kwargs[target_key] is not None:
                    target = extra_kwargs[target_key]
                    break

        if target is not None:
            target = str(target).strip()
            if not target:
                target = None

        # app_name: app, application, process
        if app_name is None or (isinstance(app_name, str) and not app_name.strip()):
            for app_key in ("app", "application", "process"):
                if app_key in extra_kwargs and extra_kwargs[app_key] is not None:
                    app_name = extra_kwargs[app_key]
                    break

        # keys: key, shortcut, combo, key_combo
        if not keys:
            for keys_key in ("key", "shortcut", "combo", "key_combo"):
                if keys_key in extra_kwargs and extra_kwargs[keys_key] is not None:
                    keys = extra_kwargs[keys_key]
                    break

        # duration_seconds: seconds, duration, sleep_seconds, time
        dur_raw: Any = duration_seconds
        if dur_raw == 0.0 or dur_raw is None:
            for dur_key in ("seconds", "duration", "sleep_seconds", "time"):
                if dur_key in extra_kwargs and extra_kwargs[dur_key] is not None:
                    dur_raw = extra_kwargs[dur_key]
                    break

        dur_float = 0.0
        if dur_raw is not None:
            try:
                dur_float = float(dur_raw)
            except (ValueError, TypeError):
                if canon_type == CanonicalActionType.WAIT:
                    raise ValueError("Action 'wait' requires a numeric 'duration_seconds' parameter.")
                dur_float = 0.0
        duration_seconds = dur_float

        # Hotkey normalizations and validation
        hotkeys: List[str] = []
        if keys is not None:
            if isinstance(keys, str):
                hotkeys = split_hotkeys(keys)
            elif isinstance(keys, Iterable):
                for k in keys:
                    if not isinstance(k, str):
                        raise ValueError("Action 'hotkey' keys parameter must contain string key names.")
                    sk = k.strip()
                    if sk:
                        hotkeys.append(sk)
            else:
                raise ValueError("Action 'hotkey' keys parameter must be a string or iterable of strings.")
        try:
            hotkeys = normalize_keys(hotkeys)
        except Exception as e:
            raise ValueError(f"Failed to normalize keys: {e}") from e

        state = str(extra_kwargs.get("state") or "").strip().lower()
        if canon_type in (CanonicalActionType.SELECT_OPTION, CanonicalActionType.SET_VALUE) and not text:
            text = str(extra_kwargs.get("option") or extra_kwargs.get("value") or "")

        # Parameter validation
        if canon_type in (CanonicalActionType.SET_TOGGLE, CanonicalActionType.SET_EXPANDED,
                          CanonicalActionType.SELECT_OPTION, CanonicalActionType.SET_VALUE):
            if not target:
                raise ValueError(f"Action '{canon_type.value}' requires a non-empty 'target' parameter.")
            # An explicit empty value is "clear this field"; a missing one is an error.
            clearing = canon_type == CanonicalActionType.SET_VALUE and (
                extra_kwargs.get("value") == "" or norm_name in ("clear", "clear_field", "clear_text")
            )
            if canon_type in (CanonicalActionType.SELECT_OPTION, CanonicalActionType.SET_VALUE) and not text and not clearing:
                raise ValueError(f"Action '{canon_type.value}' requires the option/value to set.")
        elif canon_type == CanonicalActionType.TYPE:
            if not text:
                raise ValueError("Action 'type' requires a non-empty 'text' parameter.")
        elif canon_type == CanonicalActionType.HOTKEY:
            if not hotkeys:
                raise ValueError("Action 'hotkey' requires a non-empty 'keys' parameter.")
        elif canon_type in (
            CanonicalActionType.CLICK,
            CanonicalActionType.DOUBLE_CLICK,
            CanonicalActionType.RIGHT_CLICK,
            CanonicalActionType.INVOKE,
        ):
            if not target or not target.strip():
                raise ValueError(f"Action '{canon_type.value}' requires a non-empty 'target' parameter.")
        elif canon_type == CanonicalActionType.LAUNCH:
            if not app_name or not str(app_name).strip():
                if target and str(target).strip():
                    app_name = str(target).strip()
                else:
                    raise ValueError("Action 'launch' requires an 'app_name' parameter.")
            else:
                app_name = str(app_name).strip()
        elif canon_type == CanonicalActionType.WAIT:
            if duration_seconds < 0:
                raise ValueError("Action 'wait' requires a non-negative 'duration_seconds' parameter.")

        desc = f"{canon_type.value}"
        if target:
            desc += f" on '{target}'"
        if text:
            desc += f" text='{text}'"
        if app_name:
            desc += f" app='{app_name}'"
        if hotkeys:
            desc += f" keys={hotkeys}"

        return CanonicalAction(
            action_type=canon_type,
            target=target,
            text=text,
            keys=hotkeys,
            app_name=app_name,
            button=btn,
            clear_first=clear_first,
            press_enter=press_enter,
            duration_seconds=duration_seconds,
            description=desc,
            state=state,
        )

    @staticmethod
    def resolve_element(
        scene: SceneIndex,
        target_hint: Optional[str],
        action_type: CanonicalActionType = CanonicalActionType.CLICK,
    ) -> Tuple[Optional[SceneElement], Optional[str]]:
        """Resolve a label to one control: real controls first, then row labels, then text.

        * "Show more settings (Taskbar behaviors)" - a repeated label with its row context.
        * Duplicates (the Save dialog's "File name:" combo box AND the edit inside it) are
          settled by what the action needs instead of failing as ambiguous.
        * A row's text label ("Taskbar behaviors", "File name:") stands for the control on
          that row, as a person reads a form.
        """
        if not target_hint or not str(target_hint).strip() or (
                action_type == CanonicalActionType.TYPE and _norm_label(target_hint) in _GENERIC_EDITOR_HINTS):
            # Untargeted typing goes where the keyboard focus is (a rename box that just
            # opened), not to whichever text field is biggest (the address bar).
            if action_type == CanonicalActionType.TYPE:
                focused = [e for e in scene.elements.values() if getattr(e, "has_focus", False)
                           and e.role in ("Edit", "Document", "ComboBox")]
                if len(focused) == 1:
                    return focused[0], None
            if not target_hint or not str(target_hint).strip():
                return AliasMapper._resolve_core(scene, target_hint, action_type)
        # The screen description lists controls as "TabItem: Details"; planners copy that.
        m = _ROLE_PREFIX.match(str(target_hint).strip())
        if m and m.group(1).lower() in _ROLES:
            role, rest = _ROLES[m.group(1).lower()], m.group(2).strip()
            same_role = {i: e for i, e in scene.elements.items() if e.role == role}
            if same_role:
                el, _ = AliasMapper.resolve_element(scene.model_copy(update={"elements": same_role}), rest, action_type)
                if el is not None:
                    return el, None
            return AliasMapper.resolve_element(scene, rest, action_type)
        q = _norm_label(target_hint)
        controls = [e for e in scene.elements.values() if getattr(e, "interactive", True)]
        texts = [e for e in scene.elements.values() if not getattr(e, "interactive", True)]

        by_display =[e for e in controls if _norm_label(getattr(e, "display_label", e.label)) == q]
        if by_display:
            pick = _prefer(by_display, action_type)
            if pick is not None:
                return pick, None
        exact = [e for e in controls if _norm_label(e.label) == q]
        if len(exact) > 1:
            pick = _prefer(exact, action_type)
            if pick is not None:
                return pick, None
            # Genuinely different controls with one name: say so (with their row context)
            # instead of silently taking the first.
            names = [getattr(e, "display_label", e.label) for e in exact[:5]]
            return None, (f"Ambiguous target '{target_hint}': {len(exact)} controls have this label "
                          f"({'; '.join(names)}). Use the label with its context in brackets.")

        view = scene.model_copy(update={"elements": {e.element_id: e for e in controls}})
        el, diag = AliasMapper._resolve_core(view, target_hint, action_type)
        if el is not None:
            return el, None

        by_context = [e for e in controls if getattr(e, "context", None) and _norm_label(e.context) == q]
        if by_context:
            pick = _prefer(by_context, action_type)
            if pick is not None:
                return pick, None
        partner = _row_partner(texts, controls, q, action_type)
        if partner is not None:
            return partner, None
        if texts:
            el2, _ = AliasMapper._resolve_core(scene, target_hint, action_type)
            if el2 is not None:
                return el2, None
        # Planners describe where a control is: "System in Settings left navigation",
        # "Save on the toolbar". Try the label without the location.
        bare = re.sub(r"\s+(?:in|on|from|under|at|inside|within)\s+(?:the\s+)?\S.*$", "", str(target_hint).strip(),
                      flags=re.I)
        if bare and bare != str(target_hint).strip() and len(bare) >= 2:
            el3, _ = AliasMapper.resolve_element(scene, bare, action_type)
            if el3 is not None:
                return el3, None
        return None, diag

    @staticmethod
    def _resolve_core(
        scene: SceneIndex,
        target_hint: Optional[str],
        action_type: CanonicalActionType = CanonicalActionType.CLICK,
    ) -> Tuple[Optional[SceneElement], Optional[str]]:
        """Resolve a fuzzy target hint to a single SceneElement or error message.
        
        Returns:
            (matched_element, None) on success.
            (None, error_diagnostic) on failure.
        """
        if not target_hint or not target_hint.strip():
            # If typing or launching without a target, check for unique editor
            if action_type == CanonicalActionType.TYPE:
                edits = [el for el in scene.elements.values() if el.role in ("Document", "Edit")]
                if len(edits) == 1:
                    return edits[0], None
            return None, "Target hint is empty."

        hint = target_hint.strip()
        q = hint.lower()

        # 1. Exact Match (case-insensitive)
        for el in scene.elements.values():
            if el.label.strip().lower() == q:
                return el, None

        # 2. Normalized Label Match (strip ellipsis, colon, quotes, trailing dots)
        clean_q = re.sub(r"[\.\:\'\"\…]+$", "", q).strip()
        clean_matches = [
            el for el in scene.elements.values()
            if re.sub(r"[\.\:\'\"\…]+$", "", el.label.strip().lower()).strip() == clean_q
        ]
        if len(clean_matches) == 1:
            return clean_matches[0], None

        # 3. Unique Substring Match
        partial_matches = [el for el in scene.elements.values() if q in el.label.lower()]
        if len(partial_matches) == 1:
            return partial_matches[0], None

        # 4. Word-boundary substring match (e.g. "Save" in "Save as")
        word_matches = [
            el for el in scene.elements.values()
            if re.search(r"\b" + re.escape(q) + r"\b", el.label.lower())
        ]
        if len(word_matches) == 1:
            return word_matches[0], None

        # 4b. Spoken-form synonyms: Calculator labels its keys "Seven", "Plus", "Equals".
        for alt in _label_synonyms(clean_q):
            alt_matches = [el for el in scene.elements.values()
                           if re.sub(r"[\.\:\'\"\…]+$", "", el.label.strip().lower()).strip() == alt]
            if len(alt_matches) == 1:
                return alt_matches[0], None

        # 5. Role-based alias fallback
        if q in ROLE_FALLBACK_ALIASES:
            roles = ROLE_FALLBACK_ALIASES[q]
            role_candidates = [el for el in scene.elements.values() if el.role in roles]
            if len(role_candidates) == 1:
                return role_candidates[0], None
            elif len(role_candidates) > 1:
                # Disambiguate by focus or largest area
                focusable = [el for el in role_candidates if el.is_keyboard_focusable]
                if len(focusable) == 1:
                    return focusable[0], None
                pool = focusable if focusable else role_candidates
                def _area(elem: SceneElement) -> int:
                    w = max(0, elem.bbox_screen[2] - elem.bbox_screen[0])
                    h = max(0, elem.bbox_screen[3] - elem.bbox_screen[1])
                    return w * h
                best = max(pool, key=_area)
                if _area(best) > 0:
                    return best, None

        # 5a. Typing "into Notepad" names the WINDOW, not a control: use its editor.
        if action_type == CanonicalActionType.TYPE and len(q) >= 3:
            title = (scene.window_title or "").lower()
            if q in title or q.replace(" ", "") in title.replace(" ", ""):
                el, _ = AliasMapper.resolve_element(scene, "editor", action_type=action_type)
                if el is not None:
                    return el, None

        # 5b. "the Save button" / "wifi icon": retry without the control-type word.
        stripped = re.sub(r"\s+(?:button|icon|link|tab|menu|field|box|checkbox)$", "", q).strip()
        if stripped and stripped != q:
            el, _ = AliasMapper.resolve_element(scene, stripped, action_type=action_type)
            if el is not None:
                return el, None

        # 6. Ambiguity vs Not Found Diagnostics
        if len(partial_matches) > 1:
            candidate_labels = [el.label for el in partial_matches[:4]]
            return None, (
                f"Ambiguous target '{hint}': matches multiple elements ({', '.join(candidate_labels)}). "
                f"Please provide a more specific label."
            )

        avail_labels = [el.label for el in list(scene.elements.values())[:10] if el.label]
        return None, (
            f"No UI element found matching target '{hint}'. "
            f"Available elements sample: {', '.join(avail_labels) if avail_labels else 'None'}."
        )
