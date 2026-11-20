"""OpenAI-compatible and local Ollama System 2 Planner adapter.

See ADR-10 in SERVO_ARCHITECTURE_V1.md and SERVO_ARCHITECTURE_V3.md.
"""

from __future__ import annotations

import json
import logging
from typing import List, Optional

import httpx

from servo.actuation.alias_mapping import AliasMapper
from servo.config import settings
from servo.perception.scene_index import SceneIndex
from servo.planner.base import (
    EscalationContext,
    PlannerError,
    SubGoal,
)
from servo.planner.tool_calling import (
    MacroPlan,
    PlanStep,
    SYSTEM_PROMPT_TEMPLATE,
    build_tool_planning_prompt,
    parse_deterministic_intent,
    parse_llm_tool_plan,
)

logger = logging.getLogger("servo.planner.openai_compat")


class OpenAICompatPlanner:
    """System 2 planner connecting to OpenAI or local Ollama endpoints with structured tool calling."""

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
        macro_plan = self.decompose_macro(goal, initial_scene)
        subgoals = macro_plan.to_subgoals()
        if not subgoals:
            raise PlannerError(f"OpenAI-compatible planner could not decompose the goal into actionable steps: '{goal}'")
        return subgoals

    def decompose_macro(self, goal: str, initial_scene: Optional[SceneIndex] = None) -> MacroPlan:
        """Decompose a high-level goal into a structured MacroPlan."""
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        prompt = build_tool_planning_prompt(goal, initial_scene)
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT_TEMPLATE},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.1,
        }

        try:
            with httpx.Client(timeout=self.timeout_seconds) as client:
                res = client.post(f"{self.base_url}/chat/completions", headers=headers, json=payload)
            res.raise_for_status()
            choice = res.json()["choices"][0]
            message = choice.get("message", {})
            content = message.get("content") or ""
            tool_calls = message.get("tool_calls")
        except httpx.HTTPError as e:
            logger.warning("OpenAI-compat API error (%s: %s). Attempting deterministic fallback.", e.__class__.__name__, e)
            fallback = parse_deterministic_intent(goal)
            if fallback and fallback.steps:
                return fallback
            raise PlannerError(f"OpenAI-compatible planner request failed: {e}") from e
        except (KeyError, IndexError, TypeError, ValueError) as e:
            logger.warning("OpenAI-compat unexpected response shape: %s. Attempting deterministic fallback.", e)
            fallback = parse_deterministic_intent(goal)
            if fallback and fallback.steps:
                return fallback
            raise PlannerError(f"Unexpected OpenAI-compatible response shape: {e}") from e

        if tool_calls and isinstance(tool_calls, list):
            steps: List[PlanStep] = []
            for idx, tc in enumerate(tool_calls, start=1):
                fn = tc.get("function", {})
                tool_name = fn.get("name", "click_element")
                args_raw = fn.get("arguments", {})
                if isinstance(args_raw, str):
                    try:
                        args = json.loads(args_raw)
                    except Exception:
                        args = {}
                elif isinstance(args_raw, dict):
                    args = args_raw
                else:
                    args = {}
                canon = None
                try:
                    canon = AliasMapper.canonicalize_action(raw_action=tool_name, **args)
                except Exception:
                    pass
                steps.append(
                    PlanStep(
                        step_index=idx,
                        tool_name=tool_name,
                        arguments=args,
                        canonical_action=canon,
                    )
                )
            if steps:
                return MacroPlan(goal=goal, steps=steps, raw_response=str(tool_calls))

        macro_plan = parse_llm_tool_plan(content, goal=goal)
        if not macro_plan.steps:
            fallback = parse_deterministic_intent(goal)
            if fallback and fallback.steps:
                return fallback

        return macro_plan

    def recover(self, context: EscalationContext) -> List[SubGoal]:
        """No automatic recovery."""
        return []
