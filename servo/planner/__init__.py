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

    name = provider or settings.planner_provider
    if name == "openai":
        return OpenAICompatPlanner()
    if name == "anthropic":
        return AnthropicPlanner()
    if name == "gemini":
        return GeminiPlanner()
    if name == "passthrough":
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
