"""Google Gemini System 2 Planner adapter."""

from __future__ import annotations

import json
import httpx
from typing import List, Optional
from servo.config import settings
from servo.perception.scene_index import SceneIndex
from servo.planner.base import SubGoal, EscalationContext


class GeminiPlanner:
    """System 2 planner connecting to Google Gemini API."""

    def __init__(self, api_key: Optional[str] = None, model: str = "gemini-2.0-flash") -> None:
        self.api_key = api_key or settings.gemini_api_key
        self.model = model

    def decompose(self, goal: str, initial_scene: SceneIndex) -> List[SubGoal]:
        labels = [el.label for el in initial_scene.elements.values() if el.label]
        prompt = (
            f"Decompose goal into JSON array of sub-goals with keys: "
            f"step_index, instruction, target_element_hint, action_type, text_to_type, expected_outcome.\n"
            f"Goal: {goal}\nElements: {labels[:25]}"
        )
        try:
            if not self.api_key:
                raise ValueError("Missing Gemini API Key")
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent?key={self.api_key}"
            payload = {
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"response_mime_type": "application/json"},
            }
            with httpx.Client(timeout=10.0) as client:
                res = client.post(url, json=payload)
                if res.status_code == 200:
                    raw_json = res.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
                    items = json.loads(raw_json)
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
