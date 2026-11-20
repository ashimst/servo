"""Action and Element Alias Mapping Engine for Servo.

Provides canonical translation of fuzzy natural language intents, LLM tool calls,
and user phrasings into concrete, deterministic invocations on NeedleToolSurface
and Desktop Control subsystems.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple
from pydantic import BaseModel, Field

from servo.perception.scene_index import SceneElement, SceneIndex
from servo.actuation.sandboxing import normalize_keys
from servo.actuation.needle_tools import NeedleToolSurface


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


# Comprehensive dictionary of natural language & tool aliases mapping to CanonicalActionType
ACTION_ALIAS_MAP: Dict[str, CanonicalActionType] = {
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
                hotkeys = [k.strip() for k in re.split(r"[\+\s,\-]+", keys) if k.strip()]
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

        # Parameter validation
        if canon_type == CanonicalActionType.TYPE:
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
        )

    @staticmethod
    def map_to_needle_tool(
        action: CanonicalAction,
        tools: NeedleToolSurface,
        scene: SceneIndex,
        element_id: Optional[int] = None,
    ) -> Tuple[Callable[[], str], str]:
        """Deterministically resolve a CanonicalAction to an executable callable on NeedleToolSurface and description."""
        action_type = action.action_type
        if action_type in (CanonicalActionType.CLICK, CanonicalActionType.DOUBLE_CLICK, CanonicalActionType.RIGHT_CLICK):
            if element_id is None:
                raise ValueError(f"Action '{action_type.value}' requires an element_id.")
            button = action.button
            if action_type == CanonicalActionType.DOUBLE_CLICK:
                button = "double"
            elif action_type == CanonicalActionType.RIGHT_CLICK:
                button = "right"
            call = lambda eid=element_id, b=button: tools.click(scene, eid, button=b)
            sig = f"click({element_id}, button='{button}')"
            return call, sig

        elif action_type == CanonicalActionType.INVOKE:
            if element_id is None:
                raise ValueError("Action 'invoke' requires an element_id.")
            call = lambda eid=element_id: tools.invoke(scene, eid)
            sig = f"invoke({element_id})"
            return call, sig

        elif action_type == CanonicalActionType.TYPE:
            if element_id is None:
                raise ValueError("Action 'type' requires an element_id.")
            call = lambda eid=element_id, t=action.text, pe=action.press_enter, cf=action.clear_first: tools.type_text(
                scene, eid, text=t, press_enter=pe, clear_first=cf
            )
            sig = f"type_text({element_id}, text={action.text!r})"
            return call, sig

        elif action_type == CanonicalActionType.HOTKEY:
            call = lambda k=list(action.keys): tools.hotkey(k)
            sig = f"hotkey({action.keys})"
            return call, sig

        elif action_type == CanonicalActionType.SCROLL:
            if element_id is None:
                raise ValueError("Action 'scroll' requires an element_id.")
            direction = "down"
            amount = 3
            call = lambda eid=element_id, d=direction, a=amount: tools.scroll(scene, eid, direction=d, amount=a)
            sig = f"scroll({element_id}, direction='{direction}', amount={amount})"
            return call, sig

        elif action_type == CanonicalActionType.WAIT:
            call = lambda d=action.duration_seconds: tools.wait(d)
            sig = f"wait({action.duration_seconds})"
            return call, sig

        else:
            raise ValueError(
                f"Action type '{action_type.value}' cannot be dispatched directly to NeedleToolSurface."
            )

    @staticmethod
    def resolve_element(
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
