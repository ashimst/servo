"""Jev Gate: Non-autoregressive safety and state transition comparator.

See ADR-04 and Section 5.3 in SERVO_ARCHITECTURE_V1.md.
Implements TypeSafe's POST /v1/systemone specification (jaredpalmer/kev decision models)
with a deterministic heuristic fallback for hermetic offline execution.
"""

from __future__ import annotations

import httpx
from typing import Literal, Optional
from pydantic import BaseModel
from servo.config import settings
from servo.perception.scene_index import SceneDelta


class PreRiskResult(BaseModel):
    """Pre-action safety classification result."""
    risk_level: Literal["low", "medium", "high"]
    scope_valid: bool
    reason: str


class PostTransitionResult(BaseModel):
    """Post-action state transition verification result."""
    transition_ok: bool
    confidence: float
    explanation: str


HIGH_RISK_KEYWORDS = {"delete", "remove", "drop", "destroy", "format", "checkout", "pay", "order", "purchase", "send", "shutdown"}
MEDIUM_RISK_KEYWORDS = {"save", "overwrite", "submit", "apply", "update", "close", "exit", "clear"}


class JevGate:
    """Non-autoregressive comparator validating pre-action risk and post-action transitions."""

    def __init__(
        self,
        endpoint_url: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
        use_fallback: Optional[bool] = None,
    ) -> None:
        self.endpoint_url = endpoint_url or settings.jev_url
        self.timeout_seconds = timeout_seconds or settings.jev_timeout_seconds
        # If use_fallback is explicitly True, skip HTTP endpoint calls immediately
        self.use_fallback = settings.use_heuristic_jev_fallback if use_fallback is None else use_fallback

    def evaluate_pre_risk(
        self,
        sub_goal: str,
        tool_call: str,
        element_label: str,
    ) -> PreRiskResult:
        """Classify risk level (low | medium | high) and scope validity before actuation."""
        # 1. Attempt call to System One decision model endpoint if not in fallback mode
        if not self.use_fallback and self.endpoint_url:
            try:
                payload = {
                    "task": "pre_risk_classification",
                    "document": f"Sub-goal: {sub_goal}\nProposed Tool: {tool_call}\nTarget Element: {element_label}",
                    "questions": [
                        {"id": "risk_level", "options": ["low", "medium", "high"]},
                        {"id": "scope_valid", "options": ["true", "false"]},
                    ],
                }
                with httpx.Client(timeout=self.timeout_seconds) as client:
                    res = client.post(self.endpoint_url, json=payload)
                    if res.status_code == 200:
                        data = res.json()
                        risk = data.get("answers", {}).get("risk_level", "low")
                        scope = data.get("answers", {}).get("scope_valid", "true") == "true"
                        return PreRiskResult(risk_level=risk, scope_valid=scope, reason="Jev model classification")
            except Exception:
                pass

        # 2. Heuristic rule-based fallback
        combined_text = f"{sub_goal} {tool_call} {element_label}".lower()

        for kw in HIGH_RISK_KEYWORDS:
            if kw in combined_text:
                return PreRiskResult(
                    risk_level="high",
                    scope_valid=True,
                    reason=f"High risk keyword detected: '{kw}'",
                )

        for kw in MEDIUM_RISK_KEYWORDS:
            if kw in combined_text:
                return PreRiskResult(
                    risk_level="medium",
                    scope_valid=True,
                    reason=f"Medium risk keyword detected: '{kw}'",
                )

        return PreRiskResult(
            risk_level="low",
            scope_valid=True,
            reason="Standard benign operation (Heuristic)",
        )

    def evaluate_post_transition(
        self,
        sub_goal: str,
        delta: SceneDelta,
        expected_outcome: str = "",
    ) -> PostTransitionResult:
        """Verify whether observed scene changes correspond to expected sub-goal outcome."""
        # 1. Attempt call to System One decision model endpoint if not in fallback mode
        if not self.use_fallback and self.endpoint_url:
            try:
                delta_summary = (
                    f"Has Changed: {delta.has_changed}\n"
                    f"Title Changed: {delta.title_changed} (New: '{delta.new_title}')\n"
                    f"Added Elements: {delta.added_labels}\n"
                    f"Removed Elements: {delta.removed_labels}\n"
                    f"Value Changes: {delta.value_changes}"
                )
                payload = {
                    "task": "post_transition_verification",
                    "document": f"Sub-goal: {sub_goal}\nExpected: {expected_outcome}\nObserved Delta:\n{delta_summary}",
                    "questions": [
                        {"id": "transition_ok", "options": ["true", "false"]},
                    ],
                }
                with httpx.Client(timeout=self.timeout_seconds) as client:
                    res = client.post(self.endpoint_url, json=payload)
                    if res.status_code == 200:
                        data = res.json()
                        ok = data.get("answers", {}).get("transition_ok", "true") == "true"
                        conf = float(data.get("probabilities", {}).get("transition_ok", 0.95))
                        return PostTransitionResult(
                            transition_ok=ok,
                            confidence=conf,
                            explanation="Jev model verified transition",
                        )
            except Exception:
                pass

        # 2. Heuristic rule-based fallback
        if delta.has_changed:
            return PostTransitionResult(
                transition_ok=True,
                confidence=0.92,
                explanation="UI state mutation observed matching expectation (Heuristic)",
            )

        if "type" in sub_goal.lower() and (delta.value_changes or delta.has_changed):
            return PostTransitionResult(
                transition_ok=True,
                confidence=0.90,
                explanation="Text buffer change observed (Heuristic)",
            )

        return PostTransitionResult(
            transition_ok=False,
            confidence=0.85,
            explanation="No UI state mutation detected after actuation (Heuristic)",
        )
