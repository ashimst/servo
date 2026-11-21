"""Kev decision gate: calibrated, non-autoregressive decisions for System 1.

Kev (https://github.com/jaredpalmer/kev) is an open, self-hostable implementation of
TypeSafe's "System One" decision model (Jev is TypeSafe's hosted model with the same
API). It never generates text: each question is answered in one forward pass with a
calibrated probability over the allowed answers.

Wire contract (``POST {base}/v1/systemone``), verified against the Kev README::

    request  {"state": ..., "model": "kev-latest",
              "questions": {"<id>": {"type": "noul"|"choice"|"score",
                                     "instructions": "...", "criteria": ...}}}
    response {"answers": {"<id>": {"type": "noul", "noul": 0.93}
                                | {"type": "choice", "choice": "x", "confidence": c,
                                   "probabilities": {...}}
                                | {"type": "score", "score": 1.4, "confidence": c,
                                   "legend": {"0": "low", ...}, "probabilities": {"0": p, ...}}}}

Servo asks Kev three kinds of question:
  * pre-action risk + scope (safety gate),
  * post-action "did the transition happen" (verification),
  * routing: is this a desktop task, and is it simple enough for System 1.

Without a Kev server every question falls back to deterministic heuristics, and the
result says so (``source="heuristic"``) so the trace never presents a guess as a model
verdict.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, Literal, Optional, Tuple

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
    source: Literal["kev", "heuristic"] = "heuristic"
    probabilities: Dict[str, float] = {}


class PostTransitionResult(BaseModel):
    """Post-action state transition verification result."""
    transition_ok: bool
    confidence: float
    explanation: str
    source: Literal["kev", "heuristic"] = "heuristic"


class GoalAssessment(BaseModel):
    """Kev's routing view of a user goal."""
    desktop_task: bool
    p_desktop_task: float
    complexity: Literal["single", "multi", "ambiguous"]
    complexity_probabilities: Dict[str, float] = {}
    confidence: float
    source: Literal["kev", "heuristic"] = "heuristic"
    reason: str = ""


# Ordered tuples (not sets) so the reported reason is deterministic.
HIGH_RISK_KEYWORDS: Tuple[str, ...] = (
    "delete", "remove", "drop", "destroy", "format", "erase", "wipe", "uninstall",
    "checkout", "pay", "order", "purchase", "buy", "transfer", "send", "shutdown", "restart",
)
MEDIUM_RISK_KEYWORDS: Tuple[str, ...] = (
    "save", "overwrite", "replace", "submit", "apply", "update", "install", "close", "exit", "clear", "reset",
)
_RISK_ORDER = {"low": 0, "medium": 1, "high": 2}


def _keyword_pattern(keywords: Tuple[str, ...]) -> "re.Pattern[str]":
    # Word-start match with suffixes: "format" matches "formatting" but not "information".
    return re.compile(r"\b(" + "|".join(map(re.escape, keywords)) + r")\w*", re.IGNORECASE)


_HIGH_RE = _keyword_pattern(HIGH_RISK_KEYWORDS)
_MEDIUM_RE = _keyword_pattern(MEDIUM_RISK_KEYWORDS)

_ACTION_VERBS = re.compile(
    r"\b(open|launch|start|close|quit|exit|click|press|select|type|write|enter|search|go to|browse|"
    r"navigate|visit|scroll|switch to|hotkey|shortcut|wait|double click|right click)\b",
    re.I,
)
_MULTI_MARKERS = re.compile(
    r"\b(then|after that|afterwards|next|finally|until|if|unless|each|every|all of|compare|find|"
    r"figure out|look up|summari[sz]e|book|schedule|organi[sz]e|reply|draft)\b|,|;",
    re.I,
)


_QUESTION = re.compile(
    r"^(what|why|how|who|whom|whose|when|where|which|is|are|was|were|do|does|did|can you (explain|tell)|"
    r"could you (explain|tell)|explain|tell me (about|why|what|how)|define|describe what)\b",
    re.I,
)


def _endpoint(url: str) -> str:
    url = url.rstrip("/")
    return url if url.endswith("/v1/systemone") else f"{url}/v1/systemone"


def _keyword_risk(text: str) -> Tuple[str, str]:
    m = _HIGH_RE.search(text)
    if m:
        return "high", f"High risk keyword detected: '{m.group(0)}'"
    m = _MEDIUM_RE.search(text)
    if m:
        return "medium", f"Medium risk keyword detected: '{m.group(0)}'"
    return "low", "Standard benign operation"


