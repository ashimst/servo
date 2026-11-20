"""Anthropic Claude System 2 Planner adapter.

Uses the official ``anthropic`` Python SDK (install with ``pip install servo[anthropic]``).
"""

from __future__ import annotations

from typing import List, Optional

from servo.config import settings
from servo.perception.scene_index import SceneIndex
from servo.planner.base import (
    EscalationContext,
    PlannerError,
    SubGoal,
    build_planning_prompt,
    parse_subgoals,
)

DEFAULT_ANTHROPIC_MODEL = "claude-opus-5-5"


class AnthropicPlanner:
    """System 2 planner connecting to the Anthropic Messages API."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        timeout_seconds: float = 120.0,
    ) -> None:
        self.api_key = api_key or settings.anthropic_api_key or None
        self.model = model or settings.anthropic_model or DEFAULT_ANTHROPIC_MODEL
        self.timeout_seconds = timeout_seconds

    def decompose(self, goal: str, initial_scene: SceneIndex) -> List[SubGoal]:
        try:
            import anthropic
        except ImportError as e:
            raise PlannerError("The 'anthropic' package is not installed: pip install servo[anthropic]") from e

        # With api_key=None the SDK resolves credentials from the environment
        # (ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN, or an `ant auth login` profile).
        client = anthropic.Anthropic(api_key=self.api_key, timeout=self.timeout_seconds)
        try:
            response = client.beta.messages.create(
                model=self.model,
                max_tokens=16000,
                output_config={"effort": "low"},  # short structured planning output
                # Server-side refusal fallback: if the model declines, the API re-runs
                # the request on a fallback model inside the same call.
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                messages=[{"role": "user", "content": build_planning_prompt(goal, initial_scene)}],
            )
        except anthropic.APIConnectionError as e:
            raise PlannerError(f"Could not reach the Anthropic API: {e}") from e
        except anthropic.AuthenticationError as e:
            raise PlannerError("Anthropic API authentication failed; check ANTHROPIC_API_KEY.") from e
        except anthropic.RateLimitError as e:
            raise PlannerError("Anthropic API rate limit reached.") from e
        except anthropic.APIStatusError as e:
            raise PlannerError(f"Anthropic API error {e.status_code}: {e.message}") from e

        if response.stop_reason == "refusal":
            raise PlannerError("The model declined to plan this goal.")
        if response.stop_reason == "max_tokens":
            raise PlannerError("Planner output was truncated (max_tokens).")
        text = "".join(block.text for block in response.content if block.type == "text")
        return parse_subgoals(text)

    def recover(self, context: EscalationContext) -> List[SubGoal]:
        """No automatic recovery; see OpenAICompatPlanner.recover."""
        return []
