"""Action and Element Alias Mapping Engine for Servo.

Provides canonical translation of fuzzy natural language intents, LLM tool calls,
and user phrasings into concrete, deterministic invocations on NeedleToolSurface
and Desktop Control subsystems.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple
from pydantic import BaseModel, Field

from servo.perception.scene_index import SceneElement, SceneIndex


class CanonicalActionType(str, Enum):
    CLICK = "click"
    DOUBLE_CLICK = "double_click"
    RIGHT_CLICK = "right_click"
    TYPE = "type"
    HOTKEY = "hotkey"
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
        keys: Optional[List[str]] = None,
        app_name: Optional[str] = None,
        button: str = "left",
        clear_first: bool = False,
        press_enter: bool = False,
        duration_seconds: float = 0.0,
        **extra_kwargs: Any,
    ) -> CanonicalAction:
        """Translate a raw action name and kwargs into a CanonicalAction."""
        norm_name = raw_action.strip().lower().replace("-", "_").replace(" ", "_")
        canon_type = ACTION_ALIAS_MAP.get(norm_name)

        if canon_type is None:
            # Check prefixes
            if norm_name.startswith("click"):
                canon_type = CanonicalActionType.CLICK
            elif norm_name.startswith("type") or norm_name.startswith("write"):
                canon_type = CanonicalActionType.TYPE
            elif norm_name.startswith("open") or norm_name.startswith("launch"):
                canon_type = CanonicalActionType.LAUNCH
            elif norm_name.startswith("close"):
                canon_type = CanonicalActionType.CLOSE
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

        # Hotkey normalizations
        hotkeys = keys or []
        if isinstance(hotkeys, str):
            hotkeys = [k.strip() for k in re.split(r"[\+\s,]+", hotkeys) if k.strip()]

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