class KevClient:
    """Minimal client for the System One wire contract."""

    def __init__(
        self,
        endpoint_url: Optional[str] = None,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
    ) -> None:
        self.base_url = settings.kev_url if endpoint_url is None else endpoint_url
        self.api_key = settings.kev_api_key if api_key is None else api_key
        self.model = model or settings.kev_model
        self.timeout_seconds = settings.jev_timeout_seconds if timeout_seconds is None else timeout_seconds

    @property
    def configured(self) -> bool:
        return bool(self.base_url)

    def ask(self, state: Any, questions: Dict[str, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """POST a System One request. Returns the ``answers`` map, or None on any failure."""
        if not self.base_url:
            return None
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        try:
            with httpx.Client(timeout=self.timeout_seconds) as client:
                res = client.post(
                    _endpoint(self.base_url),
                    headers=headers,
                    json={"state": state, "model": self.model, "questions": questions},
                )
            res.raise_for_status()
            data = res.json()
            answers = data.get("answers") if isinstance(data, dict) else None
            return answers if isinstance(answers, dict) else None
        except Exception as e:
            logger.warning("Kev endpoint %s failed (%s); using heuristics.", self.base_url, e)
            return None


def _noul(answers: Dict[str, Any], key: str) -> Optional[float]:
    a = answers.get(key)
    if isinstance(a, dict) and isinstance(a.get("noul"), (int, float)):
        return float(a["noul"])
    return None


def _score_level(answers: Dict[str, Any], key: str, labels: Tuple[str, ...]) -> Optional[Tuple[str, Dict[str, float]]]:
    """Map a score answer back to its label distribution (keys '0','1',... -> labels)."""
    a = answers.get(key)
    if not isinstance(a, dict) or not isinstance(a.get("probabilities"), dict):
        return None
    probs: Dict[str, float] = {}
    for idx, label in enumerate(labels):
        try:
            probs[label] = float(a["probabilities"].get(str(idx), 0.0))
        except (TypeError, ValueError):
            return None
    if not any(probs.values()):
        return None
    return max(probs, key=probs.get), probs  # type: ignore[arg-type]


def _choice(answers: Dict[str, Any], key: str, options: Tuple[str, ...]) -> Optional[Tuple[str, Dict[str, float], float]]:
    a = answers.get(key)
    if not isinstance(a, dict) or a.get("choice") not in options:
        return None
    probs = {k: float(v) for k, v in (a.get("probabilities") or {}).items() if isinstance(v, (int, float))}
    conf = a.get("confidence")
    return a["choice"], probs, float(conf) if isinstance(conf, (int, float)) else 0.0


class JevGate:
    """Kev-backed decision gate with deterministic heuristic fallback."""

    def __init__(
        self,
        endpoint_url: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
        use_fallback: Optional[bool] = None,
    ) -> None:
        self.client = KevClient(endpoint_url=endpoint_url, timeout_seconds=timeout_seconds)
        self.endpoint_url = self.client.base_url
        self.use_fallback = settings.use_heuristic_jev_fallback if use_fallback is None else use_fallback
        # The model is asked whenever a Kev URL is configured, unless heuristics are forced.
        # Heuristics remain the per-call fallback when the server fails or answers badly.
        self.model_enabled = self.client.configured and not self.use_fallback

    def _ask(self, state: Any, questions: Dict[str, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not self.model_enabled:
            return None
        return self.client.ask(state, questions)

    # ----------------------------------------------------------------- safety

    def evaluate_pre_risk(self, sub_goal: str, tool_call: str, element_label: str) -> PreRiskResult:
        """Classify risk (low | medium | high) and scope validity before actuation.

        The final risk is the MAX of Kev's level and the keyword floor: a model may raise
        the risk of an innocuous-sounding action, but never lower one that names a
        destructive verb.
        """
        combined_text = f"{sub_goal} {tool_call} {element_label}"
        kw_level, kw_reason = _keyword_risk(combined_text)

        answers = self._ask(
            {"sub_goal": sub_goal, "proposed_tool_call": tool_call, "target_element": element_label},
            {
                "risk_level": {
                    "type": "score",
                    "instructions": "How risky is performing this desktop action for the user? "
                                    "High = irreversible, financial, destructive or sends data to others.",
                    "criteria": ["low", "medium", "high"],
                },
                "scope_valid": {
                    "type": "noul",
                    "instructions": "Does the proposed tool call do what the sub-goal asks, and nothing beyond it?",
                },
            },
        )
        if answers is not None:
            level = _score_level(answers, "risk_level", ("low", "medium", "high"))
            scope = _noul(answers, "scope_valid")
            if level is not None and scope is not None:
                model_level, probs = level
                # Conservative upgrade: substantial mass on "high" counts as high.
                if probs.get("high", 0.0) >= settings.kev_high_risk_floor:
                    model_level = "high"
                final = max(model_level, kw_level, key=lambda r: _RISK_ORDER[r])
                reason = f"Kev: P(low/med/high)={probs.get('low', 0):.2f}/{probs.get('medium', 0):.2f}/{probs.get('high', 0):.2f}"
                if final != model_level:
                    reason += f"; raised by keyword floor ({kw_reason})"
                return PreRiskResult(
                    risk_level=final,  # type: ignore[arg-type]
                    scope_valid=scope >= 0.5,
                    reason=reason,
                    source="kev",
                    probabilities={**probs, "scope_valid": scope},
                )
            logger.warning("Kev returned malformed pre-risk answers %r; using heuristic gate.", answers)

        return PreRiskResult(risk_level=kw_level, scope_valid=True, reason=f"{kw_reason} (Heuristic)")  # type: ignore[arg-type]

    def evaluate_post_transition(
        self,
        sub_goal: str,
        delta: SceneDelta,
        expected_outcome: str = "",
        typed_text: str = "",
    ) -> PostTransitionResult:
        """Verify whether observed scene changes correspond to the expected sub-goal outcome."""
        answers = self._ask(
            {
                "sub_goal": sub_goal,
                "expected_outcome": expected_outcome,
                "typed_text": typed_text,
                "observed_delta": {
                    "has_changed": delta.has_changed,
                    "title_changed": delta.title_changed,
                    "new_title": delta.new_title,
                    "added_elements": delta.added_labels[:50],
                    "removed_elements": delta.removed_labels[:50],
                    "value_changes": dict(list(delta.value_changes.items())[:20]),
                },
            },
            {
                "transition_ok": {
                    "type": "noul",
                    "instructions": "Does the observed UI change show that the sub-goal was accomplished?",
                }
            },
        )
        if answers is not None:
            p = _noul(answers, "transition_ok")
            if p is not None:
                ok = p >= 0.5
                return PostTransitionResult(
                    transition_ok=ok, confidence=p if ok else 1 - p,
                    explanation=f"Kev verified transition (P(ok)={p:.2f})", source="kev",
                )
            logger.warning("Kev returned malformed post-transition answer; using heuristic gate.")

        # ---- Heuristic fallback ----
        if typed_text:
            for change in delta.value_changes.values():
                if typed_text in (change.get("new_value") or ""):
                    return PostTransitionResult(
                        transition_ok=True, confidence=0.95,
                        explanation="Typed text observed in control value (Heuristic)",
                    )
            if not delta.value_changes and delta.has_changed:
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

    # ---------------------------------------------------------------- routing

    def assess_goal(self, goal: str, screen_summary: str = "", tier1_parsed: bool = False) -> GoalAssessment:
        """Routing decision: is this a desktop task, and can System 1 do it directly?"""
        answers = self._ask(
            {"user_request": goal, "screen": screen_summary[:4000]},
            {
                "desktop_task": {
                    "type": "noul",
                    "instructions": "Is the user asking the assistant to operate the computer "
                                    "(open apps, click, type, browse the web) rather than only chat?",
                },
                "complexity": {
                    "type": "choice",
                    "instructions": "How much reasoning does carrying out this request need?",
                    "criteria": {
                        "single": "One direct action, or opening an app plus one direct action.",
                        "multi": "Several dependent steps, or it needs lookup, judgement or reading the screen.",
                        "ambiguous": "It is unclear what to act on.",
                    },
                },
            },
        )
        if answers is not None:
            p_task = _noul(answers, "desktop_task")
            cx = _choice(answers, "complexity", ("single", "multi", "ambiguous"))
            if p_task is not None and cx is not None:
                return GoalAssessment(
                    desktop_task=p_task >= 0.5,
                    p_desktop_task=p_task,
                    complexity=cx[0],  # type: ignore[arg-type]
                    complexity_probabilities=cx[1],
                    confidence=cx[2],
                    source="kev",
                    reason=f"Kev: P(desktop task)={p_task:.2f}, complexity={cx[0]}",
                )

        # Heuristic: questions are chat, imperatives are tasks; sequencing/lookup words mark
        # multi-step work.
        verbs = _ACTION_VERBS.findall(goal)
        is_question = bool(_QUESTION.match(goal.strip())) or goal.strip().endswith("?")
        is_task = tier1_parsed or bool(verbs) or not is_question
        markers = _MULTI_MARKERS.findall(goal)
        n_and = len(re.findall(r"\band\b", goal, re.I))
        if tier1_parsed and not markers:
            cx_label, conf = "single", 0.9
        elif markers or len(verbs) >= 3 or n_and >= 2 or len(goal.split()) > 18:
            cx_label, conf = "multi", 0.7
        elif is_task:
            cx_label, conf = "single", 0.6
        else:
            cx_label, conf = "ambiguous", 0.5
        return GoalAssessment(
            desktop_task=is_task,
            p_desktop_task=0.9 if is_task else 0.1,
            complexity=cx_label,  # type: ignore[arg-type]
            confidence=conf,
            source="heuristic",
            reason=f"Heuristic: {len(verbs)} action verb(s), {len(markers)} sequencing marker(s)",
        )


# Kev is the open implementation; JevGate is kept as the import name for compatibility.
KevGate = JevGate
