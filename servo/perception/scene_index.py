"""Unified integer Scene Indexing and Scene Delta differential engine.

"""

from __future__ import annotations

import time
from collections import Counter
from typing import Any, Dict, List, Literal, Optional, Tuple
from pydantic import BaseModel, Field, PrivateAttr


class SceneElement(BaseModel):
    """Normalized interactive element within a target window."""
    element_id: int
    label: str
    role: str
    bbox_screen: Tuple[int, int, int, int]
    bbox_norm: Tuple[int, int, int, int]
    center_screen: Tuple[int, int]
    is_keyboard_focusable: bool = False
    has_focus: bool = False          # the control that typed keys go to right now
    value: Optional[str] = None
    # Control state, so the planner can see whether a switch is already on, which item is
    # selected, whether a section is open. None = the control doesn't have that state.
    toggle: Optional[str] = None        # "on" | "off" | "mixed"
    selected: Optional[bool] = None
    expanded: Optional[bool] = None
    enabled: bool = True
    # False for plain text (headings, descriptions, status lines): readable, not a target.
    interactive: bool = True
    # The text label on the same row, for controls whose own label repeats or says little
    # (every Settings section header is a button called "Show more settings").
    context: Optional[str] = None

    @property
    def display_label(self) -> str:
        return f"{self.label} ({self.context})" if self.context else self.label

    def state_tags(self) -> List[str]:
        tags: List[str] = []
        if self.toggle:
            tags.append(self.toggle)
        if self.selected:
            tags.append("selected")
        if self.expanded is not None:
            tags.append("expanded" if self.expanded else "collapsed")
        if not self.enabled:
            tags.append("disabled")
        return tags


class SceneIndex(BaseModel):
    """Complete snapshot of all detected controls for a window."""
    hwnd: int
    window_title: str
    source: Literal["uia", "mirage", "hybrid"]
    timestamp: float = Field(default_factory=time.time)
    viewport: Dict[str, float | int] = Field(default_factory=dict)
    elements: Dict[int, SceneElement] = Field(default_factory=dict)
    # Set when sensing failed (COM error, window gone). An errored scene is NOT a
    # valid observation: it must never be diffed as though the UI changed.
    sense_error: Optional[str] = None
    # Live UI Automation handles for the elements (element_id -> pywinauto wrapper), so
    # pattern actions (toggle, select, expand, set value) act on the exact control.
    _live: Dict[int, Any] = PrivateAttr(default_factory=dict)

    def live(self, element_id: int) -> Any:
        return self._live.get(element_id)

    def get_by_id(self, element_id: int) -> Optional[SceneElement]:
        return self.elements.get(element_id)

def _keyed_values(scene: SceneIndex) -> Dict[str, Optional[str]]:
    """Map each valued element to a stable key: its label, plus '#n' for the nth element
    carrying the same label in document order."""
    seen: Counter = Counter()
    out: Dict[str, Optional[str]] = {}
    for el in sorted(scene.elements.values(), key=lambda e: e.element_id):
        seen[el.label] += 1
        n = seen[el.label]
        if el.value is None:
            continue
        key = el.label if n == 1 else f"{el.label}#{n}"
        out[key] = el.value
    return out


class SceneDelta(BaseModel):
    """Structured diff between two consecutive scene snapshots."""
    has_changed: bool = False
    window_closed: bool = False   # the window acted on is gone (a dialog's OK / Save / Cancel)
    title_changed: bool = False
    old_title: str = ""
    new_title: str = ""
    added_labels: List[str] = Field(default_factory=list)
    removed_labels: List[str] = Field(default_factory=list)
    value_changes: Dict[str, Dict[str, str]] = Field(default_factory=dict)

    @classmethod
    def compute_diff(cls, before: SceneIndex, after: SceneIndex) -> SceneDelta:
        """Compute semantic differential between before and after scenes.

        Labels are compared as multisets and values are keyed by (role, label, occurrence),
        so two controls sharing a label (e.g. two "OK" buttons, or several unnamed edits)
        no longer collapse into one entry and hide changes.
        """
        if not before.sense_error and after.sense_error == "window closed":
            # Not a sensing failure: the window went away, which is what Save, OK or Cancel
            # in a dialog is supposed to do. Reporting "no change" here made every such
            # click look failed.
            gone = sorted(el.label for el in before.elements.values() if el.label)
            return cls(has_changed=True, window_closed=True, title_changed=True, old_title=before.window_title,
                       new_title="", removed_labels=gone)
        if before.sense_error or after.sense_error:
            # One side is not a real observation; report "no observed change" so the
            # caller does not mistake a sensing failure for a successful transition.
            return cls(old_title=before.window_title, new_title=after.window_title)

        title_changed = before.window_title != after.window_title

        before_labels = Counter(el.label for el in before.elements.values() if el.label)
        after_labels = Counter(el.label for el in after.elements.values() if el.label)

        added = sorted((after_labels - before_labels).elements())
        removed = sorted((before_labels - after_labels).elements())

        value_changes: Dict[str, Dict[str, str]] = {}
        before_vals = _keyed_values(before)
        after_vals = _keyed_values(after)

        for label, new_val in after_vals.items():
            if label in before_vals and before_vals[label] != new_val:
                value_changes[label] = {
                    "old_value": before_vals[label] or "",
                    "new_value": new_val or "",
                }

        has_changed = (
            title_changed
            or bool(added)
            or bool(removed)
            or bool(value_changes)
        )

        return cls(
            has_changed=has_changed,
            title_changed=title_changed,
            old_title=before.window_title,
            new_title=after.window_title,
            added_labels=added,
            removed_labels=removed,
            value_changes=value_changes,
        )
