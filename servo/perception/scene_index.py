"""Unified integer Scene Indexing and Scene Delta differential engine.

See ADR-02 in SERVO_ARCHITECTURE_V1.md.
"""

from __future__ import annotations

import time
from collections import Counter
from typing import Dict, List, Literal, Optional, Tuple
from pydantic import BaseModel, Field


class SceneElement(BaseModel):
    """Normalized interactive element within a target window."""
    element_id: int
    label: str
    role: str
    bbox_screen: Tuple[int, int, int, int]
    bbox_norm: Tuple[int, int, int, int]
    center_screen: Tuple[int, int]
    is_keyboard_focusable: bool = False
    value: Optional[str] = None


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

    def get_by_id(self, element_id: int) -> Optional[SceneElement]:
        return self.elements.get(element_id)

    def find_by_label(self, query: str, exact: bool = False) -> List[SceneElement]:
        """Search elements by label."""
        q = query.strip()
        if not exact:
            q = q.lower()
        matches = []
        for el in self.elements.values():
            label = el.label.strip() if exact else el.label.strip().lower()
            if exact and q == label:
                matches.append(el)
            elif not exact and q in label:
                matches.append(el)
        return matches


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
