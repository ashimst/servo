"""MCP Passthrough System 2 Planner.

Used when an external coding agent directly acts as System 2 over Model Context Protocol (MCP).
See Section 4 in SERVO_ARCHITECTURE_V1.md.

The caller is expected to send one precise command per call, e.g.
``click "Save"`` or ``type "hello world" into "Search"``.
"""

from __future__ import annotations

import re
from typing import List, Optional

from servo.perception.scene_index import SceneIndex
from servo.planner.base import EscalationContext, PlannerError, SubGoal

_QUOTED = r"""["'“”](.+?)["'“”]"""
_TYPE_RE = re.compile(rf"^\s*(?:type|enter)\s+{_QUOTED}\s+(?:into|in)\s+(?:the\s+)?(.+?)\s*$", re.IGNORECASE)
_CLICK_RE = re.compile(r"^\s*(?:click|press|select|open)\s+(?:on\s+)?(?:the\s+)?(.+?)\s*$", re.IGNORECASE)


def _strip_quotes(s: str) -> str:
    return s.strip().strip("\"'“”").strip()


def _match_label(text: str, scene: SceneIndex) -> Optional[str]:
    """Exact label match first, otherwise the LONGEST label mentioned in the text (so
    'Save As' wins over 'Save'). No match -> None; never an arbitrary label."""
    labels = [el.label for el in scene.elements.values() if el.label]
    t = text.strip().lower()
    for label in labels:
        if label.strip().lower() == t:
            return label
    mentioned = [label for label in labels if label.strip().lower() in t]
    return max(mentioned, key=len) if mentioned else None


class PassthroughPlanner:
    """Passthrough adapter: parses the caller's single atomic command."""

    def decompose(self, goal: str, initial_scene: SceneIndex) -> List[SubGoal]:
        m = _TYPE_RE.match(goal)
        if m:
            text, target = m.group(1), _strip_quotes(m.group(2))
            hint = _match_label(target, initial_scene) or target
            return [SubGoal(
                step_index=1, instruction=goal, target_element_hint=hint,
                action_type="type", text_to_type=text,
                expected_outcome=f"'{text}' entered into {hint}",
            )]

        m = _CLICK_RE.match(goal)
        target_text = _strip_quotes(m.group(1)) if m else goal
        hint = _match_label(target_text, initial_scene)
        if hint is None:
            raise PlannerError(
                f"Passthrough planner could not match '{goal}' to an element on screen. "
                "Use the exact label from get_scene_index, e.g. click \"Save\" or "
                "type \"text\" into \"Search\"."
            )
        return [SubGoal(
            step_index=1, instruction=goal, target_element_hint=hint,
            action_type="click", expected_outcome="",
        )]

    def recover(self, context: EscalationContext) -> List[SubGoal]:
        """Return empty list to immediately hand control back to the driving MCP agent."""
        return []
