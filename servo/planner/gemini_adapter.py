"""Google Gemini System 2 Planner adapter."""

from __future__ import annotations

import logging
from typing import List, Optional

import httpx

from servo.config import settings
from servo.perception.scene_index import SceneIndex
from servo.planner.base import (
    EscalationContext,
    PlannerError,
    SubGoal,
)
from servo.planner.tool_calling import (
    MacroPlan,
    SYSTEM_PROMPT_TEMPLATE,
    build_tool_planning_prompt,
    parse_deterministic_intent,
    parse_llm_tool_plan,
)

logger = logging.getLogger("servo.planner.gemini")


class GeminiPlanner:
    """System 2 planner connecting to Google Gemini API with structured tool calling."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        timeout_seconds: float = 60.0,
    ) -> None:
        self.api_key = api_key or settings.gemini_api_key
        self.model = model or getattr(settings, "gemini_model", "gemini-2.0-flash")
        self.timeout_seconds = timeout_seconds

    def decompose(self, goal: str, initial_scene: SceneIndex) -> List[SubGoal]:
        """Decompose a high-level goal into an ordered sequence of SubGoals. Raises PlannerError."""
        macro_plan = self.decompose_macro(goal, initial_scene)
        subgoals = macro_plan.to_subgoals()
        if not subgoals:
            raise PlannerError(f"Gemini planner could not decompose the goal into actionable steps: '{goal}'")
        return subgoals

    def decompose_macro(self, goal: str, initial_scene: Optional[SceneIndex] = None) -> MacroPlan:
        """Decompose a high-level goal into a structured MacroPlan."""
        if not self.api_key:
            fallback = parse_deterministic_intent(goal)
            if fallback and fallback.steps:
                return fallback
            raise PlannerError("SERVO_GEMINI_API_KEY is not set.")

        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
        headers = {"x-goog-api-key": self.api_key, "Content-Type": "application/json"}
        prompt = build_tool_planning_prompt(goal, initial_scene)
        payload = {
            "contents": [
                {
                    "parts": [
                        {"text": f"{SYSTEM_PROMPT_TEMPLATE}\n\n{prompt}"}
                    ]
                }
            ],
            "generationConfig": {
                "response_mime_type": "application/json",
                "temperature": 0.1,
            },
        }

        try:
            with httpx.Client(timeout=self.timeout_seconds) as client:
                res = client.post(url, headers=headers, json=payload)
            res.raise_for_status()
            text = res.json()["candidates"][0]["content"]["parts"][0]["text"]
        except httpx.HTTPError as e:
            logger.warning("Gemini API error (%s: %s). Attempting deterministic fallback.", e.__class__.__name__, e)
            fallback = parse_deterministic_intent(goal)
            if fallback and fallback.steps:
                return fallback
            raise PlannerError(f"Gemini planner request failed: {e.__class__.__name__}: {e}") from e
        except (KeyError, IndexError, TypeError, ValueError) as e:
            logger.warning("Gemini unexpected response shape: %s. Attempting deterministic fallback.", e)
            fallback = parse_deterministic_intent(goal)
            if fallback and fallback.steps:
                return fallback
            raise PlannerError(f"Unexpected Gemini response shape: {e}") from e

        macro_plan = parse_llm_tool_plan(text or "", goal=goal)
        if not macro_plan.steps:
            fallback = parse_deterministic_intent(goal)
            if fallback and fallback.steps:
                return fallback

        return macro_plan

    def recover(self, context: EscalationContext) -> List[SubGoal]:
        """No automatic recovery."""
        return []
