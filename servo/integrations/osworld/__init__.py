"""OSWorld benchmark evaluation harness for Servo."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from servo.integrations.osworld.runner import OSWorldRunner
    from servo.integrations.osworld.tasks import OSWorldTask, get_osworld_benchmark_suite


def __getattr__(name: str) -> Any:
    if name == "OSWorldRunner":
        from servo.integrations.osworld.runner import OSWorldRunner
        return OSWorldRunner
    if name in ("OSWorldTask", "get_osworld_benchmark_suite"):
        from servo.integrations.osworld import tasks
        return getattr(tasks, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["OSWorldRunner", "OSWorldTask", "get_osworld_benchmark_suite"]
