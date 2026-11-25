"""The plan data types: an executor step and a list of them."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from servo.actuation.alias_mapping import CanonicalAction


class PlanStep(BaseModel):
    """One executor step: a tool name, its arguments and the canonical action they map to."""
    step_index: int
    tool_name: str
    arguments: Dict[str, Any] = Field(default_factory=dict)
    rationale: str = ""
    canonical_action: Optional[CanonicalAction] = None


class MacroPlan(BaseModel):
    """Steps run in order for one goal."""
    goal: str
    steps: List[PlanStep] = Field(default_factory=list)
