"""Planner package: System 2 setpoint planning backends and protocols."""

from typing import Optional

from servo.planner.base import (
    SubGoal,
    Plan,
    EscalationContext,
    PlannerBackend,
    PlannerError,
    parse_subgoals,
)
from servo.planner.openai_compat import OpenAICompatPlanner
from servo.planner.anthropic_adapter import AnthropicPlanner
from servo.planner.gemini_adapter import GeminiPlanner
from servo.planner.passthrough import PassthroughPlanner


def get_planner(provider: Optional[str] = None) -> PlannerBackend:
    """Instantiate the planner selected by ``provider`` or SERVO_PLANNER_PROVIDER."""
    from servo.config import settings

    name = (provider or settings.planner_provider or "").lower().strip()
    if name in ("openai", "openai_compat", "ollama"):
        return OpenAICompatPlanner()
    if name in ("anthropic", "claude"):
        return AnthropicPlanner()
    if name in ("gemini", "google"):
        return GeminiPlanner()
    if name in ("passthrough", "mcp"):
        return PassthroughPlanner()
    raise PlannerError(f"Unknown planner provider '{name}'.")


__all__ = [
    "SubGoal",
    "Plan",
    "EscalationContext",
    "PlannerBackend",
    "PlannerError",
    "parse_subgoals",
    "get_planner",
    "OpenAICompatPlanner",
    "AnthropicPlanner",
    "GeminiPlanner",
    "PassthroughPlanner",
]
