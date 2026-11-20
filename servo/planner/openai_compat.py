"""OpenAI-compatible and local Ollama System 2 Planner adapter.

See ADR-10 in SERVO_ARCHITECTURE_V1.md.
"""

from __future__ import annotations

import json
import httpx
from typing import List, Optional
from servo.config import settings
from servo.perception.scene_index import SceneIndex
from servo.planner.base import SubGoal, EscalationContext


class OpenAICompatPlanner:
    """System 2 planner connecting to OpenAI or local Ollama endpoints."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
    ) -> None:
        self.base_url = (base_url or settings.openai_base_url).rstrip("/")
        self.api_key = api_key or settings.openai_api_key
        self.model = model or settings.openai_model

    def decompose(self, goal: str, initial_scene: SceneIndex) -> List[SubGoal]:
        """Decompose goal into sub-goals using LLM, with deterministic heuristic fallback."""
        labels = [el.label for el in initial_scene.elements.values() if el.label]
        prompt = (
            f"You are Servo System 2 Planner. Decompose this goal into 1-4 atomic UI sub-goals.\n"
            f"Goal: {goal}\n"
            f"Available UI Elements: {labels[:25]}\n"
            f"Respond ONLY with a valid JSON array of objects with keys: "
            f"'step_index', 'instruction', 'target_element_hint', 'action_type', 'text_to_type', 'expected_outcome'."
        )

        try:
            headers = {"Content-Type": "application/json"}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"

            payload = {
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.1,
            }
            with httpx.Client(timeout=10.0) as client:
                res = client.post(f"{self.base_url}/chat/completions", headers=headers, json=payload)
                if res.status_code == 200:
                    data = res.json()
                    content = data["choices"][0]["message"]["content"].strip()
                    # Strip markdown fence if present
                    if content.startswith("```"):
                        content = content.split("\n", 1)[1].rsplit("\n", 1)[0]
                    items = json.loads(content)
                    return [SubGoal(**item) for item in items]
        except Exception:
            pass

        # Heuristic fallback decomposition
        return [
            SubGoal(
                step_index=1,
                instruction=goal,
                target_element_hint=labels[0] if labels else None,
                action_type="click",
                expected_outcome="Target element activated",
            )
        ]

    def recover(self, context: EscalationContext) -> List[SubGoal]:
        """Generate alternative sub-goal after failure."""
        return [
            SubGoal(
                step_index=context.failed_subgoal.step_index + 1,
                instruction=f"Retry: {context.failed_subgoal.instruction}",
                target_element_hint=context.failed_subgoal.target_element_hint,
                action_type="click",
                expected_outcome="Recovered execution",
            )
        ]
