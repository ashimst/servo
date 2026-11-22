"""Mind2Web benchmark evaluation harness for Servo web tier."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from servo.integrations.mind2web.runner import Mind2WebRunner
    from servo.integrations.mind2web.tasks import Mind2WebTask, get_mind2web_benchmark_suite
    from servo.integrations.mind2web.evaluator import Mind2WebEvaluator


def __getattr__(name: str) -> Any:
    if name == "Mind2WebRunner":
        from servo.integrations.mind2web.runner import Mind2WebRunner
        return Mind2WebRunner
    if name in ("Mind2WebTask", "get_mind2web_benchmark_suite"):
        from servo.integrations.mind2web import tasks
        return getattr(tasks, name)
    if name == "Mind2WebEvaluator":
        from servo.integrations.mind2web.evaluator import Mind2WebEvaluator
        return Mind2WebEvaluator
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["Mind2WebRunner", "Mind2WebTask", "get_mind2web_benchmark_suite", "Mind2WebEvaluator"]
