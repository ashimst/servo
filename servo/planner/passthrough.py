"""MCP Passthrough System 2 Planner.

Used when an external coding agent directly acts as System 2 over Model Context Protocol (MCP).
See Section 4 in SERVO_ARCHITECTURE_V1.md.
"""

from __future__ import annotations

from typing import List
from servo.perception.scene_index import SceneIndex
from servo.planner.base import SubGoal, EscalationContext


class PassthroughPlanner:
    """Passthrough adapter: passes the caller's atomic command directly to the reflex arc."""

    def decompose(self, goal: str, initial_scene: SceneIndex) -> List[SubGoal]:
        """Wrap the requested goal directly as a single-step sub-goal."""
        labels = [el.label for el in initial_scene.elements.values() if el.label]
        # Match goal to closest label if mentioned in goal
        target_hint = None
        for label in labels:
            if label.lower() in goal.lower():
                target_hint = label
                break

        action_type = "type" if "type" in goal.lower() or "enter" in goal.lower() else "click"

        return [
            SubGoal(
                step_index=1,
                instruction=goal,
                target_element_hint=target_hint or (labels[0] if labels else None),
                action_type=action_type,
                expected_outcome="MCP requested action completed",
            )
        ]

    def recover(self, context: EscalationContext) -> List[SubGoal]:
        """Return empty list to immediately hand control back to the driving MCP agent."""
        return []
