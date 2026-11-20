"""OpenAI-compatible and local Ollama System 2 Planner adapter.

See ADR-10 in SERVO_ARCHITECTURE_V1.md.
"""

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


class OpenAICompatPlanner:
    """System 2 planner connecting to OpenAI or local Ollama endpoints."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        timeout_seconds: float = 60.0,
    ) -> None:
        self.base_url = (base_url or settings.openai_base_url).rstrip("/")
        self.api_key = api_key or settings.openai_api_key
        self.model = model or settings.openai_model
        self.timeout_seconds = timeout_seconds

    def decompose(self, goal: str, initial_scene: SceneIndex) -> List[SubGoal]:
        """Decompose goal into sub-goals using the LLM. Raises PlannerError on failure."""
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": build_planning_prompt(goal, initial_scene)}],
            "temperature": 0.1,
        }
        try:
            with httpx.Client(timeout=self.timeout_seconds) as client:
                res = client.post(f"{self.base_url}/chat/completions", headers=headers, json=payload)
            res.raise_for_status()
            content = res.json()["choices"][0]["message"]["content"]
        except httpx.HTTPError as e:
            raise PlannerError(f"OpenAI-compatible planner request failed: {e}") from e
        except (KeyError, IndexError, TypeError, ValueError) as e:
            raise PlannerError(f"Unexpected OpenAI-compatible response shape: {e}") from e
        return parse_subgoals(content or "")

    def recover(self, context: EscalationContext) -> List[SubGoal]:
        """No automatic recovery. The old stub re-issued the identical failed action
        ("Retry: ..."), i.e. a second click on something that already failed
        verification. Returning [] surfaces the failure to the caller instead."""
        return []
