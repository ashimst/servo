"""Tests for servo.cognition.intents and CascadeRouter handoff to System 2."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from servo.cognition import intents
from servo.cognition.router import CascadeRouter
from servo.cognition.trace import Tracer
from servo.config import settings
from servo.control.jev_gate import GoalAssessment


def test_accept_rejects_missing_confidence_negation_and_ungrounded():
    assert intents.accept("open notepad", {"function_calls": [{"name": "open_app", "arguments": {"name": "notepad"}}]}, 0.5) == (
        [], "no confidence reported"
    )
    assert intents.accept(
        "don't open notepad",
        {"function_calls": [{"name": "open_app", "arguments": {"name": "notepad"}}], "confidence": 0.95, "validation": {"negation": True}},
        0.5,
    ) == ([], "the request is negated")
    assert intents.accept(
        "open notepad",
        {"function_calls": [{"name": "open_app", "arguments": {"name": "chrome"}}], "confidence": 0.95},
        0.5,
    ) == ([], "open_app: name not in the request")
    calls, why = intents.accept(
        "open notepad",
        {"function_calls": [{"name": "open_app", "arguments": {"name": "notepad"}}], "confidence": 0.8},
        0.5,
    )
    assert len(calls) == 1 and why == ""


def test_router_hands_off_to_system2_when_intent_needle_rejects(monkeypatch):
    monkeypatch.setattr(settings, "needle_intent_weights", "fake_tuned.cact")
    laya = MagicMock()
    laya.assess_goal.return_value = GoalAssessment(
        desktop_task=True, p_desktop_task=0.9, complexity="single", confidence=0.9, domain="apps"
    )
    system2 = MagicMock()
    system2.available.return_value = True
    plan = MagicMock(done=True, subtasks=[], next_agent=None, reply="Handled by planner.")
    system2.plan.return_value = plan

    tier1 = MagicMock()
    tier1.propose.return_value = None
    tier1.last_error = ""

    router = CascadeRouter(tracer=Tracer("test-run"), laya=laya, tier1=tier1, system2=system2)
    with patch("servo.cognition.intents.read", return_value=intents.Reading([], 0.2, "no tool fits")):
        out = router.run("turn on dark mode in vs code")
    assert out.success and out.route == "system2"
    assert system2.plan.called
