"""Jev Gate: Non-autoregressive safety and state transition comparator.

See ADR-04 and Section 5.3 in SERVO_ARCHITECTURE_V1.md, and SERVO_HARDENING_NOTES.md.
Implements TypeSafe's POST /v1/systemone specification (jaredpalmer/kev decision models)
with a deterministic heuristic fallback for hermetic offline execution.
"""

from __future__ import annotations

import logging
import re
from typing import Literal, Optional, Tuple

import httpx
from pydantic import BaseModel

from servo.config import settings
from servo.perception.scene_index import SceneDelta

logger = logging.getLogger(__name__)


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


# Ordered tuples (not sets) so the reported reason is deterministic.
HIGH_RISK_KEYWORDS: Tuple[str, ...] = (
    "delete", "remove", "drop", "destroy", "format", "erase", "wipe", "uninstall",
    "checkout", "pay", "order", "purchase", "buy", "transfer", "send", "shutdown", "restart",
)
MEDIUM_RISK_KEYWORDS: Tuple[str, ...] = (
    "save", "overwrite", "replace", "submit", "apply", "update", "install", "close", "exit", "clear", "reset",
)


def _keyword_pattern(keywords: Tuple[str, ...]) -> "re.Pattern[str]":
    # Match at the START of a word and allow suffixes: "format" matches "formatting"
    # but not "information"; "order" matches "orders" but not "border"; "send" matches
    # "sending" but not "resend". The previous substring match flagged all of those.
    return re.compile(r"\b(" + "|".join(map(re.escape, keywords)) + r")\w*", re.IGNORECASE)


_HIGH_RE = _keyword_pattern(HIGH_RISK_KEYWORDS)
_MEDIUM_RE = _keyword_pattern(MEDIUM_RISK_KEYWORDS)


def _bool_answer(value: object) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in ("true", "false"):
        return value.lower() == "true"
    return None


