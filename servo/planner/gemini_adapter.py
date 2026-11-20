"""Google Gemini System 2 Planner adapter."""

from __future__ import annotations

from typing import List, Optional

import httpx

from servo.config import settings
from servo.perception.scene_index import SceneIndex
from servo.planner.base import (
    EscalationContext,
    PlannerError,
    SubGoal,
    build_planning_prompt,
    parse_subgoals,
)


class GeminiPlanner:
    """System 2 planner connecting to Google Gemini API."""

    def __init__(self, api_key: Optional[str] = None, model: str = "gemini-2.0-flash", timeout_seconds: float = 60.0) -> None:
        self.api_key = api_key or settings.gemini_api_key
        self.model = model
        self.timeout_seconds = timeout_seconds

    def decompose(self, goal: str, initial_scene: SceneIndex) -> List[SubGoal]:
        if not self.api_key:
            raise PlannerError("SERVO_GEMINI_API_KEY is not set.")
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
        # Key goes in a header, not the query string, so it doesn't end up in
        # httpx/proxy request logs or exception messages that include the URL.
        headers = {"x-goog-api-key": self.api_key, "Content-Type": "application/json"}
        payload = {
            "contents": [{"parts": [{"text": build_planning_prompt(goal, initial_scene)}]}],
            "generationConfig": {"response_mime_type": "application/json"},
        }
        try:
            with httpx.Client(timeout=self.timeout_seconds) as client:
                res = client.post(url, headers=headers, json=payload)
            res.raise_for_status()
            text = res.json()["candidates"][0]["content"]["parts"][0]["text"]
        except httpx.HTTPError as e:
            raise PlannerError(f"Gemini planner request failed: {e.__class__.__name__}") from e
        except (KeyError, IndexError, TypeError, ValueError) as e:
            raise PlannerError(f"Unexpected Gemini response shape: {e}") from e
        return parse_subgoals(text or "")

    def recover(self, context: EscalationContext) -> List[SubGoal]:
        """No automatic recovery; see OpenAICompatPlanner.recover."""
        return []
