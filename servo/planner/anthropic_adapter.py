"""Anthropic Claude System 2 Planner adapter."""

from __future__ import annotations

import json
import httpx
from typing import List, Optional
from servo.config import settings
from servo.perception.scene_index import SceneIndex
from servo.planner.base import SubGoal, EscalationContext


class AnthropicPlanner:
    """System 2 planner connecting to Anthropic Claude API."""

    def __init__(self, api_key: Optional[str] = None, model: str = "claude-3-5-sonnet-20241022") -> None:
        self.api_key = api_key or settings.anthropic_api_key
        self.model = model

    def decompose(self, goal: str, initial_scene: SceneIndex) -> List[SubGoal]:
        labels = [el.label for el in initial_scene.elements.values() if el.label]
        prompt = (
            f"Decompose goal into atomic UI sub-goals. Return ONLY a JSON list of objects.\n"
            f"Goal: {goal}\nElements: {labels[:25]}"
        )
        try:
            if not self.api_key:
                raise ValueError("Missing Anthropic API Key")
            headers = {
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            }
            payload = {
                "model": self.model,
                "max_tokens": 1024,
                "messages": [{"role": "user", "content": prompt}],
            }
            with httpx.Client(timeout=10.0) as client:
                res = client.post("https://api.anthropic.com/v1/messages", headers=headers, json=payload)
                if res.status_code == 200:
                    text = res.json()["content"][0]["text"].strip()
                    if text.startswith("```"):
                        text = text.split("\n", 1)[1].rsplit("\n", 1)[0]
                    items = json.loads(text)
                    return [SubGoal(**item) for item in items]
        except Exception:
            pass

        return [
            SubGoal(
                step_index=1,
                instruction=goal,
                target_element_hint=labels[0] if labels else None,
                action_type="click",
                expected_outcome="Executed step",
            )
        ]

    def recover(self, context: EscalationContext) -> List[SubGoal]:
        return [
            SubGoal(
                step_index=context.failed_subgoal.step_index + 1,
                instruction=f"Alternative approach for {context.failed_subgoal.instruction}",
                target_element_hint=context.failed_subgoal.target_element_hint,
                action_type="click",
            )
        ]