class JevGate:
    """Non-autoregressive comparator validating pre-action risk and post-action transitions."""

    def __init__(
        self,
        endpoint_url: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
        use_fallback: Optional[bool] = None,
    ) -> None:
        self.endpoint_url = settings.jev_url if endpoint_url is None else endpoint_url
        self.timeout_seconds = settings.jev_timeout_seconds if timeout_seconds is None else timeout_seconds
        self.use_fallback = settings.use_heuristic_jev_fallback if use_fallback is None else use_fallback
        if not self.use_fallback and not self.endpoint_url:
            logger.warning("Jev model disabled fallback but SERVO_JEV_URL is empty; using heuristic gate.")

    def _query_model(self, payload: dict) -> Optional[dict]:
        if self.use_fallback or not self.endpoint_url:
            return None
        try:
            with httpx.Client(timeout=self.timeout_seconds) as client:
                res = client.post(self.endpoint_url, json=payload)
            res.raise_for_status()
            data = res.json()
            return data if isinstance(data, dict) else None
        except Exception as e:
            logger.warning("Jev model endpoint %s failed (%s); using heuristic gate.", self.endpoint_url, e)
            return None

    def evaluate_pre_risk(
        self,
        sub_goal: str,
        tool_call: str,
        element_label: str,
    ) -> PreRiskResult:
        """Classify risk level (low | medium | high) and scope validity before actuation."""
        data = self._query_model({
            "task": "pre_risk_classification",
            "document": f"Sub-goal: {sub_goal}\nProposed Tool: {tool_call}\nTarget Element: {element_label}",
            "questions": [
                {"id": "risk_level", "options": ["low", "medium", "high"]},
                {"id": "scope_valid", "options": ["true", "false"]},
            ],
        })
        if data is not None:
            answers = data.get("answers", {}) or {}
            risk = answers.get("risk_level")
            scope = _bool_answer(answers.get("scope_valid"))
            # Missing / malformed answers are NOT defaulted to "low"/"true" (the old
            # behaviour): a broken model response must not lower the risk rating.
            if risk in ("low", "medium", "high") and scope is not None:
                return PreRiskResult(risk_level=risk, scope_valid=scope, reason="Jev model classification")
            logger.warning("Jev model returned malformed pre-risk answers %r; using heuristic gate.", answers)

        combined_text = f"{sub_goal} {tool_call} {element_label}"

        m = _HIGH_RE.search(combined_text)
        if m:
            return PreRiskResult(risk_level="high", scope_valid=True, reason=f"High risk keyword detected: '{m.group(0)}'")

        m = _MEDIUM_RE.search(combined_text)
        if m:
            return PreRiskResult(risk_level="medium", scope_valid=True, reason=f"Medium risk keyword detected: '{m.group(0)}'")

        return PreRiskResult(risk_level="low", scope_valid=True, reason="Standard benign operation (Heuristic)")

    def evaluate_post_transition(
        self,
        sub_goal: str,
        delta: SceneDelta,
        expected_outcome: str = "",
        typed_text: str = "",
    ) -> PostTransitionResult:
        """Verify whether observed scene changes correspond to the expected sub-goal outcome."""
        delta_summary = (
            f"Has Changed: {delta.has_changed}\n"
            f"Title Changed: {delta.title_changed} (New: '{delta.new_title}')\n"
            f"Added Elements: {delta.added_labels}\n"
            f"Removed Elements: {delta.removed_labels}\n"
            f"Value Changes: {delta.value_changes}"
        )
        data = self._query_model({
            "task": "post_transition_verification",
            "document": f"Sub-goal: {sub_goal}\nExpected: {expected_outcome}\nObserved Delta:\n{delta_summary}",
            "questions": [{"id": "transition_ok", "options": ["true", "false"]}],
        })
        if data is not None:
            ok = _bool_answer((data.get("answers", {}) or {}).get("transition_ok"))
            if ok is not None:
                try:
                    conf = float((data.get("probabilities", {}) or {}).get("transition_ok", 0.5))
                except (TypeError, ValueError):
                    conf = 0.5
                return PostTransitionResult(transition_ok=ok, confidence=conf, explanation="Jev model verified transition")
            logger.warning("Jev model returned malformed post-transition answer; using heuristic gate.")

        # ---- Heuristic fallback ----
        # Typing has a checkable postcondition: the text must show up in a value.
        if typed_text:
            for change in delta.value_changes.values():
                if typed_text in (change.get("new_value") or ""):
                    return PostTransitionResult(
                        transition_ok=True, confidence=0.95,
                        explanation="Typed text observed in control value (Heuristic)",
                    )
            if not delta.value_changes and delta.has_changed:
                # The target exposes no readable value (custom control); the best we
                # can say is that something changed.
                return PostTransitionResult(
                    transition_ok=True, confidence=0.5,
                    explanation="UI changed but the target exposes no readable value (Heuristic, unverified)",
                )
            return PostTransitionResult(
                transition_ok=False, confidence=0.8,
                explanation="Typed text was not observed in any control value after actuation (Heuristic)",
            )

        if not delta.has_changed:
            return PostTransitionResult(
                transition_ok=False, confidence=0.85,
                explanation="No UI state mutation detected after actuation (Heuristic)",
            )

        # A click produced *some* change. Without a model we cannot prove it was the
        # intended one; if an expected outcome was given and none of its words appear in
        # the delta, accept with low confidence and say so rather than claiming 0.92.
        if expected_outcome:
            observed = " ".join(
                [delta.new_title, *delta.added_labels, *delta.removed_labels,
                 *(v.get("new_value") or "" for v in delta.value_changes.values())]
            ).lower()
            words = [w for w in re.findall(r"\w{4,}", expected_outcome.lower())]
            if words and any(w in observed for w in words):
                return PostTransitionResult(
                    transition_ok=True, confidence=0.85,
                    explanation="UI changed and the change mentions the expected outcome (Heuristic)",
                )
            return PostTransitionResult(
                transition_ok=True, confidence=0.55,
                explanation="UI changed but could not be matched to the expected outcome (Heuristic, unverified)",
            )

        return PostTransitionResult(
            transition_ok=True, confidence=0.7,
            explanation="UI state mutation observed; no expected outcome to compare (Heuristic)",
        )
